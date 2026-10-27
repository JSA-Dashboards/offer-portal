"""purchases.py — what staff bought today, logged by staff, summarised by commodity for the end-of-day recap.

A purchase is a buy the elevator made (by phone, in person, or through an offer): the commodity, bushels, flat price
and/or basis, delivery location and period, and the entity or person it was bought from. Staff see and edit only their own
entries; admins see everyone's. Entries are never edited in place: a mistake is voided and re-entered.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from . import contracts
from .data import CONTRACT_BUSHELS, PortalError, _audit, _insert, _require_staff, new_id, today_ct
from .db import Db

_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _is_admin(role: str) -> bool:
    return role in ("elevator_admin", "platform_admin")


def add_purchase(db: Db, tenant_id: str, staff_id: int, *, grain: str, bushels: int, counterparty: str | None = None,
                 farmer_id: int | None = None, price: float | None = None, basis_cents: int | None = None,
                 futures_symbol: str | None = None, location_id: int | None = None, delivery_period: str | None = None,
                 contract_ref: str | None = None, note: str | None = None, purchase_date: date | None = None,
                 offer_id: int | None = None) -> int:
    _require_staff(db, tenant_id, staff_id)
    if grain not in CONTRACT_BUSHELS:
        raise PortalError("Choose a commodity.")
    if not bushels or bushels <= 0:
        raise PortalError("Bushels must be positive.")
    if price is None and basis_cents is None:
        raise PortalError("Enter the flat price, the basis, or both.")
    if price is not None and price <= 0:
        raise PortalError("The price must be positive.")
    grain = contracts.canon_grain(grain)
    sym = (futures_symbol or "").strip().upper() or None
    if basis_cents is not None and not sym:
        raise PortalError("A basis purchase needs its futures month (e.g. ZCZ26).")
    if sym:
        try:
            sym = contracts.check_symbol(grain, sym)
        except ValueError as e:
            raise PortalError(str(e)) from None
    period = (delivery_period or "").strip() or None
    if period and not _PERIOD.match(period):
        raise PortalError("Delivery period must look like 2026-12.")
    day = purchase_date or today_ct()
    if day > today_ct():
        raise PortalError("A purchase can't be dated in the future.")
    name = (counterparty or "").strip()
    if farmer_id is not None:
        f = db.query("SELECT name FROM farmers WHERE farmer_id = %s AND tenant_id = %s", (farmer_id, tenant_id))
        if not f:
            raise PortalError("Farmer not found for this tenant.")
        name = f[0]["name"]
    if not name:
        raise PortalError("Enter the entity or person the grain was bought from.")
    if len(name) > 120:
        raise PortalError("The name is limited to 120 characters.")
    loc_name = None
    if location_id is not None:
        loc = db.query("SELECT name FROM locations WHERE location_id = %s AND tenant_id = %s", (location_id, tenant_id))
        if not loc:
            raise PortalError("Unknown delivery location.")
        loc_name = loc[0]["name"]
    ref = (contract_ref or "").strip() or None
    if ref and len(ref) > 60:
        raise PortalError("Contract reference is limited to 60 characters.")
    pid = new_id()
    row = dict(purchase_id=pid, tenant_id=tenant_id, purchase_date=day.isoformat(), staff_user_id=staff_id,
               farmer_id=farmer_id, counterparty=name, grain=grain, bushels=int(bushels), price=price,
               basis_cents=basis_cents, futures_symbol=sym, location_id=location_id,
               delivery_location=loc_name, delivery_period=period, contract_ref=ref,
               note=(note or "").strip()[:300] or None)
    if offer_id is not None:
        row["offer_id"] = offer_id
    _insert(db, "purchases", row)
    _audit(db, tenant_id, staff_id, "purchase_logged", dict(purchase_id=pid, grain=grain, bushels=int(bushels)))
    return pid


def list_purchases(db: Db, tenant_id: str, user: dict, day: date | None = None, *, staff_id: int | None = None) -> list[dict]:
    """A day's purchases (default today, Central). Staff get only their own; admins get everyone's (optionally one staff)."""
    _require_staff(db, tenant_id, user["user_id"])
    day = day or today_ct()
    sql = ("SELECT p.*, u.name AS staff_name, u.email AS staff_email FROM purchases p "
           "LEFT JOIN users u ON u.user_id = p.staff_user_id WHERE p.tenant_id = %s AND p.purchase_date = %s "
           "AND p.voided_at IS NULL")
    params: list = [tenant_id, day.isoformat()]
    if not _is_admin(user["role"]):
        sql += " AND p.staff_user_id = %s"
        params.append(user["user_id"])
    elif staff_id is not None:
        sql += " AND p.staff_user_id = %s"
        params.append(staff_id)
    return db.query(sql + " ORDER BY p.grain, p.created_at", tuple(params))


def void_purchase(db: Db, tenant_id: str, user: dict, purchase_id: int) -> None:
    """Staff void their own entry; an admin can void anyone's."""
    _require_staff(db, tenant_id, user["user_id"])
    r = db.query("SELECT staff_user_id, voided_at FROM purchases WHERE purchase_id = %s AND tenant_id = %s",
                 (purchase_id, tenant_id))
    if not r or r[0]["voided_at"] is not None:
        raise PortalError("Purchase not found.")
    if r[0]["staff_user_id"] != user["user_id"] and not _is_admin(user["role"]):
        raise PortalError("You can only void your own purchases.")
    db.execute("UPDATE purchases SET voided_at = CURRENT_TIMESTAMP() WHERE purchase_id = %s AND tenant_id = %s",
               (purchase_id, tenant_id))
    _audit(db, tenant_id, user["user_id"], "purchase_voided", dict(purchase_id=purchase_id))


