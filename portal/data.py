"""data.py — tenant-scoped data layer. The UI calls these and never writes SQL.

Rule: every function that touches tenant-owned data takes `tenant_id` first and
puts it in the WHERE clause (or the INSERT). Platform-level functions (tenants,
platform users) are at the bottom and are for platform_admin only.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import contracts
from .db import Db

CT = ZoneInfo("America/Chicago")          # Cloud hosts run UTC; business dates are Central

CONTRACT_TYPES = ("CASH", "HTA", "FUTURES_ONLY")
PRICE_TYPES = ("FIXED", "BASIS")
ROLES = ("platform_admin", "elevator_admin", "elevator_staff", "farmer")
ELEVATOR_ROLES = ("elevator_admin", "elevator_staff")

# status -> {action: (new_status, roles allowed)}
_TRANSITIONS = {
    "submitted": {"hit": ("hit", ELEVATOR_ROLES), "decline": ("declined", ELEVATOR_ROLES),
                  "withdraw": ("withdrawn", ("farmer",)), "expire": ("expired", ())},
}                                     # hit -> acknowledged -> completed: acknowledge_offer / complete_offer; cancel_offer after a hit

CONTRACT_BUSHELS = {**{g: sp["bushels"] for g, sp in contracts.SPECS.items()}, **{k: 5000 for k in contracts.LEGACY}}
STATUS_LABELS = {"submitted": "Outstanding", "hit": "Hit", "acknowledged": "Acknowledged", "completed": "Completed",
                 "declined": "Declined", "withdrawn": "Withdrawn", "expired": "Expired", "cancelled": "Cancelled"}
HELD = ("hit", "acknowledged")        # the elevator owns the grain and the hedge question is open


class PortalError(ValueError):
    pass


class PublishResult(int):
    """publish_bid's return value: the number of flat-price offers re-priced (an int, so existing
    callers still compare it), with `.auto_hit` = how many offers the new bid then hit automatically."""
    auto_hit = 0


def new_id() -> int:
    """An id the app assigns itself: milliseconds since 2026-01-01, then 5 random digits.

    Why not let Snowflake's IDENTITY pick it and read it back with MAX(id)? Snowflake does not promise
    IDENTITY values increase (an integration run returned the same id for two different offers), so
    "insert, then take the max" can hand back someone else's row. Assigning the id up front removes the
    guesswork and keeps ids unique across concurrent writers. Within one process an id never repeats and
    never goes backwards; across processes a clash needs two writers in the same millisecond AND the same
    five random digits (about 1 in 100,000)."""
    global _LAST_ID
    with _ID_LOCK:
        cand = (time.time_ns() // 1_000_000 - _EPOCH_MS) * 100_000 + secrets.randbelow(100_000)
        _LAST_ID = cand if cand > _LAST_ID else _LAST_ID + 1
        return _LAST_ID


_EPOCH_MS = 1_767_225_600_000        # 2026-01-01T00:00:00Z
_LAST_ID = 0
_ID_LOCK = threading.Lock()


def _notify(db: Db, tenant_id: str, kind: str, offer_id: int, **ctx) -> None:
    """Queue the email(s) for an offer event. Best effort: a notification problem must never undo or block the
    action that caused it (the portal/notify.py outbox is added separately; until then this does nothing)."""
    try:
        from . import notify
    except ImportError:
        return
    try:
        notify.offer_event(db, tenant_id, kind, offer_id, **ctx)
    except Exception:                                   # pragma: no cover - never break the caller
        pass


def portal_link(tenant_id: str) -> str:
    """The tenant's portal address for emails ('' when OFFER_PORTAL_URL is not set)."""
    base = os.environ.get("OFFER_PORTAL_URL", "").rstrip("/")
    return f"{base}/?t={tenant_id}" if base and tenant_id else ""


def today_ct():
    return datetime.now(CT).date()


def _j(v) -> str:
    return json.dumps(v, default=str)


def _insert(db: Db, table: str, cols: dict, json_cols: tuple = ()) -> None:
    """INSERT ... SELECT so Snowflake can PARSE_JSON into VARIANT columns."""
    names = ", ".join(cols)
    vals = ", ".join(db.json_fn if c in json_cols else "%s" for c in cols)
    params = tuple(_j(v) if c in json_cols else v for c, v in cols.items())
    db.execute(f"INSERT INTO {table} ({names}) SELECT {vals}", params)


def _usage(db: Db, tenant_id: str, event_type: str, ref_id=None, qty: int = 1) -> None:
    _insert(db, "usage_events", dict(tenant_id=tenant_id, event_type=event_type,
                                     quantity=qty, ref_id=ref_id))


def _audit(db: Db, tenant_id, actor_id, action: str, detail=None) -> None:
    _insert(db, "audit_log", dict(tenant_id=tenant_id, actor_user_id=actor_id,
                                  action=action, detail=detail or {}), ("detail",))


# ── Tenant reads ────────────────────────────────────────────────────────────
def get_tenant(db: Db, tenant_id: str) -> dict | None:
    r = db.query("SELECT * FROM tenants WHERE tenant_id = %s AND status = 'active'", (tenant_id,))
    return r[0] if r else None


def list_locations(db: Db, tenant_id: str) -> list[dict]:
    return db.query("SELECT * FROM locations WHERE tenant_id = %s AND active ORDER BY name", (tenant_id,))


def get_user_by_email(db: Db, email: str) -> dict | None:
    r = db.query("SELECT * FROM users WHERE LOWER(email) = %s AND active", (email.strip().lower(),))
    return r[0] if r else None


# ── Bid board (published) and scraper staging ───────────────────────────────
QUOTE_MAX_AGE_MIN = 60      # a delayed quote older than this is not used to price a bid (the job has stopped)


def _quote_fresh(q: dict, now=None) -> bool:
    from datetime import timezone
    from .auth import _as_utc
    stamp = q.get("fetched_at") or q.get("as_of")
    if stamp is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - _as_utc(stamp)).total_seconds() <= QUOTE_MAX_AGE_MIN * 60


def live_flat(bid: dict, quote: dict | None, now=None) -> tuple[float | None, str]:
    """The flat price a bid is paying right now, and where it came from.

    The sticky part of a bid is its BASIS (it changes only when staff post or confirm a new one). The flat bid is
    futures + basis and moves with every delayed quote:
      ('futures')  fresh quote for the bid's futures month  -> round(futures + basis/100, 4)
      ('stale')    a quote exists but is older than QUOTE_MAX_AGE_MIN -> None: never priced (or hit) off a dead feed
      ('posted')   no futures feed for that month (or a cash-only bid) -> the cash price as posted / scraped
    """
    basis = bid.get("basis_cents")
    if basis is not None and quote and quote.get("price") is not None:
        if _quote_fresh(quote, now):
            return round(float(quote["price"]) + basis / 100.0, 4), "futures"
        return None, "stale"
    return bid.get("cash_price"), "posted"


def list_bids(db: Db, tenant_id: str, grain: str | None = None) -> list[dict]:
    """Live bids, each annotated with flat_price / flat_source (see live_flat) and the quote it used."""
    sql = "SELECT * FROM bids WHERE tenant_id = %s AND active"
    params: list = [tenant_id]
    if grain:
        sql += " AND grain = %s"
        params.append(grain)
    rows = db.query(sql + " ORDER BY grain, delivery_month", tuple(params))
    syms = {b["futures_symbol"] for b in rows if b["futures_symbol"]}
    quotes = ({q["symbol"]: q for q in db.query("SELECT symbol, price, as_of, fetched_at FROM futures_quotes")
               if q["symbol"] in syms} if syms else {})
    for b in rows:
        q = quotes.get(b["futures_symbol"])
        b["flat_price"], b["flat_source"] = live_flat(b, q)
        b["quote_price"] = q["price"] if q else None
        b["quote_as_of"] = (q.get("as_of") or q.get("fetched_at")) if q else None
        b["window_start"], b["window_end"] = bid_window(b)
    return rows


