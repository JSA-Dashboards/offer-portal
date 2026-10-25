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
                 contract_ref: str | None = None, note: str | None = None, purchase_date: date | None = None) -> int:
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
    _insert(db, "purchases", dict(purchase_id=pid, tenant_id=tenant_id, purchase_date=day.isoformat(), staff_user_id=staff_id,
                                  farmer_id=farmer_id, counterparty=name, grain=grain, bushels=int(bushels), price=price,
                                  basis_cents=basis_cents, futures_symbol=sym, location_id=location_id,
                                  delivery_location=loc_name, delivery_period=period, contract_ref=ref,
                                  note=(note or "").strip()[:300] or None))
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
