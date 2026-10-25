"""scrapers.py — pull a tenant's public cash bids into normalized suggestion dicts.

Adapted from the basis tracker's agricharts_scraper / bushelsites_scraper (copied,
not imported, so this repo stands alone). Each fetcher returns a list of:
    {location_name, grain, delivery_month 'YYYY-MM', futures_symbol, basis_cents, cash_price}
Output goes to bid_suggestions via data.sync_suggestions — never straight to farmers.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from .contracts import SPECS
from .data import month_bounds

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; JPSI offer portal; kpostin@jpsi.com)"}
_AGRI_QS = ("?filter=all&location=&commodity=&groupby=location&format=table"
            "&fields=name,delivery_start,delivery_end,basismonth,futures,futureschange,basis,price"
            "&bidsort=commodity&dateformat=%25m/%25d/%25Y&months=11")
_BIDS_RE = re.compile(r"var bids\s*=\s*(\[.*?\]);", re.DOTALL)
_MON = {"jan": "F", "feb": "G", "mar": "H", "apr": "J", "may": "K", "jun": "M",
        "jul": "N", "aug": "Q", "sep": "U", "oct": "V", "nov": "X", "dec": "Z"}
_MONNUM = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_ROOT = {g: sp["root"] for g, sp in SPECS.items()}
HARVEST_MONTH = 10          # "Harvest 2026" -> 2026-10 (approximate; the admin confirms every bid)


def norm_grain(text: str) -> str:
    t = (text or "").lower()
    if "bean" in t:
        return "Soybeans"
    if "wheat" in t:
        # A plain "wheat" line is CBOT soft red; hard red and spring are named by class (the admin confirms every bid).
        if "hrw" in t or "hard red" in t or "hard winter" in t:
            return "HRW Wheat"
        if "hrs" in t or "spring" in t or "dark northern" in t:
            return "HRS Wheat"
        return "SRW Wheat"
    return "Corn"


def _money(v) -> float | None:
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def parse_agricharts(feed: list[dict]) -> list[dict]:
    out = []
    for loc in feed:
        if str(loc.get("hide_on_sites_and_apis") or "").strip().lower() in ("1", "true", "yes"):
            continue
        name = (loc.get("name") or "").strip()
        if not name or name.lower() == "futures only":
            continue
        for b in loc.get("cashbids") or []:
            try:
                ds = datetime.strptime(b.get("delivery_start") or "", "%m/%d/%Y")
            except ValueError:
                continue
            if b.get("basis") is None or not b.get("symbol"):
                continue
            try:
                de = datetime.strptime(b.get("delivery_end") or "", "%m/%d/%Y")
            except ValueError:
                de = None
            if de is not None and de < ds:
                de = None                                  # a nonsense range is no range: staff will be asked for one
            out.append(dict(location_name=name, grain=norm_grain(b.get("name")),
                            delivery_month=ds.strftime("%Y-%m"), futures_symbol=b["symbol"],
                            delivery_start=ds.strftime("%Y-%m-%d") if de else None,
                            delivery_end=de.strftime("%Y-%m-%d") if de else None,
                            basis_cents=int(b["basis"]), cash_price=_money(b.get("price"))))
    return out


def fetch_agricharts(host: str) -> list[dict]:
    r = requests.get(host.rstrip("/") + "/inc/cashbids/cashbids-js.php" + _AGRI_QS, headers=UA, timeout=30)
    r.raise_for_status()
    m = _BIDS_RE.search(r.text)
    return parse_agricharts(json.loads(m.group(1)) if m else [])


def _fut_symbol(fut_month: str, grain: str) -> str | None:
    m = re.search(r"([A-Za-z]{3})[A-Za-z]*\s+(\d{2})", fut_month or "")
    code = _MON.get(m.group(1).lower()) if m else None
    return f"{_ROOT[grain]}{code}{m.group(2)}" if code else None


def _bushel_range(label: str) -> tuple[str | None, str | None]:
    """Delivery range from a Bushel row label. Only a plain month ('January 2027') is a clear range (the whole month);
    'Harvest 2026', 'New crop', seasons or multi-month labels are NOT, and return (None, None) so staff must enter one."""
    m = re.match(r"^([a-z]{3})[a-z]*\.?\s+(20\d{2})$", (label or "").strip().lower())
    if not m or m.group(1) not in _MONNUM:
        return None, None
    first, last = month_bounds(f"{m.group(2)}-{_MONNUM[m.group(1)]:02d}")
    return first.isoformat(), last.isoformat()


def _bushel_month(label: str) -> str | None:
    lab = (label or "").strip().lower()
    y = re.search(r"\b(20\d{2})\b", lab)
    if not y:
        return None
    if "harvest" in lab:
        return f"{y.group(1)}-{HARVEST_MONTH:02d}"
    for m, n in _MONNUM.items():
        if lab.startswith(m):
            return f"{y.group(1)}-{n:02d}"
    return None


def parse_bushelsites(html: str, location_name: str) -> list[dict]:
    from bs4 import BeautifulSoup
    out = []
    for div in BeautifulSoup(html, "html.parser").find_all("div", class_="cbCommodity"):
        h = div.find("h3")
        grain = norm_grain(h.get_text(strip=True) if h else "")
        for ul in div.find_all("ul"):
            c = {li.get("class", ["?"])[0]: li.get_text(" ", strip=True) for li in ul.find_all("li")}
            if not c.get("c1") or c["c1"].lower() == "delivery":
                continue
            month, sym = _bushel_month(c["c1"]), _fut_symbol(c.get("c6", ""), grain)
            try:
                basis = round(float(c["c3"].replace("+", "")) * 100)
            except (KeyError, ValueError):
                continue
            if month and sym:
                ds, de = _bushel_range(c["c1"])
                out.append(dict(location_name=location_name, grain=grain, delivery_month=month,
                                delivery_start=ds, delivery_end=de,
                                futures_symbol=sym, basis_cents=basis, cash_price=_money(c.get("c2"))))
    return out


def fetch_bushelsites(url: str, location_name: str) -> list[dict]:
    r = requests.get(url, headers=UA, timeout=40)
    r.raise_for_status()
    return parse_bushelsites(r.text, location_name)


def fetch_source(src: dict) -> list[dict]:
    """src: a scrape_sources row. kind 'agricharts' (url = site host) | 'bushelsites'
    (url = the bids page; location_name = the one tenant location it feeds)."""
    if src["kind"] == "agricharts":
        return fetch_agricharts(src["url"])
    if src["kind"] == "bushelsites":
        return fetch_bushelsites(src["url"], src["location_name"])
    raise ValueError(f"unknown scraper kind {src['kind']!r}")