def _require_staff(db: Db, tenant_id: str, actor_id: int) -> None:
    """Bid-board writes: elevator staff of this tenant, or a JSA platform admin."""
    r = db.query("SELECT tenant_id, role FROM users WHERE user_id = %s AND active", (actor_id,))
    ok = r and (r[0]["role"] == "platform_admin"
                or (r[0]["tenant_id"] == tenant_id and r[0]["role"] in ELEVATOR_ROLES))
    if not ok:
        raise PortalError("Elevator staff only.")


def retire_bid(db: Db, tenant_id: str, actor_id: int, bid_id: int) -> None:
    _require_staff(db, tenant_id, actor_id)
    db.execute("UPDATE bids SET active = FALSE WHERE bid_id = %s AND tenant_id = %s", (bid_id, tenant_id))
    _audit(db, tenant_id, actor_id, "bid_retired", dict(bid_id=bid_id))


def _futures_month(grain, symbol) -> str:
    """Every bid and offer names the futures month it is hedged against; PortalError if it is missing or not listed."""
    try:
        return contracts.check_symbol(grain, symbol)
    except ValueError as e:
        raise PortalError(str(e)) from None


def publish_bid(db: Db, tenant_id: str, actor_id: int, *, grain: str, delivery_month: str,
                location_id=None, futures_symbol=None, basis_cents=None, cash_price=None,
                bushel_cap=None, expires_at=None, source_suggestion_id=None,
                delivery_start=None, delivery_end=None) -> int:
    """Publish a bid. It replaces the live bid for the same slot (location, grain, delivery month,
    futures month). Returns a PublishResult: how many outstanding flat-price offers had their implied
    futures re-computed because the basis moved, and (.auto_hit) how many offers it then hit."""
    _require_staff(db, tenant_id, actor_id)
    grain = contracts.canon_grain(grain)
    futures_symbol = _futures_month(grain, futures_symbol)
    if basis_cents is None and cash_price is None:
        raise PortalError("A bid needs a basis or a cash price.")
    mb = month_bounds(delivery_month)
    if mb is None:
        raise PortalError("The delivery month must look like 2026-12.")
    ds, de = _to_date(delivery_start) or mb[0], _to_date(delivery_end) or mb[1]   # no dates given: the whole month
    if de < ds:
        raise PortalError("The delivery range ends before it starts.")
    bid_id = new_id()
    _insert(db, "bids", dict(
        bid_id=bid_id, tenant_id=tenant_id, location_id=location_id, grain=grain, delivery_month=delivery_month,
        delivery_start=ds.isoformat(), delivery_end=de.isoformat(), futures_symbol=futures_symbol, basis_cents=basis_cents, cash_price=cash_price,
        bushel_cap=bushel_cap, effective_date=today_ct().isoformat(), expires_at=expires_at,
        source_suggestion_id=source_suggestion_id, published_by=actor_id))
    new_bid_id = bid_id
    old_ids = [r["bid_id"] for r in db.query(
        "SELECT bid_id FROM bids WHERE tenant_id = %s AND active AND bid_id <> %s AND grain = %s "
        "AND delivery_month = %s AND COALESCE(futures_symbol, '') = %s AND COALESCE(location_id, -1) = %s",
        (tenant_id, new_bid_id, grain, delivery_month, futures_symbol or "", location_id if location_id is not None else -1))]
    for oid in old_ids:
        db.execute("UPDATE bids SET active = FALSE WHERE bid_id = %s AND tenant_id = %s", (oid, tenant_id))
        # Outstanding offers made against the replaced bid follow the new one (any price type), so they
        # keep a live reference for pricing and for the hit check.
        db.execute("UPDATE offers SET bid_id = %s WHERE tenant_id = %s AND status = 'submitted' AND bid_id = %s",
                   (new_bid_id, tenant_id, oid))
    _usage(db, tenant_id, "bid_published")
    _audit(db, tenant_id, actor_id, "bid_published", dict(grain=grain, delivery_month=delivery_month))
    new_bid = db.query("SELECT * FROM bids WHERE bid_id = %s AND tenant_id = %s", (new_bid_id, tenant_id))[0]
    result = PublishResult(_reprice_flat_offers(db, tenant_id, new_bid, old_ids, actor_id))
    result.auto_hit = run_auto_hit(db, tenant_id)          # after re-pricing, so a hit locks the new implied futures
    return result


def stage_suggestion(db: Db, tenant_id: str, *, grain: str, delivery_month: str, source: str,
                     location_id=None, futures_symbol=None, basis_cents=None, cash_price=None,
                     delivery_start=None, delivery_end=None) -> None:
    """Scraper entry point: lands as 'suggested'; farmers never see it. delivery_start/end are None when the source did
    not give a clear delivery range: an admin must then enter one before approving."""
    ds, de = _to_date(delivery_start), _to_date(delivery_end)
    if not (ds and de and de >= ds):
        ds = de = None
    grain = contracts.canon_grain(grain)
    _insert(db, "bid_suggestions", dict(
        tenant_id=tenant_id, location_id=location_id, grain=grain, delivery_month=delivery_month,
        delivery_start=ds.isoformat() if ds else None, delivery_end=de.isoformat() if de else None,
        futures_symbol=futures_symbol, basis_cents=basis_cents, cash_price=cash_price, source=source))


def sync_suggestions(db: Db, tenant_id: str, source: str, items: list[dict]) -> dict:
    """Stage scraped items (see scrapers.py) as suggestions, skipping what needs no review.

    Skipped: delivery months already past, locations the tenant doesn't have, anything
    identical to a live bid or an open suggestion. A suggestion whose values changed
    supersedes the older open one. Returns counts for the run log.
    """
    locs = {l["name"].strip().lower(): l["location_id"] for l in list_locations(db, tenant_id)}
    this_month = today_ct().strftime("%Y-%m")
    live = {(b["location_id"], b["grain"], b["delivery_month"], b["futures_symbol"]):
            (b["basis_cents"], b["cash_price"]) for b in list_bids(db, tenant_id)}
    openq = {(x["location_id"], x["grain"], x["delivery_month"], x["futures_symbol"]): x
             for x in list_suggestions(db, tenant_id)}
    n = dict(staged=0, unchanged=0, unmatched=0, stale=0)
    for it in items:
        lid = locs.get((it["location_name"] or "").strip().lower())
        if lid is None:
            n["unmatched"] += 1
            continue
        if it["delivery_month"] < this_month:
            n["stale"] += 1
            continue
        key = (lid, it["grain"], it["delivery_month"], it["futures_symbol"])
        vals = (it["basis_cents"], it["cash_price"])
        if live.get(key) == vals or (key in openq and
                                     (openq[key]["basis_cents"], openq[key]["cash_price"]) == vals):
            n["unchanged"] += 1
            continue
        if key in openq:
            db.execute("UPDATE bid_suggestions SET status = 'superseded' WHERE suggestion_id = %s "
                       "AND tenant_id = %s", (openq[key]["suggestion_id"], tenant_id))
        stage_suggestion(db, tenant_id, grain=it["grain"], delivery_month=it["delivery_month"],
                         source=source, location_id=lid, futures_symbol=it["futures_symbol"],
                         basis_cents=it["basis_cents"], cash_price=it["cash_price"],
                         delivery_start=it.get("delivery_start"), delivery_end=it.get("delivery_end"))
        n["staged"] += 1
    return n


