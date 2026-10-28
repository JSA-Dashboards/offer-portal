import pytest

from portal import data

from portal.data import PortalError

from conftest import user



OFFER = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=5000,

             basis_cents=-30, futures_symbol="ZCZ26")





def test_tenant_cannot_see_other_tenant_data(db):

    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2026-12", basis_cents=-38, futures_symbol="ZCZ26")

    assert len(data.list_bids(db, "rcm")) == 1

    assert data.list_bids(db, "dorans") == []





def test_farmer_cannot_offer_into_another_tenant(db):

    with pytest.raises(PortalError):

        data.create_offer(db, "dorans", user(db, 1), **OFFER)





def test_suggestions_are_hidden_until_approved(db):

    data.stage_suggestion(db, "rcm", grain="Corn", delivery_month="2026-12", source="agricharts", basis_cents=-38)

    assert data.list_bids(db, "rcm") == []

    sid = data.list_suggestions(db, "rcm")[0]["suggestion_id"]

    with pytest.raises(PortalError):                      # another tenant's admin can't review it

        data.review_suggestion(db, "dorans", 4, sid, True)

    with pytest.raises(PortalError):                      # the source gave no delivery range: staff must enter one
        data.review_suggestion(db, "rcm", 2, sid, True, basis_cents=-35)
    with pytest.raises(PortalError):                      # ...and the futures month the bid hedges against
        data.review_suggestion(db, "rcm", 2, sid, True, basis_cents=-35, delivery_start="2026-12-01",
                               delivery_end="2026-12-31")
    data.review_suggestion(db, "rcm", 2, sid, True, basis_cents=-35, delivery_start="2026-12-01",
                           delivery_end="2026-12-31", futures_symbol="ZCZ26")   # admin edits then approves

    assert data.list_bids(db, "rcm")[0]["basis_cents"] == -35

    assert data.list_suggestions(db, "rcm") == []





def test_offer_validation(db):

    f = user(db, 1)

    with pytest.raises(PortalError):

        data.create_offer(db, "rcm", f, **{**OFFER, "contract_type": "SWAP"})

    with pytest.raises(PortalError):

        data.create_offer(db, "rcm", f, **{**OFFER, "futures_symbol": None})

    with pytest.raises(PortalError):

        data.create_offer(db, "rcm", f, **{**OFFER, "basis_cents": None})                 # basis only needs a basis

    with pytest.raises(PortalError):

        data.create_offer(db, "rcm", f, **{**OFFER, "contract_type": "FUTURES_ONLY", "basis_cents": None})   # needs a futures price

    with pytest.raises(PortalError):

        data.create_offer(db, "rcm", f, **{**OFFER, "contract_type": "CASH", "basis_cents": -30})            # cash needs both prices





def test_open_pricing_off_requires_posted_bid(db):

    db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (9, 'closed', 'f@c.com', 'farmer')")

    with pytest.raises(PortalError):

        data.create_offer(db, "closed", user(db, 9), **OFFER)





def test_hit_creates_confirmation_and_usage(db):
    f, a = user(db, 1), user(db, 2)
    oid = data.create_offer(db, "rcm", f, **OFFER)
    with pytest.raises(PortalError):                       # a farmer can't accept their own offer
        data.act_on_offer(db, "rcm", f, oid, "hit")
    assert data.act_on_offer(db, "rcm", a, oid, "hit") == "hit"
    with pytest.raises(PortalError):                       # nothing more can happen to it
        data.act_on_offer(db, "rcm", a, oid, "decline")
    assert db.query("SELECT COUNT(*) AS n FROM confirmations WHERE tenant_id='rcm'")[0]["n"] == 1
    types = {r["event_type"] for r in data.usage_by_tenant(db, user(db, 5), db.query(
        "SELECT SUBSTR(CAST(created_at AS VARCHAR),1,7) AS m FROM usage_events LIMIT 1")[0]["m"])}
    assert {"offer_submitted", "offer_hit"} <= types


def test_there_are_no_counter_offers(db):
    oid = data.create_offer(db, "rcm", user(db, 1), **OFFER)
    for uid in (1, 2, 5):                                  # farmer, elevator admin, platform admin
        with pytest.raises(PortalError):
            data.act_on_offer(db, "rcm", user(db, uid), oid, "counter")
    assert data.list_offers(db, "rcm", user(db, 2))[0]["status"] == "submitted"
    assert data.act_on_offer(db, "rcm", user(db, 2), oid, "decline") == "declined"


def test_other_tenant_admin_cannot_touch_offer(db):

    oid = data.create_offer(db, "rcm", user(db, 1), **OFFER)

    with pytest.raises(PortalError):

        data.act_on_offer(db, "rcm", user(db, 4), oid, "hit")





def test_farmer_sees_only_own_offers(db):

    db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (10, 'rcm', 'f2@a.com', 'farmer')")

    data.create_offer(db, "rcm", user(db, 1), **OFFER)

    data.create_offer(db, "rcm", user(db, 10), **OFFER)

    assert len(data.list_offers(db, "rcm", user(db, 1))) == 1

    assert len(data.list_offers(db, "rcm", user(db, 2))) == 2





def test_usage_report_is_platform_only(db):

    with pytest.raises(PortalError):

        data.usage_by_tenant(db, user(db, 2), "2026-10")





def test_only_tenant_staff_can_write_the_bid_board(db):

    for uid in (1, 4):                                    # rcm farmer; dorans admin

        with pytest.raises(PortalError):

            data.publish_bid(db, "rcm", uid, grain="Corn", delivery_month="2026-12", basis_cents=-38, futures_symbol="ZCZ26")

    data.publish_bid(db, "rcm", 5, grain="Corn", delivery_month="2026-12", basis_cents=-38, futures_symbol="ZCZ26")  # platform admin ok

    bid = data.list_bids(db, "rcm")[0]["bid_id"]

    with pytest.raises(PortalError):

        data.retire_bid(db, "rcm", 4, bid)

    data.retire_bid(db, "rcm", 2, bid)

    assert data.list_bids(db, "rcm") == []





def test_expire_stale_and_gap(db):

    f = user(db, 1)

    oid = data.create_offer(db, "rcm", f, **OFFER, expires_at="2000-01-01 00:00:00")

    keep = data.create_offer(db, "rcm", f, **OFFER, expires_at="2999-01-01 00:00:00")

    assert data.expire_stale(db, "rcm") == 1

    st = {o["offer_id"]: o["status"] for o in data.list_offers(db, "rcm", f)}

    assert st[oid] == "expired" and st[keep] == "submitted"

    assert data.expire_stale(db, "dorans") == 0

    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2026-12", basis_cents=-38, futures_symbol="ZCZ26")

    bid = data.list_bids(db, "rcm")[0]["bid_id"]

    o2 = data.create_offer(db, "rcm", f, **OFFER, bid_id=bid)

    got = [o for o in data.list_offers(db, "rcm", f) if o["offer_id"] == o2][0]

    assert data.bid_gap(got) == "+8¢ vs posted basis"          # offered -30 vs posted -38

