"""A quick-reference calendar of the USDA reports that move grain markets (shown on the farmer and staff pages).

Two kinds of entry:
  * fixed dates the agencies have published (WASDE and Crop Production 2026-27, the quarterly Grain Stocks /
    Prospective Plantings / Acreage / Hogs and Pigs dates that are out). Add new ones to FIXED when USDA posts them.
  * weekly reports that follow a standing schedule (Crop Progress, Export Inspections, Export Sales).
Holiday weeks can move a weekly report by a day: the page says so. Times are U.S. Central (the agencies publish in ET).
"""
from datetime import date, timedelta

# (date, report, time CT, what it is)
FIXED = [
    # WASDE + Crop Production, 12:00 ET (usda.gov WASDE schedule)
    *[(date(y, m, d), "WASDE and Crop Production", "11:00 AM", "Supply and demand: yields, stocks, exports")
      for y, m, d in [(2026, 11, 10), (2026, 12, 10), (2027, 1, 12), (2027, 2, 10), (2027, 3, 10), (2027, 4, 9),
                      (2027, 5, 12), (2027, 6, 11), (2027, 7, 9), (2027, 8, 12), (2027, 9, 10), (2027, 10, 8),
                      (2027, 11, 10), (2027, 12, 10)]],
    (date(2026, 12, 23), "Hogs and Pigs", "2:00 PM", "Quarterly hog inventory"),
    (date(2027, 1, 12), "Grain Stocks", "11:00 AM", "Dec. 1 stocks, with the annual Crop Production summary"),
    (date(2027, 3, 31), "Prospective Plantings", "11:00 AM", "Planting intentions"),
    (date(2027, 3, 31), "Grain Stocks", "11:00 AM", "Mar. 1 stocks"),
    (date(2027, 6, 30), "Acreage", "11:00 AM", "Planted acres"),
    (date(2027, 6, 30), "Grain Stocks", "11:00 AM", "June 1 stocks"),
]

_SEASON = (4, 11)          # Crop Progress runs Apr-Nov
WEEKLY = [
    (0, "Export Inspections", "10:00 AM", "Grain loaded for export last week", None),
    (0, "Crop Progress", "3:00 PM", "Planting, condition and harvest pace", _SEASON),
    (3, "Export Sales", "7:30 AM", "Last week's new sales and shipments", None),
]


def _clock(t: str) -> int:
    """'2:00 PM' -> minutes after midnight, for ordering."""
    hm, ap = t.split()
    h, m = (int(x) for x in hm.split(":"))
    return (h % 12 + (12 if ap == "PM" else 0)) * 60 + m


def upcoming(today: date | None = None, days: int = 30) -> list[dict]:
    """Reports from today through `days` ahead, soonest first: [{date, weekday, report, time, note}]."""
    today = today or date.today()
    end = today + timedelta(days=days)
    rows = [dict(date=d, report=r, time=t, note=n) for d, r, t, n in FIXED if today <= d <= end]
    d = today
    while d <= end:
        for wd, report, t, note, season in WEEKLY:
            if d.weekday() == wd and (season is None or season[0] <= d.month <= season[1]):
                rows.append(dict(date=d, report=report, time=t, note=note))
        d += timedelta(days=1)
    for r in rows:
        r["weekday"] = r["date"].strftime("%a")
    return sorted(rows, key=lambda r: (r["date"], _clock(r["time"]), r["report"]))