def list_suggestions(db: Db, tenant_id: str) -> list[dict]:
    return db.query("SELECT * FROM bid_suggestions WHERE tenant_id = %s AND status = 'suggested' "
                    "ORDER BY grain, delivery_month", (tenant_id,))


def review_suggestion(db: Db, tenant_id: str, actor_id: int, suggestion_id: int, approve: bool,
                      **overrides) -> int:
    """Approve (optionally with edited basis/price) -> publishes a bid; or discard.
    Returns how many outstanding flat-price offers the approved basis re-priced (0 for a discard)."""
    _require_staff(db, tenant_id, actor_id)
    r = db.query("SELECT * FROM bid_suggestions WHERE suggestion_id = %s AND tenant_id = %s "
                 "AND status = 'suggested'", (suggestion_id, tenant_id))
    if not r:
        raise PortalError("Suggestion not found for this tenant.")
    s = r[0]
    repriced = 0
    if approve:
        ds = _to_date(overrides.get("delivery_start")) or _to_date(s.get("delivery_start"))
        de = _to_date(overrides.get("delivery_end")) or _to_date(s.get("delivery_end"))
        if not (ds and de):
            raise PortalError("Enter the delivery date range (from and to) before approving: the source did not give a "
                              "clear one, and offers expire when it ends.")
        repriced = publish_bid(db, tenant_id, actor_id, grain=s["grain"], delivery_month=s["delivery_month"],
                    location_id=s["location_id"],
                    futures_symbol=overrides.get("futures_symbol", s["futures_symbol"]),
                    basis_cents=overrides.get("basis_cents", s["basis_cents"]),
                    cash_price=overrides.get("cash_price", s["cash_price"]),
                    source_suggestion_id=suggestion_id, delivery_start=ds, delivery_end=de)
    db.execute("UPDATE bid_suggestions SET status = %s, reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP() "
               "WHERE suggestion_id = %s AND tenant_id = %s",
               ("approved" if approve else "discarded", actor_id, suggestion_id, tenant_id))
    return repriced


# ── Offers ──────────────────────────────────────────────────────────────────
# ── Pricing reference: which posted bid prices an offer, and the implied futures ──────────
def _month_of(d) -> str | None:
    return str(d)[:7] if d else None


def _to_date(v) -> date | None:
    """A date from a date / datetime / 'YYYY-MM-DD...' string (Snowflake returns dates, sqlite text); None if unusable."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def month_bounds(ym: str) -> tuple[date, date] | None:
    """('2026-12') -> (2026-12-01, 2026-12-31); None when it isn't YYYY-MM."""
    try:
        y, m = int(ym[:4]), int(ym[5:7])
        first = date(y, m, 1)
    except (ValueError, TypeError, IndexError):
        return None
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return first, date.fromordinal(nxt.toordinal() - 1)


def bid_window(b: dict) -> tuple[date | None, date | None]:
    """A bid's delivery range: its own dates, else the whole of its delivery month."""
    s, e = _to_date(b.get("delivery_start")), _to_date(b.get("delivery_end"))
    if s and e:
        return s, e
    mb = month_bounds(b.get("delivery_month") or "")
    return mb if mb else (s, e)


def offer_delivery_end(o: dict) -> date | None:
    """The last day of the offer's delivery period (its offer expires the day after): the offer's own delivery_end,
    else the end of the bid it was made against (as it was at submission), else the end of its delivery_start month.
    None when nothing says."""
    d = _to_date(o.get("delivery_end"))
    if d:
        return d
    snap = o.get("bid_snapshot")
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except ValueError:
            snap = None
    if snap:
        d = _to_date(snap.get("delivery_end"))
        if d:
            return d
        mb = month_bounds(snap.get("delivery_month") or "")
        if mb:
            return mb[1]
    start = _to_date(o.get("delivery_start"))
    return month_bounds(start.strftime("%Y-%m"))[1] if start else None


def _reference_bid(offer: dict, bids: list[dict]) -> dict | None:
    """The live bid whose basis prices this offer: the bid it was made against, else the one live
    bid with the same commodity, futures month and delivery month (and location, when the offer
    names one). None when there isn't exactly one candidate."""
    if offer.get("bid_id") is not None:
        for b in bids:
            if b["bid_id"] == offer["bid_id"]:
                return b
    month = _month_of(offer.get("delivery_start"))
    if not month:
        return None
    cands = [b for b in bids if b["grain"] == offer["grain"] and b["futures_symbol"] == offer.get("futures_symbol")
             and b["delivery_month"] == month
             and (offer.get("location_id") is None or b["location_id"] == offer["location_id"])]
    return cands[0] if len(cands) == 1 else None


def implied_futures(offer: dict, basis_cents) -> float | None:
    """Flat price less basis = the futures level the elevator needs. Futures-only offers ARE a futures
    price. Basis (HTA) offers have no price yet, so no implied futures."""
    if offer["price_type"] != "FIXED" or offer.get("price") is None:
        return None
    if offer["contract_type"] == "FUTURES_ONLY":
        return round(float(offer["price"]), 4)
    if basis_cents is None:
        return None
    return round(float(offer["price"]) - basis_cents / 100.0, 4)


def _reprice_flat_offers(db: Db, tenant_id: str, bid: dict, old_ids: list, actor_id: int) -> int:
    """A bid's basis changed: every OUTSTANDING flat-price offer priced off it gets its implied futures
    recalculated (the farmer's flat price does not move). Hit offers are locked at the basis they were
    hit on. Each change is written to the offer's history."""
    if bid["basis_cents"] is None:
        return 0
    n = 0
    rows = db.query("SELECT * FROM offers WHERE tenant_id = %s AND status = 'submitted' AND price_type = 'FIXED' "
                    "AND contract_type <> 'FUTURES_ONLY'", (tenant_id,))
    for o in rows:
        linked = o["bid_id"] is not None and (o["bid_id"] in old_ids or o["bid_id"] == bid["bid_id"])
        if not linked and _reference_bid(o, [bid]) is None:
            continue
        new_imp = implied_futures(o, bid["basis_cents"])
        if (o["basis_ref_cents"], o["implied_futures"]) == (bid["basis_cents"], new_imp) and (
                not linked or o["bid_id"] == bid["bid_id"]):
            continue
        db.execute("UPDATE offers SET basis_ref_cents = %s, implied_futures = %s, bid_id = %s, "
                   "updated_at = CURRENT_TIMESTAMP() WHERE offer_id = %s AND tenant_id = %s",
                   (bid["basis_cents"], new_imp, bid["bid_id"] if linked else o["bid_id"], o["offer_id"], tenant_id))
        if (o["basis_ref_cents"], o["implied_futures"]) != (bid["basis_cents"], new_imp):
            _insert(db, "offer_events", dict(
                tenant_id=tenant_id, offer_id=o["offer_id"], actor_user_id=actor_id, event_type="basis_updated",
                detail=dict(basis_before=o["basis_ref_cents"], basis_after=bid["basis_cents"],
                            implied_futures_before=o["implied_futures"], implied_futures_after=new_imp,
                            flat_price=o["price"])), ("detail",))
            n += 1
    if n:
        _audit(db, tenant_id, actor_id, "flat_offers_repriced", dict(count=n, bid_id=bid["bid_id"],
                                                                      basis=bid["basis_cents"]))
    return n


def bid_reached(offer: dict, bid: dict | None) -> bool:
    """Has the live bid come up to this outstanding offer? (A prompt for staff to mark it hit.)"""
    if bid is None or offer["status"] != "submitted":
        return False
    if offer["price_type"] == "FIXED":
        flat = bid["flat_price"] if "flat_price" in bid else bid.get("cash_price")      # live: futures + basis
        return flat is not None and offer["price"] is not None and flat >= offer["price"]
    return bid["basis_cents"] is not None and offer["basis_cents"] is not None and bid["basis_cents"] >= offer["basis_cents"]


