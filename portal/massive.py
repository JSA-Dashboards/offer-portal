"""massive.py — PLACEHOLDER client for delayed futures quotes from the Massive API (api.massive.com).

Status: wired up and unit-tested against a fabricated payload only. It has NOT been run against the live
API from this project, and it needs `MASSIVE_API_KEY` (the same key the River FOB portal and the basis
tracker use; the plan serves DELAYED snapshots). Without a key `fetch_quotes` returns nothing and the portal
carries on with the bids as posted.

Feed coverage: CBOT corn / soybeans / SRW wheat (ZC, ZS, ZW) and KC HRW wheat (KE; Massive lists it under XCBT).
MIAX spring wheat (MWE) is not carried, so HRS bids are never priced off futures here.

Shape taken from JSA's notes on the API (see the basis tracker's massive_futures.py):
  GET https://api.massive.com/futures/v1/snapshot?ticker.any_of=ZCZ6,ZSX6   Authorization: Bearer <key>
  results[] -> details.ticker ("ZCZ6", ONE-digit year), last_trade.price, session.settlement_price /
  session.close; grains are quoted in CENTS per bushel (we store $/bu).
If the live payload differs, `parse_snapshot` is the one place to adjust.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

BASE = "https://api.massive.com/futures/v1"
_SYMBOL = re.compile(r"^([A-Z]{1,3})([FGHJKMNQUVXZ])(\d{2})$")
CHUNK = 25
NO_FEED_ROOTS = ("MWE",)    # MIAX spring wheat: not on Massive's feed. Its bids stay as posted (flat_source 'posted').


def configured() -> bool:
    return bool((os.environ.get("MASSIVE_API_KEY") or "").strip())


def to_ticker(symbol: str) -> str | None:
    """Our two-digit-year symbol -> Massive's one-digit-year ticker: ZCZ26 -> ZCZ6. None if not an outright."""
    m = _SYMBOL.match((symbol or "").strip().upper())
    if not m or m.group(1) in NO_FEED_ROOTS:
        return None
    return f"{m.group(1)}{m.group(2)}{m.group(3)[-1]}"


def _as_of(block: dict | None) -> datetime | None:
    """Last-trade time: Massive uses nanosecond epochs; accept the usual field names."""
    for k in ("last_updated", "sip_timestamp", "participant_timestamp", "timestamp"):
        v = (block or {}).get(k)
        if isinstance(v, (int, float)) and v > 0:
            secs = v / 1e9 if v > 1e14 else (v / 1e3 if v > 1e11 else v)
            return datetime.fromtimestamp(secs, tz=timezone.utc)
    return None


def parse_snapshot(payload: dict, wanted: dict[str, str]) -> dict[str, dict]:
    """`wanted` maps Massive ticker -> our symbol. Price fallback: last trade, then settlement, then close.
    Returns {our_symbol: {"price": $/bu float, "as_of": datetime | None}}."""
    out = {}
    for r in (payload or {}).get("results") or []:
        tk = ((r.get("details") or {}).get("ticker")) or r.get("ticker")
        sym = wanted.get(tk)
        if not sym:
            continue
        lt, sess = r.get("last_trade") or {}, r.get("session") or {}
        px = next((v for v in (lt.get("price"), sess.get("settlement_price"), sess.get("close"))
                   if isinstance(v, (int, float)) and v > 0), None)
        if px is None:
            continue
        out[sym] = {"price": round(px / 100.0, 4), "as_of": _as_of(lt)}
    return out


def fetch_quotes(symbols, api_key: str | None = None, session=None) -> dict[str, dict]:
    """Delayed quotes for our symbols. {} when no key is configured (the portal then keeps bids as posted)."""
    key = (api_key or os.environ.get("MASSIVE_API_KEY") or "").strip()
    if not key:
        return {}
    import requests
    http = session or requests
    wanted = {t: s for s in sorted(set(symbols)) if (t := to_ticker(s))}
    tickers = list(wanted)
    out: dict[str, dict] = {}
    for i in range(0, len(tickers), CHUNK):
        part = tickers[i:i + CHUNK]
        r = http.get(f"{BASE}/snapshot", params={"ticker.any_of": ",".join(part)},
                     headers={"Authorization": f"Bearer {key}"}, timeout=20)
        r.raise_for_status()
        out.update(parse_snapshot(r.json(), {t: wanted[t] for t in part}))
    return out