def summarize(rows: list[dict]) -> list[dict]:
    """By commodity: totals, weighted average price and basis, and the entries (price, basis, delivery location and
    period, who from)."""
    groups: dict = {}
    for p in rows:
        g = groups.setdefault(p["grain"], dict(grain=p["grain"], entries=0, bushels=0, px_bu=0, px_sum=0.0,
                                               bs_bu=0, bs_sum=0.0, rows=[]))
        g["entries"] += 1
        g["bushels"] += p["bushels"]
        if p["price"] is not None:
            g["px_bu"] += p["bushels"]
            g["px_sum"] += p["bushels"] * p["price"]
        if p["basis_cents"] is not None:
            g["bs_bu"] += p["bushels"]
            g["bs_sum"] += p["bushels"] * p["basis_cents"]
        g["rows"].append(p)
    out = []
    for g in sorted(groups.values(), key=lambda g: g["grain"]):
        out.append(dict(grain=g["grain"], entries=g["entries"], bushels=g["bushels"],
                        avg_price=round(g["px_sum"] / g["px_bu"], 4) if g["px_bu"] else None,
                        avg_basis_cents=round(g["bs_sum"] / g["bs_bu"], 1) if g["bs_bu"] else None,
                        rows=sorted(g["rows"], key=lambda p: (p["delivery_period"] or "", p["counterparty"]))))
    return out


# ── purchases that come from offers ─────────────────────────────────────────────
# When staff ACKNOWLEDGE a hit offer the elevator has bought that grain: it is added to that day's purchases (so the
# recap carries it) in the name of the person who acknowledged it. One purchase per offer; the contract number follows
# when the offer is completed; cancelling the offer voids the purchase.
def linked_ready(db: Db) -> bool:
    """True once sql/13 has added purchases.offer_id. Until then offers simply don't create purchases."""
    import time
    now = time.monotonic()
    state = getattr(db, "_purchase_link_ready", None)
    if state and (state[0] or now - state[1] < 60):
        return state[0]
    try:
        db.query("SELECT offer_id FROM purchases LIMIT 1")
        ok = True
    except Exception:
        ok = False
    db._purchase_link_ready = (ok, now)
    return ok


def record_from_offer(db: Db, tenant_id: str, offer_id: int, staff_id: int) -> int | None:
    """The purchase for an acknowledged offer (None if it already has one, or sql/13 hasn't run). Priced as the offer is:
    a flat-price offer logs its price (and the basis it was struck against); a basis (HTA) offer logs its basis."""
    import json
    from .data import delivery_period
    if not linked_ready(db):
        return None
    if db.query("SELECT 1 AS x FROM purchases WHERE tenant_id = %s AND offer_id = %s", (tenant_id, offer_id)):
        return None                                           # one per offer, even if someone voided it since
    r = db.query("SELECT * FROM offers WHERE offer_id = %s AND tenant_id = %s", (offer_id, tenant_id))
    if not r:
        return None
    o = r[0]
    snap = o.get("bid_snapshot")
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except ValueError:
            snap = None
    loc = o.get("location_id") if o.get("location_id") is not None else (snap or {}).get("location_id")
    period = delivery_period(o)
    fixed = o["price_type"] == "FIXED"
    kind = {"CASH": "cash", "HTA": "HTA", "FUTURES_ONLY": "futures-only"}.get(o["contract_type"], o["contract_type"])
    return add_purchase(
        db, tenant_id, staff_id, grain=o["grain"], bushels=o["bushels"], farmer_id=o["farmer_id"],
        price=o["price"] if fixed else None,
        basis_cents=o["basis_ref_cents"] if fixed else o["basis_cents"], futures_symbol=o["futures_symbol"],
        location_id=loc, delivery_period=None if period == "Unscheduled" else period,
        contract_ref=o.get("contract_ref"), note=f"From offer #{offer_id} ({kind}, purchase confirmed)", offer_id=offer_id)


def set_contract_for_offer(db: Db, tenant_id: str, offer_id: int, contract_ref: str) -> None:
    """The offer was completed: its purchase carries the contract number."""
    if linked_ready(db):
        db.execute("UPDATE purchases SET contract_ref = %s WHERE tenant_id = %s AND offer_id = %s AND voided_at IS NULL",
                   (contract_ref, tenant_id, offer_id))


def void_for_offer(db: Db, tenant_id: str, offer_id: int, actor_id: int) -> int:
    """The offer was cancelled after its hit: its purchase no longer stands. Returns how many were voided."""
    if not linked_ready(db):
        return 0
    rows = db.query("SELECT purchase_id FROM purchases WHERE tenant_id = %s AND offer_id = %s AND voided_at IS NULL",
                    (tenant_id, offer_id))
    for r in rows:
        db.execute("UPDATE purchases SET voided_at = CURRENT_TIMESTAMP() WHERE purchase_id = %s AND tenant_id = %s",
                   (r["purchase_id"], tenant_id))
        _audit(db, tenant_id, actor_id, "purchase_voided", dict(purchase_id=r["purchase_id"], offer_id=offer_id,
                                                                reason="offer cancelled"))
    return len(rows)