def delivery_period(offer: dict) -> str:
    """'YYYY-MM' of the offer's delivery start, else of the bid it was made against, else 'Unscheduled'."""
    if offer.get("delivery_start"):
        return _month_of(offer["delivery_start"])
    snap = offer.get("bid_snapshot")
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except ValueError:
            snap = None
    return (snap or {}).get("delivery_month") or "Unscheduled"


def _clean_details(note, contract_ref):
    note = (note or "").strip() or None
    ref = (contract_ref or "").strip() or None
    if note and len(note) > 500:
        raise PortalError("Notes are limited to 500 characters.")
    if ref and len(ref) > 60:
        raise PortalError("Contract reference is limited to 60 characters.")
    return note, ref


def update_offer_details(db: Db, tenant_id: str, staff_id: int, offer_id: int, *, note, contract_ref) -> None:
    """Staff edit an offer's note and contract reference at any status (the contract number is
    usually assigned once the offer is accepted). Terms and status are not touched; the change is
    written to the offer history."""
    _require_staff(db, tenant_id, staff_id)
    note, contract_ref = _clean_details(note, contract_ref)
    r = db.query("SELECT note, contract_ref, status FROM offers WHERE offer_id = %s AND tenant_id = %s",
                 (offer_id, tenant_id))
    if not r:
        raise PortalError("Offer not found for this tenant.")
    if r[0]["status"] == "completed" and not contract_ref:
        raise PortalError("A completed offer must keep its contract number.")
    db.execute("UPDATE offers SET note = %s, contract_ref = %s, updated_at = CURRENT_TIMESTAMP() "
               "WHERE offer_id = %s AND tenant_id = %s", (note, contract_ref, offer_id, tenant_id))
    _insert(db, "offer_events", dict(
        tenant_id=tenant_id, offer_id=offer_id, actor_user_id=staff_id, event_type="details_updated",
        detail=dict(note_before=r[0]["note"], note_after=note, contract_ref_before=r[0]["contract_ref"],
                    contract_ref_after=contract_ref)), ("detail",))


def ensure_farmer_for_user(db: Db, tenant_id: str, user: dict) -> int:
    """The farmer record behind a portal login (created on first use)."""
    r = db.query("SELECT farmer_id FROM farmers WHERE tenant_id = %s AND user_id = %s",
                 (tenant_id, user["user_id"]))
    if r:
        return r[0]["farmer_id"]
    who = user.get("name") or user["email"]
    fid = new_id()
    _insert(db, "farmers", dict(farmer_id=fid, tenant_id=tenant_id, name=who, individual_name=who,
                                email=user["email"], user_id=user["user_id"], created_by=user["user_id"]))
    return fid


def _new_offer(db: Db, tenant_id: str, farmer_id: int, farmer_user_id, placed_by, actor_id: int, *,
               grain: str, contract_type: str, price_type: str, bushels: int, price=None,
               basis_cents=None, futures_symbol=None, bid_id=None, location_id=None,
               delivery_start=None, delivery_end=None, note=None, contract_ref=None,
               expires_at=None) -> int:
    if contract_type not in CONTRACT_TYPES:
        raise PortalError(f"contract_type must be one of {CONTRACT_TYPES}")
    if price_type not in PRICE_TYPES:
        raise PortalError(f"price_type must be one of {PRICE_TYPES}")
    if bushels <= 0:
        raise PortalError("Bushels must be positive.")
    if price_type == "FIXED" and price is None:
        raise PortalError("A fixed-price offer needs a price.")
    grain = contracts.canon_grain(grain)
    futures_symbol = _futures_month(grain, futures_symbol)
    if price_type == "BASIS" and basis_cents is None:
        raise PortalError("A basis offer needs a basis and a futures symbol.")
    note, contract_ref = _clean_details(note, contract_ref)
    snapshot = None
    if bid_id is not None:
        b = db.query("SELECT * FROM bids WHERE bid_id = %s AND tenant_id = %s AND active", (bid_id, tenant_id))
        if not b:
            raise PortalError("That bid is not on this tenant's board.")
        snapshot = b[0]
    else:
        t = get_tenant(db, tenant_id)
        if not t or not t["open_pricing"]:
            raise PortalError("This elevator only accepts offers against posted bids.")
    end = offer_delivery_end(dict(delivery_end=delivery_end, delivery_start=delivery_start, bid_snapshot=snapshot))
    if end is not None and end < today_ct():
        raise PortalError("That delivery period has already ended.")
    ref = _reference_bid(dict(bid_id=bid_id, grain=grain, futures_symbol=futures_symbol, location_id=location_id,
                              delivery_start=delivery_start), list_bids(db, tenant_id))
    ref_basis = ref["basis_cents"] if (ref and price_type == "FIXED") else None
    imp = implied_futures(dict(price_type=price_type, price=price, contract_type=contract_type), ref_basis)
    oid = new_id()
    _insert(db, "offers", dict(
        offer_id=oid, basis_ref_cents=ref_basis, implied_futures=imp,
        tenant_id=tenant_id, farmer_id=farmer_id, farmer_user_id=farmer_user_id, placed_by=placed_by,
        location_id=location_id, bid_id=bid_id, grain=grain, contract_type=contract_type,
        price_type=price_type, bushels=bushels, price=price, basis_cents=basis_cents,
        futures_symbol=futures_symbol, delivery_start=delivery_start, delivery_end=delivery_end,
        note=note, contract_ref=contract_ref, bid_snapshot=snapshot or {}, status="submitted",
        expires_at=expires_at),
        ("bid_snapshot",))
    _insert(db, "offer_events", dict(
        tenant_id=tenant_id, offer_id=oid, actor_user_id=actor_id, event_type="submitted",
        detail=dict(bushels=bushels, price=price, basis_cents=basis_cents,
                    on_behalf=placed_by is not None)), ("detail",))
    _usage(db, tenant_id, "offer_submitted", oid)
    _notify(db, tenant_id, "offer_submitted", oid)
    _auto_hit_one(db, tenant_id, db.query("SELECT * FROM offers WHERE offer_id = %s AND tenant_id = %s",
                                          (oid, tenant_id))[0], list_bids(db, tenant_id), _auto_rules(db, tenant_id))
    return oid


def create_offer(db: Db, tenant_id: str, farmer: dict, **terms) -> int:
    """A farmer submits their own offer through the portal."""
    if farmer["tenant_id"] != tenant_id or farmer["role"] != "farmer":
        raise PortalError("Only a farmer of this tenant can submit offers.")
    fid = ensure_farmer_for_user(db, tenant_id, farmer)
    return _new_offer(db, tenant_id, fid, farmer["user_id"], None, farmer["user_id"], **terms)


def place_offer_for_farmer(db: Db, tenant_id: str, staff_id: int, farmer_id: int, **terms) -> int:
    """Elevator staff enter an offer on a farmer's behalf (phone, in person). Recorded as placed_by."""
    _require_staff(db, tenant_id, staff_id)
    f = db.query("SELECT * FROM farmers WHERE farmer_id = %s AND tenant_id = %s AND active", (farmer_id, tenant_id))
    if not f:
        raise PortalError("Farmer not found for this tenant.")
    return _new_offer(db, tenant_id, farmer_id, f[0]["user_id"], staff_id, staff_id, **terms)


