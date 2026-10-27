"""The offer flow: Outstanding -> Accept Offer -> Working -> (the bid reaches it) Filled -> Confirm Purchase -> Completed."""
from datetime import datetime, timedelta, timezone

import pytest

from portal import data, farmers, futures, purchases
from portal.data import PortalError
from conftest import user

FLAT = dict(grain="Corn", contract_type="CASH", price_type="FIXED", bushels=5000, price=4.30, futures_symbol="ZCZ26")
HTA = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=5000, basis_cents=-30, futures_symbol="ZCZ26")


@pytest.fixture
def staff_db(db):
    db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 's@rcm.com', 'elevator_staff')")
    return db


def _bid(db, cash=4.18, basis=-38):
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26",
                     basis_cents=basis, cash_price=cash)
    return data.list_bids(db, "rcm")[0]["bid_id"]


def _offer(db, **kw):
    return data.create_offer(db, "rcm", user(db, 1), **{**FLAT, **kw}, bid_id=_bid_id(db))


def _bid_id(db):
    return data.list_bids(db, "rcm")[0]["bid_id"]


def _status(db, oid):
    return db.query("SELECT status FROM offers WHERE offer_id = %s", (oid,))[0]["status"]


def test_an_entered_offer_stays_outstanding_even_when_the_bid_is_at_its_price(db):
    _bid(db, cash=4.50)
    oid = _offer(db)
    assert _status(db, oid) == "submitted"                                            # nobody accepted it: nothing fills it
    assert data.run_auto_hit(db, "rcm") == 0 and _status(db, oid) == "submitted"
    assert [o["bid_reached"] for o in data.list_offers(db, "rcm", user(db, 2))] == [True]    # but it is flagged: accept it now


def test_accept_makes_it_working_and_the_bid_reaching_it_fills_it_by_itself(staff_db):
    db = staff_db
    _bid(db, cash=4.18)
    oid = _offer(db)
    assert data.act_on_offer(db, "rcm", user(db, 20), oid, "accept") == "working"
    assert [e["event_type"] for e in db.query("SELECT event_type FROM offer_events WHERE offer_id = %s ORDER BY event_id", (oid,))][-1] == "accepted"
    assert data.run_auto_hit(db, "rcm") == 0                                          # 4.18 < 4.30: still working
    _bid(db, cash=4.40)                                                               # a better bid is posted: reaches 4.30
    assert _status(db, oid) == "hit"
    o = [x for x in data.list_offers(db, "rcm", user(db, 2)) if x["offer_id"] == oid][0]
    assert o["auto_hit"] and o["hit_by"] is None                                      # filled by the market, not by a person


def test_accepting_an_offer_the_bid_has_already_reached_fills_it_at_once(db):
    _bid(db, cash=4.50)
    oid = _offer(db)
    assert data.act_on_offer(db, "rcm", user(db, 2), oid, "accept") == "hit"
    assert _status(db, oid) == "hit"


def test_only_elevator_staff_accept_and_a_working_offer_cannot_be_accepted_again(db):
    _bid(db)
    oid = _offer(db)
    with pytest.raises(PortalError):
        data.act_on_offer(db, "rcm", user(db, 1), oid, "accept")                      # a farmer can't accept their own offer
    data.act_on_offer(db, "rcm", user(db, 2), oid, "accept")
    with pytest.raises(PortalError):
        data.act_on_offer(db, "rcm", user(db, 2), oid, "accept")


def test_a_working_offer_can_be_withdrawn_or_declined_and_futures_only_offers_never_fill_by_themselves(db):
    _bid(db, cash=4.50)
    a, b = _offer(db), _offer(db)
    data.act_on_offer(db, "rcm", user(db, 2), a, "accept")                            # fills at once...
    futs = data.create_offer(db, "rcm", user(db, 1), **{**FLAT, "contract_type": "FUTURES_ONLY", "price": 4.55}, bid_id=_bid_id(db))
    assert data.act_on_offer(db, "rcm", user(db, 2), futs, "accept") == "working"     # ...a futures-only one has no bid to reach
    assert data.run_auto_hit(db, "rcm") == 0 and _status(db, futs) == "working"
    assert data.act_on_offer(db, "rcm", user(db, 1), futs, "withdraw") == "withdrawn"
    data.act_on_offer(db, "rcm", user(db, 2), b, "accept")
    assert _status(db, b) == "hit"
    c = _offer(db, price=9.99)
    data.act_on_offer(db, "rcm", user(db, 2), c, "accept")
    assert data.act_on_offer(db, "rcm", user(db, 2), c, "decline") == "declined"


