"""futures.py — delayed futures quotes. Quotes are market data (no tenant_id); everything that touches a tenant's
bids stays tenant-scoped.

How a bid moves (Kolten, 2026-10-25): the sticky part of a bid is its BASIS. Staff post or confirm a basis and it stays
until they change it. The flat bid is futures + basis, so it moves with every delayed quote (data.live_flat). Offers are
compared with that live flat bid to decide whether they were hit. A quote therefore never edits a stored bid; it only
changes the computed flat price, and after each quote refresh the auto-hit rules run.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import massive
from .data import _insert, expire_all_tenants, run_auto_hit
from .db import Db


def update_quotes(db: Db, quotes: dict[str, dict], source: str = "massive") -> int:
    """Store the latest delayed quote per symbol (one row per symbol: update, else insert)."""
    n = 0
    for sym, q in quotes.items():
        as_of = q["as_of"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if q.get("as_of") else None
        if db.query("SELECT 1 AS x FROM futures_quotes WHERE symbol = %s", (sym,)):
            db.execute("UPDATE futures_quotes SET price = %s, as_of = %s, fetched_at = CURRENT_TIMESTAMP(), "
                       "source = %s WHERE symbol = %s", (q["price"], as_of, source, sym))
        else:
            _insert(db, "futures_quotes", dict(symbol=sym, price=q["price"], as_of=as_of, source=source))
        n += 1
    return n


def list_quotes(db: Db, symbols=None) -> dict[str, dict]:
    rows = db.query("SELECT symbol, price, as_of, fetched_at, source FROM futures_quotes")
    want = set(symbols) if symbols is not None else None
    return {r["symbol"]: r for r in rows if want is None or r["symbol"] in want}


def latest_stamp(quotes: dict[str, dict]) -> str | None:
    """Human-readable 'as of' for a banner: the newest quote time we hold (UTC)."""
    stamps = [str(q["as_of"] or q["fetched_at"])[:16] for q in quotes.values() if q.get("as_of") or q.get("fetched_at")]
    return (max(stamps) + " UTC") if stamps else None


def wanted_symbols(db: Db) -> list[str]:
    """Every futures month that matters right now, across tenants: live bids and open (outstanding or hit) offers."""
    rows = db.query("SELECT futures_symbol AS s FROM bids WHERE active AND futures_symbol IS NOT NULL "
                    "UNION SELECT futures_symbol FROM offers WHERE status IN ('submitted', 'working', 'hit', 'acknowledged', 'completed') "
                    "AND futures_symbol IS NOT NULL")
    return sorted({r["s"] for r in rows if r["s"]})


def run_update(db: Db, fetch=massive.fetch_quotes) -> dict:
    """The job: fetch delayed quotes for every needed futures month, store them, then run each active tenant's
    auto-hit rules against the new live flat bids. Without MASSIVE_API_KEY it does nothing (and says so)."""
    if fetch is massive.fetch_quotes and not massive.configured():
        return dict(status="not configured: set MASSIVE_API_KEY; bids stay as posted")
    symbols = wanted_symbols(db)
    if not symbols:
        return dict(status="no futures months in use yet")
    quotes = fetch(symbols)
    stored = update_quotes(db, quotes)
    expire_all_tenants(db)                                 # an offer whose delivery period ended is never hit
    tenants = db.query("SELECT tenant_id FROM tenants WHERE status = 'active' ORDER BY tenant_id")
    out = {t["tenant_id"]: dict(auto_hit=run_auto_hit(db, t["tenant_id"])) for t in tenants}
    return dict(status="ok", symbols=len(symbols), quotes=stored, tenants=out,
                missing=sorted(set(symbols) - set(quotes)))