def list_offers(db: Db, tenant_id: str, user: dict, status: str | None = None) -> list[dict]:
    """Farmers see only their own offers (incl. ones staff placed for them); staff see the tenant."""
    sql = ("SELECT o.*, f.name AS farmer_name, f.email AS farmer_email, f.phone AS farmer_phone, "
           "f.client_id AS farmer_client_id, f.entity_name AS farmer_entity, "
           "f.individual_name AS farmer_individual, "
           "pb.name AS placed_by_name, pb.email AS placed_by_email, "
           "ab.name AS acknowledged_by_name, ab.email AS acknowledged_by_email, "
           "cb.name AS cancelled_by_name, cb.email AS cancelled_by_email FROM offers o "
           "LEFT JOIN farmers f ON f.farmer_id = o.farmer_id "
           "LEFT JOIN users pb ON pb.user_id = o.placed_by "
           "LEFT JOIN users ab ON ab.user_id = o.acknowledged_by "
           "LEFT JOIN users cb ON cb.user_id = o.cancelled_by WHERE o.tenant_id = %s")
    params: list = [tenant_id]
    if user["role"] == "farmer":
        sql += " AND f.user_id = %s"
        params.append(user["user_id"])
    if status:
        sql += " AND o.status = %s"
        params.append(status)
    rows = db.query(sql + " ORDER BY o.created_at DESC", tuple(params))
    bids = list_bids(db, tenant_id)
    for o in rows:
        o["delivery_period"] = delivery_period(o)
        o["bid_reached"] = bid_reached(o, _reference_bid(o, bids)) if o["status"] == "submitted" else False
    return rows


def bid_gap(offer: dict) -> str:
    """How the offer compares with the bid it was made against ('' for open-priced offers)."""
    snap = offer.get("bid_snapshot")
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except ValueError:
            snap = None
    if not snap:
        return ""
    if offer["price_type"] == "BASIS" and snap.get("basis_cents") is not None and offer.get("basis_cents") is not None:
        d = offer["basis_cents"] - snap["basis_cents"]
        return f"{d:+d}¢ vs posted basis"
    if offer["price_type"] == "FIXED" and snap.get("cash_price") is not None and offer.get("price") is not None:
        d = offer["price"] - snap["cash_price"]
        return f"{d:+.2f} $/bu vs posted cash"
    return ""


def expire_stale(db: Db, tenant_id: str) -> int:
    """Expire outstanding offers that are past their good-until time, or whose delivery period is over (the last
    delivery day has passed: the offer lives through that day and expires the next). Cheap; runs on page loads and
    from the jobs. Hit offers are never expired: only outstanding ones."""
    from datetime import timezone
    from .auth import _as_utc
    now, today, n = datetime.now(timezone.utc), today_ct(), 0
    for o in db.query("SELECT offer_id, expires_at, delivery_start, delivery_end, bid_snapshot FROM offers "
                      "WHERE tenant_id = %s AND status = 'submitted'", (tenant_id,)):
        end = offer_delivery_end(o)
        if o["expires_at"] is not None and _as_utc(o["expires_at"]) <= now:
            reason = "good_until"
        elif end is not None and end < today:
            reason = "delivery_period_ended"
        else:
            continue
        db.execute("UPDATE offers SET status = 'expired', updated_at = CURRENT_TIMESTAMP() "
                   "WHERE offer_id = %s AND tenant_id = %s", (o["offer_id"], tenant_id))
        _insert(db, "offer_events", dict(tenant_id=tenant_id, offer_id=o["offer_id"], actor_user_id=None,
                                         event_type="expired", detail=dict(
                                             reason=reason, delivery_end=str(end) if end else None)), ("detail",))
        _notify(db, tenant_id, "offer_expired", o["offer_id"])
        n += 1
    return n


def expire_all_tenants(db: Db) -> int:
    """The job form of expire_stale: every active tenant."""
    return sum(expire_stale(db, t["tenant_id"]) for t in
               db.query("SELECT tenant_id FROM tenants WHERE status = 'active' ORDER BY tenant_id"))


def _hit_context(bid: dict | None) -> dict:
    """The one price snapshot worth keeping: what the market was doing at the moment the offer was hit.
    (Price moves that hit nothing are never stored: one latest quote per futures month, flat bids computed on read.)"""
    if not bid:
        return {}
    return dict(bid_id=bid["bid_id"], basis_cents=bid["basis_cents"], flat_price=bid.get("flat_price"),
                flat_source=bid.get("flat_source"), futures_quote=bid.get("quote_price"),
                quote_as_of=bid.get("quote_as_of"), posted_cash_price=bid["cash_price"])


def _mark_hit(db: Db, tenant_id: str, offer_id: int, *, actor_id=None, rule: dict | None = None,
              bid: dict | None = None) -> None:
    """The one place an offer becomes hit: staff clicking Mark hit, or an auto-hit rule (rule given,
    actor None). Writes the status, the history event, the confirmation, and the billing usage. Either way the
    live price context at the moment of the hit is saved with the event and the confirmation."""
    auto = rule is not None
    cur = db.query("SELECT * FROM offers WHERE offer_id = %s AND tenant_id = %s", (offer_id, tenant_id))[0]
    if bid is None:                                      # a manual hit: look up the bid that prices this offer
        bid = _reference_bid(cur, list_bids(db, tenant_id))
    ctx = _hit_context(bid)
    db.execute("UPDATE offers SET status = 'hit', hit_at = CURRENT_TIMESTAMP(), hit_by = %s, auto_hit = %s, "
               "auto_hit_rule_id = %s, updated_at = CURRENT_TIMESTAMP() "
               "WHERE offer_id = %s AND tenant_id = %s AND status = 'submitted'",
               (actor_id, auto, rule["rule_id"] if auto else None, offer_id, tenant_id))
    detail = {"on_behalf": False, "hit_context": ctx}
    if auto:
        detail.update(auto_hit=True, rule_id=rule["rule_id"], bid_id=bid["bid_id"],
                      bid_basis_cents=bid["basis_cents"], bid_cash_price=bid["cash_price"],
                      bid_flat_price=bid.get("flat_price"), flat_source=bid.get("flat_source"),
                      futures_quote=bid.get("quote_price"))        # the live flat bid the offer was compared with
    _insert(db, "offer_events", dict(tenant_id=tenant_id, offer_id=offer_id, actor_user_id=actor_id,
                                     event_type="hit", detail=detail), ("detail",))
    cur = db.query("SELECT * FROM offers WHERE offer_id = %s AND tenant_id = %s", (offer_id, tenant_id))[0]
    _insert(db, "confirmations", dict(tenant_id=tenant_id, offer_id=offer_id, terms={**cur, "hit_context": ctx},
                                      hit_by=actor_id, auto_hit=auto), ("terms",))
    _usage(db, tenant_id, "offer_hit", offer_id)
    _notify(db, tenant_id, "offer_hit", offer_id, auto=auto)


# ── Auto-hit rules ─────────────────────────────────────────────────────────────
def _auto_rules(db: Db, tenant_id: str) -> list[dict]:
    return db.query("SELECT * FROM auto_hit_rules WHERE tenant_id = %s AND active ORDER BY rule_id", (tenant_id,))


def _matching_rule(offer: dict, bid: dict, rules: list[dict]) -> dict | None:
    loc = offer["location_id"] if offer.get("location_id") is not None else bid.get("location_id")
    for r in rules:
        if r["location_id"] is not None and r["location_id"] != loc:
            continue
        if r["grain"] and r["grain"] != offer["grain"]:
            continue
        if r["contract_type"] and r["contract_type"] != offer["contract_type"]:
            continue
        if r["max_bushels"] is not None and offer["bushels"] > r["max_bushels"]:
            continue
        return r
    return None


