from conftest import goto, pages  # noqa: F401
from datetime import date, timedelta

import pytest
from portal import data, notify, scrapers
from portal.data import PortalError
from conftest import user
from test_app import _as, app_db  # noqa: F401  (fixture + signed-in session helper)

TODAY = data.today_ct()
D = lambda n: (TODAY + timedelta(days=n)).isoformat()                    # a day relative to today
HTA = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=5000, basis_cents=-30, futures_symbol="ZCZ26")


def _offer(db, **kw):
    return data.create_offer(db, "rcm", user(db, 1), **{**HTA, **kw})


def _status(db, oid):
    return [o for o in data.list_offers(db, "rcm", user(db, 2)) if o["offer_id"] == oid][0]["status"]


def _age(db, oid, **cols):
    """Move an offer's dates into the past directly (the entry check refuses a period that has already ended)."""
    sets = ", ".join(f"{k} = %s" for k in cols)
    db.execute(f"UPDATE offers SET {sets} WHERE offer_id = %s", tuple(cols.values()) + (oid,))


# ── the date rules ───────────────────────────────────────────────────────────
def test_month_bounds_and_bid_window():
    assert data.month_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))
    assert data.month_bounds("2028-02")[1] == date(2028, 2, 29)                       # leap year
    assert data.month_bounds("2026-13") is None and data.month_bounds("Dec 2026") is None and data.month_bounds("") is None
    assert data.bid_window({"delivery_month": "2026-12"}) == (date(2026, 12, 1), date(2026, 12, 31))
    assert data.bid_window({"delivery_month": "2026-12", "delivery_start": "2026-09-01", "delivery_end": "2026-11-30"}) \
        == (date(2026, 9, 1), date(2026, 11, 30))                                    # its own dates win over the month


def test_the_last_delivery_day_comes_from_the_offer_then_the_bid_then_the_month():
    f = data.offer_delivery_end
    snap = '{"delivery_month": "2026-12", "delivery_end": "2026-11-30"}'
    assert f({"delivery_end": "2026-10-20", "bid_snapshot": snap, "delivery_start": "2026-10-01"}) == date(2026, 10, 20)
    assert f({"delivery_end": None, "bid_snapshot": snap}) == date(2026, 11, 30)       # the bid's range at submission
    assert f({"bid_snapshot": '{"delivery_month": "2026-12"}'}) == date(2026, 12, 31)    # an old bid with only a month
    assert f({"delivery_start": date(2026, 2, 10)}) == date(2026, 2, 28)               # a start date alone: that month's end
    assert f({}) is None and f({"bid_snapshot": "{}"}) is None and f({"delivery_end": "garbage"}) is None


# ── offers expire the day after the last delivery day ───────────────────────
def test_an_offer_lives_through_its_last_delivery_day_and_expires_the_day_after(db):
    last_day, tomorrow, yesterday = _offer(db, delivery_end=D(0)), _offer(db, delivery_end=D(1)), _offer(db, delivery_end=D(5))
    _age(db, yesterday, delivery_end=D(-1))
    assert data.expire_stale(db, "rcm") == 1
    assert (_status(db, last_day), _status(db, tomorrow), _status(db, yesterday)) == ("submitted", "submitted", "expired")
    ev = db.query("SELECT detail, actor_user_id FROM offer_events WHERE offer_id = %s AND event_type = 'expired'", (yesterday,))[0]
    assert '"reason": "delivery_period_ended"' in ev["detail"] and ev["actor_user_id"] is None
    assert data.expire_stale(db, "rcm") == 0                                            # once only


def test_an_offer_made_against_a_bid_expires_when_the_bids_delivery_range_ends(db):
    db.execute("INSERT INTO locations (location_id, tenant_id, name) VALUES (1, 'rcm', 'Riverton')")
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month=TODAY.strftime("%Y-%m"), futures_symbol="ZCZ26", location_id=1,
                     basis_cents=-38, cash_price=4.18, delivery_start=D(-20), delivery_end=D(10))
    bid = data.list_bids(db, "rcm")[0]
    assert (str(bid["window_start"]), str(bid["window_end"])) == (D(-20), D(10))
    oid = _offer(db, bid_id=bid["bid_id"])
    assert str(data.offer_delivery_end([o for o in data.list_offers(db, "rcm", user(db, 2)) if o["offer_id"] == oid][0])) == D(10)
    snap = db.query("SELECT bid_snapshot FROM offers WHERE offer_id = %s", (oid,))[0]["bid_snapshot"]
    db.execute("UPDATE offers SET bid_snapshot = %s WHERE offer_id = %s", (snap.replace(D(10), D(-1)), oid))   # the bid's range ended yesterday
    assert data.expire_stale(db, "rcm") == 1 and _status(db, oid) == "expired"


