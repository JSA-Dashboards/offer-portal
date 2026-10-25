from datetime import date

from portal import usda_calendar as cal


def test_next_two_weeks_from_a_known_date():
    rows = cal.upcoming(date(2026, 10, 25), 16)             # Sun Oct 25 -> Tue Nov 10
    got = [(str(r["date"]), r["report"]) for r in rows]
    assert got[0] == ("2026-10-26", "Export Inspections") and ("2026-10-26", "Crop Progress") in got
    assert ("2026-10-29", "Export Sales") in got and ("2026-11-10", "WASDE and Crop Production") in got
    assert [r["weekday"] for r in rows if r["report"] == "Export Sales"] == ["Thu", "Thu"]


def test_crop_progress_stops_after_november_and_order_is_by_time():
    assert not [r for r in cal.upcoming(date(2026, 12, 1), 20) if r["report"] == "Crop Progress"]
    day = [r["report"] for r in cal.upcoming(date(2026, 10, 26), 0)]
    assert day == ["Export Inspections", "Crop Progress"]       # 10:00 AM before 3:00 PM


def test_fixed_dates_are_unique_and_future_ones_appear():
    assert any(r["report"] == "Prospective Plantings" for r in cal.upcoming(date(2027, 3, 25), 10))
    keys = [(d, r) for d, r, _, _ in cal.FIXED]
    assert len(keys) == len(set(keys))