def _auto_hit_one(db: Db, tenant_id: str, offer: dict, bids: list[dict], rules: list[dict]) -> bool:
    """Hit this offer if it is outstanding, the live bid has reached it, and an active rule allows it."""
    if not rules or offer["status"] != "submitted" or offer["contract_type"] == "FUTURES_ONLY":
        return False
    bid = _reference_bid(offer, bids)
    if not bid_reached(offer, bid):
        return False
    rule = _matching_rule(offer, bid, rules)
    if rule is None:
        return False
    _mark_hit(db, tenant_id, offer["offer_id"], rule=rule, bid=bid)
    return True


def run_auto_hit(db: Db, tenant_id: str) -> int:
    """Check every outstanding offer against the live bids and the tenant's active auto-hit rules.
    Safe to call any time (idempotent). Runs when a bid is posted, an offer is entered, a rule is
    added or switched on, and from the admin's 'check now' button. Returns how many offers it hit."""
    expire_stale(db, tenant_id)                                  # a lapsed offer is expired, never hit
    rules = _auto_rules(db, tenant_id)
    if not rules:
        return 0
    bids = list_bids(db, tenant_id)
    return sum(_auto_hit_one(db, tenant_id, o, bids, rules)
               for o in db.query("SELECT * FROM offers WHERE tenant_id = %s AND status = 'submitted'", (tenant_id,)))


def list_auto_hit_rules(db: Db, tenant_id: str, admin_id: int) -> list[dict]:
    _require_admin(db, tenant_id, admin_id)
    return db.query("SELECT * FROM auto_hit_rules WHERE tenant_id = %s ORDER BY rule_id", (tenant_id,))


def add_auto_hit_rule(db: Db, tenant_id: str, admin_id: int, *, location_id=None, grain=None,
                      contract_type=None, max_bushels=None) -> int:
    """Admin adds a rule and it is applied at once. Returns how many outstanding offers it hit."""
    _require_admin(db, tenant_id, admin_id)
    if grain and grain not in CONTRACT_BUSHELS:
        raise PortalError("Unknown commodity.")
    if contract_type and contract_type not in ("CASH", "HTA"):
        raise PortalError("Auto-hit applies to cash and HTA offers (futures-only offers have no bid to reach).")
    if location_id is not None and not db.query(
            "SELECT 1 AS x FROM locations WHERE location_id = %s AND tenant_id = %s", (location_id, tenant_id)):
        raise PortalError("Unknown location.")
    if max_bushels is not None and max_bushels <= 0:
        raise PortalError("The bushel limit must be positive (leave it blank for no limit).")
    _insert(db, "auto_hit_rules", dict(tenant_id=tenant_id, location_id=location_id, grain=grain or None,
                                       contract_type=contract_type or None, max_bushels=max_bushels,
                                       created_by=admin_id))
    _audit(db, tenant_id, admin_id, "auto_hit_rule_added", dict(location_id=location_id, grain=grain,
                                                                contract_type=contract_type, max_bushels=max_bushels))
    return run_auto_hit(db, tenant_id)


def set_auto_hit_rule_active(db: Db, tenant_id: str, admin_id: int, rule_id: int, active: bool) -> int:
    _require_admin(db, tenant_id, admin_id)
    if not db.query("SELECT 1 AS x FROM auto_hit_rules WHERE rule_id = %s AND tenant_id = %s", (rule_id, tenant_id)):
        raise PortalError("Rule not found for this tenant.")
    db.execute("UPDATE auto_hit_rules SET active = %s WHERE rule_id = %s AND tenant_id = %s",
               (active, rule_id, tenant_id))
    _audit(db, tenant_id, admin_id, "auto_hit_rule_on" if active else "auto_hit_rule_off", dict(rule_id=rule_id))
    return run_auto_hit(db, tenant_id) if active else 0


def check_auto_hit_now(db: Db, tenant_id: str, admin_id: int) -> int:
    _require_admin(db, tenant_id, admin_id)
    return run_auto_hit(db, tenant_id)


def list_auto_hits(db: Db, tenant_id: str, admin_id: int, limit: int = 25) -> list[dict]:
    """Recent offers hit automatically, newest first."""
    _require_admin(db, tenant_id, admin_id)
    viewer = {"role": "elevator_admin", "user_id": admin_id, "tenant_id": tenant_id}
    rows = [o for o in list_offers(db, tenant_id, viewer) if o["auto_hit"]]
    rows.sort(key=lambda o: str(o["hit_at"]), reverse=True)
    return rows[:limit]


def act_on_offer(db: Db, tenant_id: str, user: dict, offer_id: int, action: str,
                 on_behalf: bool = False) -> str:
    """hit | decline | withdraw. Returns the new status. There are no counter-offers.

    on_behalf=True: elevator staff record a farmer's withdrawal (phone call, in person). The
    event log keeps the staff user as actor and flags it.
    """
    if user["tenant_id"] != tenant_id:
        raise PortalError("Wrong tenant.")
    r = db.query("SELECT * FROM offers WHERE offer_id = %s AND tenant_id = %s", (offer_id, tenant_id))
    if not r:
        raise PortalError("Offer not found for this tenant.")
    o = r[0]
    if user["role"] == "farmer":
        mine = db.query("SELECT 1 AS x FROM farmers WHERE farmer_id = %s AND tenant_id = %s AND user_id = %s",
                        (o["farmer_id"], tenant_id, user["user_id"]))
        if on_behalf or not mine:
            raise PortalError("Not your offer.")
    if on_behalf and user["role"] not in ELEVATOR_ROLES:
        raise PortalError("Only elevator staff can respond for a farmer.")
    allowed = _TRANSITIONS.get(o["status"], {}).get(action)
    if not allowed:
        raise PortalError(f"Cannot {action} an offer that is {o['status']}.")
    new_status, roles = allowed
    acting_role = "farmer" if on_behalf else user["role"]
    if acting_role not in roles:
        raise PortalError(f"A {acting_role} cannot {action} this offer.")
    if new_status == "hit":
        _mark_hit(db, tenant_id, offer_id, actor_id=user["user_id"])
        return new_status
    db.execute("UPDATE offers SET status = %s, updated_at = CURRENT_TIMESTAMP() "
               "WHERE offer_id = %s AND tenant_id = %s", (new_status, offer_id, tenant_id))
    _insert(db, "offer_events", dict(tenant_id=tenant_id, offer_id=offer_id, actor_user_id=user["user_id"],
                                     event_type=new_status, detail={"on_behalf": on_behalf}),
            ("detail",))
    _notify(db, tenant_id, "offer_" + new_status, offer_id, on_behalf=on_behalf)
    return new_status


# ── Platform admin (JSA) ────────────────────────────────────────────────────
def usage_by_tenant(db: Db, actor: dict, month: str) -> list[dict]:
    """month = 'YYYY-MM'. Platform admin only — drives per-tenant billing."""
    if actor["role"] != "platform_admin":
        raise PortalError("Platform admin only.")
    return db.query("SELECT tenant_id, event_type, SUM(quantity) AS qty FROM usage_events "
                    "WHERE SUBSTR(CAST(created_at AS VARCHAR), 1, 7) = %s "
                    "GROUP BY tenant_id, event_type ORDER BY tenant_id, event_type", (month,))


def create_tenant(db: Db, actor: dict, tenant_id: str, name: str, **fields) -> None:
    if actor["role"] != "platform_admin":
        raise PortalError("Platform admin only.")
    _insert(db, "tenants", dict(tenant_id=tenant_id, name=name, **fields))
    _audit(db, tenant_id, actor["user_id"], "tenant_created", dict(name=name))


# ── Hit -> completed (archive), hedging, summary ─────────────────────────────
def _require_admin(db: Db, tenant_id: str, actor_id: int) -> None:
    r = db.query("SELECT tenant_id, role FROM users WHERE user_id = %s AND active", (actor_id,))
    if not r or not (r[0]["role"] == "platform_admin"
                     or (r[0]["tenant_id"] == tenant_id and r[0]["role"] == "elevator_admin")):
        raise PortalError("Elevator admin only.")