def test_an_offer_with_only_a_start_date_expires_at_the_end_of_that_month(db):
    oid = _offer(db, delivery_start=D(0))
    _age(db, oid, delivery_start="2020-03-10")
    assert data.expire_stale(db, "rcm") == 1 and _status(db, oid) == "expired"


def test_offers_with_no_delivery_information_are_left_to_their_good_until_date(db):
    oid = _offer(db)
    assert data.expire_stale(db, "rcm") == 0 and _status(db, oid) == "submitted"
    good = _offer(db, expires_at="2000-01-01 00:00:00")                                  # good-until still works on its own
    assert data.expire_stale(db, "rcm") == 1 and _status(db, good) == "expired"
    ev = db.query("SELECT detail FROM offer_events WHERE offer_id = %s AND event_type = 'expired'", (good,))[0]["detail"]
    assert '"reason": "good_until"' in ev


def test_only_outstanding_offers_expire_a_hit_offer_is_a_done_deal(db):
    hit, ack = _offer(db, delivery_end=D(3)), _offer(db, delivery_end=D(3))
    data.act_on_offer(db, "rcm", user(db, 2), hit, "hit")
    data.act_on_offer(db, "rcm", user(db, 2), ack, "hit")
    data.acknowledge_offer(db, "rcm", 2, ack)
    _age(db, hit, delivery_end=D(-30))
    _age(db, ack, delivery_end=D(-30))
    assert data.expire_stale(db, "rcm") == 0
    assert (_status(db, hit), _status(db, ack)) == ("hit", "acknowledged")


def test_expiry_tells_the_farmer_and_the_job_covers_every_tenant(db):
    db.execute("UPDATE users SET email = 'farmer@a.example' WHERE user_id = 1")
    mine, theirs = _offer(db, delivery_end=D(2)), data.create_offer(db, "dorans", user(db, 3), **HTA, delivery_end=D(2))
    db.execute("UPDATE users SET email = 'farmer@b.example' WHERE user_id = 3")
    _age(db, mine, delivery_end=D(-1))
    _age(db, theirs, delivery_end=D(-1))
    assert data.expire_all_tenants(db) == 2
    assert [r["to_email"] for r in db.query("SELECT to_email FROM notifications WHERE kind = 'offer_expired' AND tenant_id = 'rcm'")] == ["farmer@a.example"]
    assert data.expire_all_tenants(db) == 0
    assert _status(db, mine) == "expired"
    notify.send_pending(db, send=lambda *a: None, limit=100)
    assert db.query("SELECT COUNT(*) AS n FROM notifications WHERE kind = 'offer_expired' AND status = 'sent'")[0]["n"] == 2


def test_an_offer_whose_period_ended_is_expired_never_hit(db):
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", basis_cents=-20, cash_price=4.50)
    data.add_auto_hit_rule(db, "rcm", 2)
    oid = _offer(db, delivery_end=D(5))                                                  # basis -30 <= bid -20: would be reached
    assert _status(db, oid) == "submitted"                                                # (its basis is better than the bid's)
    cheap = data.create_offer(db, "rcm", user(db, 1), **{**HTA, "basis_cents": -40}, delivery_end=D(5))
    _age(db, cheap, delivery_end=D(-1))
    assert data.run_auto_hit(db, "rcm") == 0 and _status(db, cheap) == "expired"


def test_a_delivery_period_that_has_already_ended_cannot_be_offered(db):
    with pytest.raises(PortalError):
        _offer(db, delivery_end=D(-1))
    with pytest.raises(PortalError):
        _offer(db, delivery_start=(TODAY.replace(day=1) - timedelta(days=5)).isoformat())     # start in a past month, no end
    _offer(db, delivery_end=D(0))                                                          # the last day itself is still fine


# ── bids and scrapes carry a delivery range ──────────────────────────────────
def test_a_bid_with_no_dates_covers_its_whole_month_and_a_backwards_range_is_refused(db):
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", basis_cents=-38)
    b = data.list_bids(db, "rcm")[0]
    assert (str(b["window_start"]), str(b["window_end"])) == ("2999-12-01", "2999-12-31")
    with pytest.raises(PortalError):
        data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", basis_cents=-38,
                         delivery_start="2999-12-20", delivery_end="2999-12-10")
    with pytest.raises(PortalError):
        data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="December", basis_cents=-38)


def test_agricharts_gives_a_clear_range_only_when_the_feed_has_one():
    feed = [{"name": "Riverton", "cashbids": [
        {"name": "Corn", "symbol": "ZCZ26", "basis": -38, "delivery_start": "09/01/2999", "delivery_end": "11/30/2999"},
        {"name": "Corn", "symbol": "ZCH27", "basis": -30, "delivery_start": "03/01/2999"},                       # no end
        {"name": "Corn", "symbol": "ZCK27", "basis": -25, "delivery_start": "05/01/2999", "delivery_end": "04/01/2999"}]}]  # backwards
    out = scrapers.parse_agricharts(feed)
    assert [(o["delivery_start"], o["delivery_end"]) for o in out] == [("2999-09-01", "2999-11-30"), (None, None), (None, None)]
    assert [o["delivery_month"] for o in out] == ["2999-09", "2999-03", "2999-05"]


