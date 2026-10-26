from datetime import date

from conftest import goto
from portal import usda_calendar as cal
from test_app import _as, app_db  # noqa: F401  (fixture + signed-in session helper)


def test_month_grid_is_whole_weeks_with_events_on_their_dates():
    weeks = cal.month_grid(2026, 11)                        # Nov 2026 starts on a Sunday
    assert all(len(w) == 7 for w in weeks) and weeks[0][0]["date"] == date(2026, 11, 1)
    day = {d["date"]: d for w in weeks for d in w}
    assert [e["report"] for e in day[date(2026, 11, 10)]["events"]] == ["WASDE and Crop Production"]
    assert {e["report"] for e in day[date(2026, 11, 9)]["events"]} == {"Export Inspections", "Crop Progress"}
    assert not day[date(2026, 11, 14)]["events"]            # a Saturday
    assert all(d["in_month"] for d in weeks[1])


def test_every_published_wasde_date_is_a_business_day_and_unique():
    wasde = [e[0] for e in cal.FIXED if e[4] == "wasde"]
    assert len(wasde) == len(set(wasde)) == 24 and all(d.weekday() < 5 for d in wasde)


def test_page_shows_a_calendar_that_pages_by_month(app_db):
    at = goto(_as(app_db, 1), "USDA Calendar")
    assert not at.exception, at.exception
    html = " ".join(m.value for m in at.markdown)
    assert "cal-day" in html and "Export Sales" in html
    next(b for b in at.button if b.label == "Next").click().run()
    assert not at.exception and at.session_state["cal_ym"] != (date.today().year, date.today().month)
    next(b for b in at.button if b.label == "Today").click().run()
    assert at.session_state["cal_ym"] == (date.today().year, date.today().month)