def acknowledge_offer(db: Db, tenant_id: str, staff_id: int, offer_id: int) -> None:
    """A staff member takes a hit offer on (who and when are recorded). Next step: its contract number."""
    _require_staff(db, tenant_id, staff_id)
    r = db.query("SELECT status FROM offers WHERE offer_id = %s AND tenant_id = %s", (offer_id, tenant_id))
    if not r:
        raise PortalError("Offer not found for this tenant.")
    if r[0]["status"] != "hit":
        raise PortalError("Only a hit offer can be acknowledged.")
    db.execute("UPDATE offers SET status = 'acknowledged', acknowledged_at = CURRENT_TIMESTAMP(), acknowledged_by = %s, "
               "updated_at = CURRENT_TIMESTAMP() WHERE offer_id = %s AND tenant_id = %s", (staff_id, offer_id, tenant_id))
    _insert(db, "offer_events", dict(tenant_id=tenant_id, offer_id=offer_id, actor_user_id=staff_id,
                                     event_type="acknowledged", detail={}), ("detail",))
    _notify(db, tenant_id, "offer_acknowledged", offer_id)


def complete_offer(db: Db, tenant_id: str, staff_id: int, offer_id: int, contract_ref: str) -> None:
    """Complete an ACKNOWLEDGED offer. A contract number is mandatory; the offer then lives in the archive."""
    _require_staff(db, tenant_id, staff_id)
    _, ref = _clean_details(None, contract_ref)
    if not ref:
        raise PortalError("Enter the contract number to complete this offer.")
    r = db.query("SELECT status FROM offers WHERE offer_id = %s AND tenant_id = %s", (offer_id, tenant_id))
    if not r:
        raise PortalError("Offer not found for this tenant.")
    if r[0]["status"] == "hit":
        raise PortalError("Acknowledge this offer first, then enter its contract number to complete it.")
    if r[0]["status"] != "acknowledged":
        raise PortalError("Only an acknowledged offer can be completed.")
    db.execute("UPDATE offers SET status = 'completed', contract_ref = %s, completed_at = CURRENT_TIMESTAMP(), "
               "completed_by = %s, updated_at = CURRENT_TIMESTAMP() WHERE offer_id = %s AND tenant_id = %s",
               (ref, staff_id, offer_id, tenant_id))
    _insert(db, "offer_events", dict(tenant_id=tenant_id, offer_id=offer_id, actor_user_id=staff_id,
                                     event_type="completed", detail=dict(contract_ref=ref)), ("detail",))
    _usage(db, tenant_id, "offer_completed", offer_id)
    _notify(db, tenant_id, "offer_completed", offer_id)


def cancel_offer(db: Db, tenant_id: str, actor_id: int, offer_id: int, reason: str) -> None:
    """Amend an offer to CANCELLED after it was hit. Staff can cancel a hit or acknowledged offer; a completed one
    needs an admin. A reason is required. Admins are notified and the offer sits on the Hedge page's
    'adjust hedges' list until an admin confirms the hedge was adjusted."""
    _require_staff(db, tenant_id, actor_id)
    reason = (reason or "").strip()
    if not reason:
        raise PortalError("Enter a reason for cancelling.")
    if len(reason) > 200:
        raise PortalError("The reason is limited to 200 characters.")
    r = db.query("SELECT status, hedged_at FROM offers WHERE offer_id = %s AND tenant_id = %s", (offer_id, tenant_id))
    if not r:
        raise PortalError("Offer not found for this tenant.")
    status = r[0]["status"]
    if status not in ("hit", "acknowledged", "completed"):
        raise PortalError("Only an offer that was hit can be cancelled (use decline / withdraw before that).")
    if status == "completed":
        _require_admin(db, tenant_id, actor_id)
    db.execute("UPDATE offers SET status = 'cancelled', cancelled_at = CURRENT_TIMESTAMP(), cancelled_by = %s, "
               "cancel_reason = %s, hedge_adjust_needed = TRUE, updated_at = CURRENT_TIMESTAMP() "
               "WHERE offer_id = %s AND tenant_id = %s", (actor_id, reason, offer_id, tenant_id))
    _insert(db, "offer_events", dict(tenant_id=tenant_id, offer_id=offer_id, actor_user_id=actor_id,
                                     event_type="cancelled", detail=dict(reason=reason, from_status=status,
                                                                        was_hedged=r[0]["hedged_at"] is not None)), ("detail",))
    _notify(db, tenant_id, "offer_cancelled", offer_id)


def hedge_adjustments(db: Db, tenant_id: str, admin_id: int) -> list[dict]:
    """Cancelled-after-hit offers whose hedge an admin has not yet confirmed as adjusted, newest first."""
    _require_admin(db, tenant_id, admin_id)
    viewer = {"role": "elevator_admin", "user_id": admin_id, "tenant_id": tenant_id}
    rows = [o for o in list_offers(db, tenant_id, viewer) if o["status"] == "cancelled"
            and o["hedge_adjust_needed"] and o["hedge_adjusted_at"] is None]
    for o in rows:
        o["contracts"] = round(o["bushels"] / CONTRACT_BUSHELS.get(o["grain"], 5000), 2)
        o["was_hedged"] = o["hedged_at"] is not None
    rows.sort(key=lambda o: str(o["cancelled_at"]), reverse=True)
    return rows


def mark_hedge_adjusted(db: Db, tenant_id: str, admin_id: int, offer_ids: list[int]) -> int:
    """Admin confirms the hedge for these cancelled offers has been adjusted (or that none was placed)."""
    _require_admin(db, tenant_id, admin_id)
    n = 0
    for oid in offer_ids:
        r = db.query("SELECT status, hedge_adjust_needed, hedge_adjusted_at FROM offers WHERE offer_id = %s "
                     "AND tenant_id = %s", (oid, tenant_id))
        if not r or r[0]["status"] != "cancelled" or not r[0]["hedge_adjust_needed"] or r[0]["hedge_adjusted_at"] is not None:
            continue
        db.execute("UPDATE offers SET hedge_adjusted_at = CURRENT_TIMESTAMP(), hedge_adjusted_by = %s "
                   "WHERE offer_id = %s AND tenant_id = %s", (admin_id, oid, tenant_id))
        _insert(db, "offer_events", dict(tenant_id=tenant_id, offer_id=oid, actor_user_id=admin_id,
                                         event_type="hedge_adjusted", detail={}), ("detail",))
        n += 1
    return n


def mark_hedged(db: Db, tenant_id: str, admin_id: int, offer_ids: list[int]) -> int:
    """Admin records that these hit offers have been hedged (for elevators that hedge by hand)."""
    _require_admin(db, tenant_id, admin_id)
    n = 0
    for oid in offer_ids:
        r = db.query("SELECT status, hedged_at FROM offers WHERE offer_id = %s AND tenant_id = %s", (oid, tenant_id))
        if not r or r[0]["status"] not in HELD or r[0]["hedged_at"] is not None:
            continue
        db.execute("UPDATE offers SET hedged_at = CURRENT_TIMESTAMP(), hedged_by = %s WHERE offer_id = %s "
                   "AND tenant_id = %s", (admin_id, oid, tenant_id))
        _insert(db, "offer_events", dict(tenant_id=tenant_id, offer_id=oid, actor_user_id=admin_id,
                                         event_type="hedged", detail={}), ("detail",))
        n += 1
    return n


def _contains(hay, needle) -> bool:
    return not needle or needle.strip().lower() in (hay or "").lower()