def test_bushel_labels_are_a_clear_range_only_for_a_plain_month():
    f = scrapers._bushel_range
    assert f("January 2027") == ("2027-01-01", "2027-01-31") and f("Mar 2027") == ("2027-03-01", "2027-03-31")
    for unclear in ("Harvest 2026", "New Crop 2027", "Dec/Jan 2027", "Spring 2027", "2027", "", "Jan 5 - 20 2027"):
        assert f(unclear) == (None, None), unclear


def test_a_scrape_without_a_range_is_staged_unclear_and_cannot_be_approved_until_staff_enter_one(db):
    db.execute("INSERT INTO locations (location_id, tenant_id, name) VALUES (1, 'rcm', 'Riverton')")
    items = [dict(location_name="Riverton", grain="Corn", delivery_month="2999-10", futures_symbol="ZCZ99", basis_cents=-30,
                  cash_price=4.1, delivery_start=None, delivery_end=None),                                     # "Harvest": unclear
             dict(location_name="Riverton", grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ98", basis_cents=-25,
                  cash_price=4.2, delivery_start="2999-12-01", delivery_end="2999-12-31")]                      # clear
    assert data.sync_suggestions(db, "rcm", "t", items)["staged"] == 2
    unclear, clear = sorted(data.list_suggestions(db, "rcm"), key=lambda s: s["delivery_month"])
    assert unclear["delivery_start"] is None and str(clear["delivery_start"]) == "2999-12-01"
    with pytest.raises(PortalError):
        data.review_suggestion(db, "rcm", 2, unclear["suggestion_id"], True)                                    # no range: refused
    with pytest.raises(PortalError):
        data.review_suggestion(db, "rcm", 2, unclear["suggestion_id"], True, delivery_start="2999-10-01")        # half a range
    with pytest.raises(PortalError):
        data.review_suggestion(db, "rcm", 2, unclear["suggestion_id"], True, delivery_start="2999-10-31", delivery_end="2999-10-01")
    data.review_suggestion(db, "rcm", 2, unclear["suggestion_id"], True, delivery_start="2999-09-15", delivery_end="2999-11-15")
    data.review_suggestion(db, "rcm", 2, clear["suggestion_id"], True)                                          # a clear one needs nothing
    got = {b["delivery_month"]: (str(b["window_start"]), str(b["window_end"])) for b in data.list_bids(db, "rcm")}
    assert got == {"2999-10": ("2999-09-15", "2999-11-15"), "2999-12": ("2999-12-01", "2999-12-31")}
    assert data.list_suggestions(db, "rcm") == []


def test_discarding_an_unclear_suggestion_needs_no_range(db):
    data.stage_suggestion(db, "rcm", grain="Corn", delivery_month="2999-10", source="x", basis_cents=-30)
    sid = data.list_suggestions(db, "rcm")[0]["suggestion_id"]
    data.review_suggestion(db, "rcm", 2, sid, False)
    assert data.list_suggestions(db, "rcm") == []


def test_a_nonsense_range_from_a_scraper_is_staged_as_unclear(db):
    data.stage_suggestion(db, "rcm", grain="Corn", delivery_month="2999-10", source="x", basis_cents=-30,
                          delivery_start="2999-10-31", delivery_end="2999-10-01")
    s = data.list_suggestions(db, "rcm")[0]
    assert s["delivery_start"] is None and s["delivery_end"] is None


# ── on screen ────────────────────────────────────────────────────────────────
def test_outstanding_offers_show_when_they_expire_and_the_bid_board_shows_the_window(app_db):
    data.publish_bid(app_db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", location_id=1,
                     basis_cents=-30, cash_price=4.2, delivery_start="2999-12-01", delivery_end="2999-12-20")
    bid = data.list_bids(app_db, "rcm")[0]
    data.create_offer(app_db, "rcm", user(app_db, 1), **HTA, bid_id=bid["bid_id"])
    at = _as(app_db, 2)
    assert any("Expires after Dec 20, 2999" in c.value for c in at.caption)
    goto(at, "Bid board")
    assert not at.exception, at.exception
    assert list(at.dataframe[0].value["Delivery window"]) == ["Dec 01 – Dec 20, 2999"]


def test_the_offer_form_requires_a_delivery_period_when_there_is_no_bid(app_db):
    at = _as(app_db, 1)
    goto(at, "Make an offer")
    next(b for b in at.button if b.label == "Send offer").click().run()
    assert at.error and "delivery period" in at.error[0].value.lower()
    assert data.list_offers(app_db, "rcm", user(app_db, 1)) == []