def test_working_offers_follow_the_basis_expire_and_count_as_open_for_the_hedge_view(db):
    _bid(db, basis=-38)
    oid = _offer(db)
    data.act_on_offer(db, "rcm", user(db, 2), oid, "accept")
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", basis_cents=-30, cash_price=4.20)
    o = [x for x in data.list_offers(db, "rcm", user(db, 2)) if x["offer_id"] == oid][0]
    assert o["implied_futures"] == 4.60 and o["basis_ref_cents"] == -30                # re-priced while it works
    assert [l["offers"] for l in data.hedge_summary(db, "rcm", 2)["potential"]] == [1]
    db.execute("UPDATE offers SET delivery_end = '2000-01-01' WHERE offer_id = %s", (oid,))
    assert data.expire_stale(db, "rcm") == 1 and _status(db, oid) == "expired"


def test_confirm_purchase_needs_the_contract_number_first_and_then_does_both_steps(staff_db):
    db = staff_db
    _bid(db, cash=4.50)
    oid = _offer(db)
    data.act_on_offer(db, "rcm", user(db, 2), oid, "accept")
    for bad in ("", "  ", None):
        with pytest.raises(PortalError):
            data.confirm_purchase(db, "rcm", 20, oid, bad)
    assert _status(db, oid) == "hit" and purchases.list_purchases(db, "rcm", user(db, 20)) == []      # nothing half-done
    data.confirm_purchase(db, "rcm", 20, oid, "C-4001")
    o = db.query("SELECT status, contract_ref, acknowledged_by, completed_by FROM offers WHERE offer_id = %s", (oid,))[0]
    assert (o["status"], o["contract_ref"], o["acknowledged_by"], o["completed_by"]) == ("completed", "C-4001", 20, 20)
    (p,) = purchases.list_purchases(db, "rcm", user(db, 20))
    assert (p["offer_id"], p["contract_ref"], p["staff_user_id"]) == (oid, "C-4001", 20)
    kinds = [e["event_type"] for e in db.query("SELECT event_type FROM offer_events WHERE offer_id = %s ORDER BY event_id", (oid,))]
    assert kinds[-3:] == ["hit", "acknowledged", "completed"]
    with pytest.raises(PortalError):
        data.confirm_purchase(db, "rcm", 20, oid, "C-4002")                             # already done


def test_only_a_filled_offer_can_be_confirmed_and_a_confirmed_offer_without_a_number_just_completes(staff_db):
    db = staff_db
    _bid(db)
    oid = _offer(db)
    for early in ("submitted", "working"):
        if early == "working":
            data.act_on_offer(db, "rcm", user(db, 2), oid, "accept")
        with pytest.raises(PortalError):
            data.confirm_purchase(db, "rcm", 20, oid, "C-1")
    data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")
    data.acknowledge_offer(db, "rcm", 20, oid)                                            # a legacy half-way offer
    data.confirm_purchase(db, "rcm", 20, oid, "C-9")
    assert _status(db, oid) == "completed" and len(purchases.list_purchases(db, "rcm", user(db, 20))) == 1


def test_the_hedge_list_still_has_a_completed_purchase_until_its_hedge_is_acknowledged(staff_db):
    db = staff_db
    _bid(db, cash=4.50)
    oid = _offer(db)
    data.act_on_offer(db, "rcm", user(db, 2), oid, "accept")
    data.confirm_purchase(db, "rcm", 20, oid, "C-1")                                       # completed straight away
    h = data.hedge_summary(db, "rcm", 2)
    assert [o["offer_id"] for o in h["now_offers"]] == [oid] and h["position"][0]["todo_contracts"] == 1.0
    assert data.mark_hedged(db, "rcm", 2, [oid]) == 1
    h = data.hedge_summary(db, "rcm", 2)
    assert h["now_offers"] == [] and h["position"][0]["hedged_contracts"] == 1.0


def test_ct_date_reads_utc_text_and_datetimes_in_central_time():
    assert str(data.ct_date("2026-10-26 02:30:00")) == "2026-10-25"                        # 9:30 pm Central the day before
    assert str(data.ct_date(datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc))) == "2026-10-26"
    assert data.ct_date(None) is None and data.ct_date("") is None