def offer_summary(db: Db, tenant_id: str, staff_id: int, *, grains=None, periods=None, entity="",
                  individual="", statuses=("submitted", "hit", "acknowledged")) -> dict:
    """Offers grouped by commodity and delivery period, after filtering. Staff only.
    Returns {"groups": [...], "offers": [...]} — groups carry counts, bushels and weighted averages."""
    _require_staff(db, tenant_id, staff_id)
    viewer = {"role": "elevator_admin", "user_id": staff_id, "tenant_id": tenant_id}
    rows = [o for o in list_offers(db, tenant_id, viewer) if o["status"] in statuses
            and (not grains or o["grain"] in grains) and (not periods or o["delivery_period"] in periods)
            and _contains(o["farmer_entity"], entity) and _contains(o["farmer_individual"], individual)]
    groups: dict = {}
    for o in rows:
        g = groups.setdefault((o["grain"], o["delivery_period"]), dict(
            grain=o["grain"], period=o["delivery_period"], offers=0, bushels=0, flat_bu=0, flat_sum=0.0,
            imp_bu=0, imp_sum=0.0, basis_bu=0, basis_sum=0.0))
        g["offers"] += 1
        g["bushels"] += o["bushels"]
        if o["price_type"] == "FIXED" and o["price"] is not None:
            g["flat_bu"] += o["bushels"]
            g["flat_sum"] += o["bushels"] * o["price"]
            if o["implied_futures"] is not None:
                g["imp_bu"] += o["bushels"]
                g["imp_sum"] += o["bushels"] * o["implied_futures"]
        elif o["price_type"] == "BASIS" and o["basis_cents"] is not None:
            g["basis_bu"] += o["bushels"]
            g["basis_sum"] += o["bushels"] * o["basis_cents"]
    out = []
    for g in sorted(groups.values(), key=lambda g: (g["grain"], g["period"])):
        out.append(dict(grain=g["grain"], period=g["period"], offers=g["offers"], bushels=g["bushels"],
                        avg_flat_price=round(g["flat_sum"] / g["flat_bu"], 4) if g["flat_bu"] else None,
                        avg_implied_futures=round(g["imp_sum"] / g["imp_bu"], 4) if g["imp_bu"] else None,
                        avg_basis_cents=round(g["basis_sum"] / g["basis_bu"], 1) if g["basis_bu"] else None))
    return dict(groups=out, offers=rows)


def _grain_rank(grain) -> int:
    g = contracts.canon_grain(grain)
    return contracts.GRAINS.index(g) if g in contracts.GRAINS else len(contracts.GRAINS)


def hedge_summary(db: Db, tenant_id: str, admin_id: int) -> dict:
    """What the elevator has to (or may have to) hedge. Admin only.

    hit or acknowledged, not yet hedged -> "now": the elevator owns the grain.
    adjustments          -> cancelled-after-hit offers whose hedge an admin still has to adjust.
    outstanding offers   -> "potential": what a hit would add.
    Rows are grouped by commodity, futures month and price level: the implied futures price for
    flat-price offers, the futures price for futures-only offers, the basis for basis (HTA) offers
    (their futures price is not set yet). Contracts = bushels / contract size.
    """
    _require_admin(db, tenant_id, admin_id)
    viewer = {"role": "elevator_admin", "user_id": admin_id, "tenant_id": tenant_id}
    offers = list_offers(db, tenant_id, viewer)
    now = [o for o in offers if o["status"] in HELD and o["hedged_at"] is None]
    potential = [o for o in offers if o["status"] == "submitted"]

    def build(rows):
        buckets: dict = {}
        for o in rows:
            if o["price_type"] == "BASIS":
                kind, level = "Basis (HTA)", o["basis_cents"]
            elif o["contract_type"] == "FUTURES_ONLY":
                kind, level = "Futures only", o["implied_futures"]
            else:
                kind, level = "Flat price", o["implied_futures"]
            key = (o["grain"], o["futures_symbol"], kind, level)
            b = buckets.setdefault(key, dict(grain=o["grain"], futures_symbol=o["futures_symbol"], kind=kind,
                                             futures_month=contracts.futures_label(o["futures_symbol"]),
                                             level=level, offers=0, bushels=0))
            b["offers"] += 1
            b["bushels"] += o["bushels"]
        lines = sorted(buckets.values(), key=lambda b: (_grain_rank(b["grain"]), contracts.sort_key(b["futures_symbol"]), b["kind"],
                                                         -(b["level"] if b["level"] is not None else -1e9)))
        for b in lines:
            size = CONTRACT_BUSHELS.get(b["grain"], 5000)
            b["contracts"] = round(b["bushels"] / size, 2)
        totals: dict = {}
        for b in lines:
            t = totals.setdefault((b["grain"], b["futures_symbol"]), dict(
                grain=b["grain"], futures_symbol=b["futures_symbol"], futures_month=b["futures_month"],
                exchange=(contracts.SPECS.get(contracts.canon_grain(b["grain"])) or {}).get("exchange", ""),
                offers=0, bushels=0))
            t["offers"] += b["offers"]
            t["bushels"] += b["bushels"]
        for t in totals.values():
            size = CONTRACT_BUSHELS.get(t["grain"], 5000)
            t["contracts"] = round(t["bushels"] / size, 2)
            t["whole_contracts"] = t["bushels"] // size
            t["remainder_bushels"] = t["bushels"] % size
        return lines, sorted(totals.values(), key=lambda t: (_grain_rank(t["grain"]), contracts.sort_key(t["futures_symbol"])))

    now_lines, now_totals = build(now)
    pot_lines, pot_totals = build(potential)
    return dict(now=now_lines, now_totals=now_totals, now_offers=now,
                potential=pot_lines, potential_totals=pot_totals,
                adjustments=hedge_adjustments(db, tenant_id, admin_id))


# ── Housekeeping: no price archive ───────────────────────────────────────────────
def prune_price_history(db: Db, tenant_id: str, bid_days: int = 7, suggestion_days: int = 14) -> dict:
    """Delete price rows nobody needs. A price that moved without anything happening is not archived:
      * retired (replaced) bids older than `bid_days` that no offer or confirmation points at
      * reviewed / superseded scraper suggestions older than `suggestion_days`
    Hit offers keep their price in the offer, its history event and its confirmation, so nothing of value is lost.
    Needs DELETE on bids and bid_suggestions (sql/06); the caller treats a permission error as non-fatal."""
    from datetime import timedelta, timezone
    now = datetime.now(timezone.utc)
    cut_bid = (now - timedelta(days=bid_days)).strftime("%Y-%m-%d %H:%M:%S")
    cut_sug = (now - timedelta(days=suggestion_days)).strftime("%Y-%m-%d %H:%M:%S")
    nb = db.query("SELECT COUNT(*) AS n FROM bids WHERE tenant_id = %s AND NOT active AND published_at < %s "
                  "AND bid_id NOT IN (SELECT bid_id FROM offers WHERE tenant_id = %s AND bid_id IS NOT NULL)",
                  (tenant_id, cut_bid, tenant_id))[0]["n"]
    db.execute("DELETE FROM bids WHERE tenant_id = %s AND NOT active AND published_at < %s "
               "AND bid_id NOT IN (SELECT bid_id FROM offers WHERE tenant_id = %s AND bid_id IS NOT NULL)",
               (tenant_id, cut_bid, tenant_id))
    ns = db.query("SELECT COUNT(*) AS n FROM bid_suggestions WHERE tenant_id = %s AND status <> 'suggested' "
                  "AND scraped_at < %s", (tenant_id, cut_sug))[0]["n"]
    db.execute("DELETE FROM bid_suggestions WHERE tenant_id = %s AND status <> 'suggested' AND scraped_at < %s",
               (tenant_id, cut_sug))
    return dict(retired_bids_deleted=int(nb), suggestions_deleted=int(ns))
