"""The futures contract behind each commodity: which exchange, how big, and which months are listed.

A bid's futures month is what tells the elevator how to hedge it, so every bid and offer carries a symbol
(root + month code + two-digit year, e.g. ZCZ26) and it must be a real listed month for that commodity.
Wheat is three markets, not one: SRW (CBOT), HRW (KC) and HRS (MGEX / MIAX spring wheat).
"""
from datetime import date

GRAINS = ("Corn", "Soybeans", "SRW Wheat", "HRW Wheat", "HRS Wheat")
LEGACY = {"Wheat": "SRW Wheat"}          # the single "Wheat" the first build used was CBOT soft red

_MONTH_CODE = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6, "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
_MONTH_NAME = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

SPECS = {
    "Corn":      dict(root="ZC",  exchange="CBOT", bushels=5000, months="HKNUZ",   name="Corn (CBOT ZC)"),
    "Soybeans":  dict(root="ZS",  exchange="CBOT", bushels=5000, months="FHKNQUX", name="Soybeans (CBOT ZS)"),
    "SRW Wheat": dict(root="ZW",  exchange="CBOT", bushels=5000, months="HKNUZ",   name="SRW wheat (CBOT ZW)"),
    "HRW Wheat": dict(root="KE",  exchange="KC",   bushels=5000, months="HKNUZ",   name="HRW wheat (KC KE)"),
    "HRS Wheat": dict(root="MWE", exchange="MIAX", bushels=5000, months="HKNUZ",   name="HRS wheat (MIAX MWE)"),
}


def canon_grain(grain: str | None) -> str | None:
    return LEGACY.get(grain, grain)


def bushels_per_contract(grain: str) -> int:
    spec = SPECS.get(canon_grain(grain))
    return spec["bushels"] if spec else 5000


def parse_symbol(symbol: str | None):
    """'ZCZ26' -> ('ZC', 'Z', 2026), or None when it is not root + month code + 2-digit year."""
    s = (symbol or "").strip().upper()
    if len(s) < 5 or not s[-2:].isdigit() or s[-3] not in _MONTH_CODE:
        return None
    return s[:-3], s[-3], 2000 + int(s[-2:])


def check_symbol(grain: str, symbol: str | None) -> str:
    """The cleaned symbol, or ValueError saying what is wrong. The month must be listed for this commodity."""
    g = canon_grain(grain)
    spec = SPECS.get(g)
    if spec is None:
        raise ValueError(f"Unknown commodity {grain!r}.")
    s = (symbol or "").strip().upper()
    if not s:
        raise ValueError(f"Choose the futures month ({spec['root']} + month + year, e.g. {spec['root']}{spec['months'][-1]}26).")
    p = parse_symbol(s)
    if p is None or p[0] != spec["root"]:
        raise ValueError(f"{s!r} is not a {spec['name']} contract. Use {spec['root']} + month code + year, "
                         f"e.g. {spec['root']}{spec['months'][-1]}26.")
    if p[1] not in spec["months"]:
        listed = ", ".join(_MONTH_NAME[_MONTH_CODE[c] - 1] for c in spec["months"])
        raise ValueError(f"{s} is not a listed month: {spec['name']} trades {listed}.")
    return s


def calendar_month(symbol: str | None) -> str | None:
    """'ZCZ26' -> '2026-12': the calendar month a futures month stands for (an offer on it runs through that month)."""
    p = parse_symbol(symbol)
    return f"{p[2]}-{_MONTH_CODE[p[1]]:02d}" if p else None


def futures_label(symbol: str | None) -> str:
    """'ZCZ26' -> 'Dec 2026'; anything unparseable comes back as given."""
    p = parse_symbol(symbol)
    return f"{_MONTH_NAME[_MONTH_CODE[p[1]] - 1]} {p[2]}" if p else (symbol or "—")


def sort_key(symbol: str | None):
    """Chronological order for futures months (unknown symbols last)."""
    p = parse_symbol(symbol)
    return (p[2], _MONTH_CODE[p[1]]) if p else (9999, 99)


def options(grain: str, today: date | None = None, count: int = 8) -> list[str]:
    """The next `count` listed futures months for a commodity, as symbols (current month included)."""
    spec = SPECS[canon_grain(grain)]
    today = today or date.today()
    codes = sorted((n, c) for c, n in _MONTH_CODE.items() if c in spec["months"])
    out, y = [], today.year
    while len(out) < count:
        for n, c in codes:
            if (y, n) >= (today.year, today.month) and len(out) < count:
                out.append(f"{spec['root']}{c}{y % 100:02d}")
        y += 1
    return out
