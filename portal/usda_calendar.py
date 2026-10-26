"""A calendar of the USDA reports that move grain markets (the "USDA Calendar" page).

Two kinds of entry:
  * fixed dates the agencies have published (WASDE and Crop Production 2026-27, the quarterly Grain Stocks /
    Prospective Plantings / Acreage / Hogs and Pigs dates that are out). Add new ones to FIXED when USDA posts them.
  * weekly reports that follow a standing schedule (Crop Progress, Export Inspections, Export Sales).
Holiday weeks can move a weekly report by a day: the page says so. Times are U.S. Central (the agencies publish in ET).
Each event has a `kind` the page colours by: 'wasde', 'nass' (NASS quarterly/annual), 'weekly'.
"""
import calendar as _cal
from datetime import date, timedelta

KIND_LABELS = {"wasde": "WASDE and Crop Production", "nass": "NASS reports", "weekly": "Weekly reports"}

# (date, report, time CT, what it is, kind)
_WASDE = [(2026, m, d) for m, d in [(1, 12), (2, 10), (3, 10), (4, 9), (5, 12), (6, 11), (7, 10), (8, 12), (9, 11),
                                    (10, 9), (11, 10), (12, 10)]] + \
         [(2027, m, d) for m, d in [(1, 12), (2, 10), (3, 10), (4, 9), (5, 12), (6, 11), (7, 9), (8, 12), (9, 10),
                                    (10, 8), (11, 10), (12, 10)]]
FIXED = [
    # WASDE + Crop Production, 12:00 ET (usda.gov WASDE schedule)
    *[(date(y, m, d), "WASDE and Crop Production", "11:00 AM", "Supply and demand: yields, stocks, exports", "wasde")
      for y, m, d in _WASDE],
    (date(2026, 1, 12), "Grain Stocks", "11:00 AM", "Dec. 1 stocks, with the annual Crop Production summary", "nass"),
    (date(2026, 3, 26), "Hogs and Pigs", "2:00 PM", "Quarterly hog inventory", "nass"),
    (date(2026, 3, 31), "Prospective Plantings", "11:00 AM", "Planting intentions", "nass"),
    (date(2026, 3, 31), "Grain Stocks", "11:00 AM", "Mar. 1 stocks", "nass"),
    (date(2026, 6, 25), "Hogs and Pigs", "2:00 PM", "Quarterly hog inventory", "nass"),
    (date(2026, 6, 30), "Acreage", "11:00 AM", "Planted acres", "nass"),
    (date(2026, 6, 30), "Grain Stocks", "11:00 AM", "June 1 stocks", "nass"),
    (date(2026, 9, 24), "Hogs and Pigs", "2:00 PM", "Quarterly hog inventory", "nass"),
    (date(2026, 9, 30), "Grain Stocks", "11:00 AM", "Sept. 1 stocks, with the Small Grains Summary", "nass"),
    (date(2026, 12, 23), "Hogs and Pigs", "2:00 PM", "Quarterly hog inventory", "nass"),
    (date(2027, 1, 12), "Grain Stocks", "11:00 AM", "Dec. 1 stocks, with the annual Crop Production summary", "nass"),
    (date(2027, 3, 31), "Prospective Plantings", "11:00 AM", "Planting intentions", "nass"),
    (date(2027, 3, 31), "Grain Stocks", "11:00 AM", "Mar. 1 stocks", "nass"),
    (date(2027, 6, 30), "Acreage", "11:00 AM", "Planted acres", "nass"),
    (date(2027, 6, 30), "Grain Stocks", "11:00 AM", "June 1 stocks", "nass"),
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


def events_between(start: date, end: date) -> list[dict]:
    """Every event from start through end (inclusive), soonest first: [{date, weekday, report, time, note, kind}]."""
    rows = [dict(date=d, report=r, time=t, note=n, kind=k) for d, r, t, n, k in FIXED if start <= d <= end]
    d = start
    while d <= end:
        for wd, report, t, note, season in WEEKLY:
            if d.weekday() == wd and (season is None or season[0] <= d.month <= season[1]):
                rows.append(dict(date=d, report=report, time=t, note=note, kind="weekly"))
        d += timedelta(days=1)
    for r in rows:
        r["weekday"] = r["date"].strftime("%a")
    return sorted(rows, key=lambda r: (r["date"], _clock(r["time"]), r["report"]))


def upcoming(today: date | None = None, days: int = 30) -> list[dict]:
    """Reports from today through `days` ahead, soonest first."""
    today = today or date.today()
    return events_between(today, today + timedelta(days=days))


def month_grid(year: int, month: int) -> list[list[dict]]:
    """The month as weeks (Sunday first) of day cells: {date, in_month, events}. Events are the ones on that date."""
    weeks = _cal.Calendar(firstweekday=6).monthdatescalendar(year, month)
    events = events_between(weeks[0][0], weeks[-1][-1])
    by_day: dict[date, list[dict]] = {}
    for e in events:
        by_day.setdefault(e["date"], []).append(e)
    return [[dict(date=d, in_month=d.month == month, events=by_day.get(d, [])) for d in week] for week in weeks]
