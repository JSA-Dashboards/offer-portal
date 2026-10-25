"""futures.py — delayed futures quotes and the "indicated cash" they suggest. Quotes are market data (no
tenant_id); everything that touches a tenant's bids stays tenant-scoped.

Bids are STICKY. A quote never changes a posted bid, an offer, a hit or a hedge number. The only thing a
quote can do is stage a *suggestion* (futures + the bid's posted basis = indicated cash) in the same review
queue the scrapers use; an elevator admin edits and confirms it, and only then does the bid change.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import massive
from .data import _insert, list_bids, list_locations, sync_suggestions
from .db import Db

THRESHOLD = 0.0025            # indicated cash must differ from the posted cash by a quarter cent to be suggested


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


def indicated_cash(bid: dict, quote: dict | None) -> float | None:
    """Futures (delayed) + the bid's posted basis. None without a quote or a basis."""
    if not quote or bid.get("basis_cents") is None or quote.get("price") is None:
        return None
    return round(float(quote["price"]) + bid["basis_cents"] / 100.0, 4)


def wanted_symbols(db: Db) -> list[str]:
    """Every futures month that matters right now, across tenants: live bids and open (outstanding or hit) offers."""
    rows = db.query("SELECT futures_symbol AS s FROM bids WHERE active AND futures_symbol IS NOT NULL "
                    "UNION SELECT futures_symbol FROM offers WHERE status IN ('submitted', 'hit') "
                    "AND futures_symbol IS NOT NULL")
    return sorted({r["s"] for r in rows if r["s"]})


def suggest_cash_updates(db: Db, tenant_id: str, quotes: dict[str, dict] | None = None) -> dict:
    """Stage 'indicated cash' suggestions for a tenant's live bids. Skipped when the tenant has an active
    scrape source: its own site's cash bids are the better signal there. Never touches a bid."""
    if db.query("SELECT 1 AS x FROM scrape_sources WHERE tenant_id = %s AND active", (tenant_id,)):
        return dict(skipped="tenant has an active scrape source")
    quotes = quotes if quotes is not None else list_quotes(db)
    names = {l["location_id"]: l["name"] for l in list_locations(db, tenant_id)}
    items = []
    for b in list_bids(db, tenant_id):
        cash = indicated_cash(b, quotes.get(b["futures_symbol"]))
        if cash is None or b["location_id"] not in names:
            continue
        if b["cash_price"] is not None and abs(cash - b["cash_price"]) < THRESHOLD:
            continue
        items.append(dict(location_name=names[b["location_id"]], grain=b["grain"], delivery_month=b["delivery_month"],
                          futures_symbol=b["futures_symbol"], basis_cents=b["basis_cents"], cash_price=cash))
    return sync_suggestions(db, tenant_id, "massive:delayed", items) if items else dict(staged=0)


def run_update(db: Db, fetch=massive.fetch_quotes) -> dict:
    """The job: fetch delayed quotes for every needed futures month, store them, stage suggestions for each
    active tenant. Without MASSIVE_API_KEY it does nothing (and says so); bids stay exactly as posted."""
    if fetch is massive.fetch_quotes and not massive.configured():
        return dict(status="not configured: set MASSIVE_API_KEY; bids stay as posted")
    symbols = wanted_symbols(db)
    if not symbols:
        return dict(status="no futures months in use yet")
    quotes = fetch(symbols)
    stored = update_quotes(db, quotes)
    tenants = db.query("SELECT tenant_id FROM tenants WHERE status = 'active' ORDER BY tenant_id")
    out = {t["tenant_id"]: suggest_cash_updates(db, t["tenant_id"], list_quotes(db)) for t in tenants}
    return dict(status="ok", symbols=len(symbols), quotes=stored, tenants=out,
                missing=sorted(set(symbols) - set(quotes)))
