"""Who may do what: platform admin > elevator admin > elevator staff > farmer."""
import pytest
from portal import data, farmers, purchases
from portal.data import PortalError
from conftest import user

BID = dict(grain="Corn", delivery_month="2999-12", basis_cents=-38, futures_symbol="ZCZ26")


@pytest.fixture
def staff_db(db):
    db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 'staff@rcm.com', 'elevator_staff')")
    return db


def test_only_admins_change_the_bid_board(staff_db):
    db = staff_db
    for uid in (1, 20):                                   # farmer, staff
        with pytest.raises(PortalError):
            data.publish_bid(db, "rcm", uid, **BID)
    data.publish_bid(db, "rcm", 2, **BID)                 # elevator admin
    data.publish_bid(db, "rcm", 5, **{**BID, "grain": "Soybeans", "futures_symbol": "ZSX26"})   # JSA platform admin
    bid = data.list_bids(db, "rcm")[0]["bid_id"]
    for uid in (1, 20):
        with pytest.raises(PortalError):
            data.retire_bid(db, "rcm", uid, bid)
    data.retire_bid(db, "rcm", 2, bid)
    data.stage_suggestion(db, "rcm", grain="Corn", delivery_month="2999-12", source="x", basis_cents=-30)
    sid = data.list_suggestions(db, "rcm")[0]["suggestion_id"]
    with pytest.raises(PortalError):
        data.review_suggestion(db, "rcm", 20, sid, False)
    data.review_suggestion(db, "rcm", 2, sid, False)


def test_staff_work_offers_farmers_and_purchases_but_not_hedging(staff_db):
    db = staff_db
    data.publish_bid(db, "rcm", 2, **BID)
    fid = farmers.create_farmer(db, "rcm", 20, "Pat Farmer", "555")
    oid = data.place_offer_for_farmer(db, "rcm", 20, fid, grain="Corn", contract_type="HTA", price_type="BASIS",
                                      bushels=5000, basis_cents=-30, futures_symbol="ZCZ26")
    assert data.list_offers(db, "rcm", user(db, 20))[0]["offer_id"] == oid          # staff see the company's offers
    purchases.add_purchase(db, "rcm", 20, grain="Corn", bushels=5000, counterparty="Smith", price=4.2)
    for fn in (lambda: data.hedge_summary(db, "rcm", 20), lambda: data.mark_hedged(db, "rcm", 20, [oid])):
        with pytest.raises(PortalError):
            fn()
    data.hedge_summary(db, "rcm", 2)                      # admin and
    data.hedge_summary(db, "rcm", 5)                      # platform admin can


def test_a_farmer_sees_only_their_own_offers_old_and_new(staff_db):
    db = staff_db
    data.publish_bid(db, "rcm", 2, **BID)
    bid = data.list_bids(db, "rcm")[0]["bid_id"]
    mine = data.create_offer(db, "rcm", user(db, 1), grain="Corn", contract_type="HTA", price_type="BASIS",
                             bushels=5000, basis_cents=-30, futures_symbol="ZCZ26", bid_id=bid)
    data.act_on_offer(db, "rcm", user(db, 1), mine, "withdraw")                       # a historical offer
    data.create_offer(db, "rcm", user(db, 1), grain="Corn", contract_type="HTA", price_type="BASIS",
                      bushels=5000, basis_cents=-31, futures_symbol="ZCZ26", bid_id=bid)
    db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (30, 'rcm', 'other@a.com', 'farmer')")
    assert len(data.list_offers(db, "rcm", user(db, 1))) == 2
    assert data.list_offers(db, "rcm", user(db, 30)) == []
