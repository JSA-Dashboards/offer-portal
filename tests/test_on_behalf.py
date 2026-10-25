import pytest
from portal import data, farmers, people
from portal.data import PortalError
from conftest import user

TERMS = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=5000,
             basis_cents=-30, futures_symbol="ZCZ26")


def test_staff_create_and_list_farmers(db):
    fid = farmers.create_farmer(db, "rcm", 2, " Pat Farmer ", "555-1212", "PAT@x.com")
    f = [x for x in farmers.list_farmers(db, "rcm", 2) if x["farmer_id"] == fid][0]
    assert (f["name"], f["phone"], f["email"], f["user_id"], f["open_offers"]) == \
        ("Pat Farmer", "555-1212", "pat@x.com", None, 0)
    with pytest.raises(PortalError):
        farmers.create_farmer(db, "rcm", 2, "pat farmer", "555-1212")      # duplicate name+phone
    farmers.create_farmer(db, "rcm", 2, "Pat Farmer", "555-9999")           # same name, other phone is fine
    for bad in (lambda: farmers.create_farmer(db, "rcm", 1, "X"),            # a farmer can't
                lambda: farmers.create_farmer(db, "rcm", 4, "X"),            # other tenant's admin can't
                lambda: farmers.create_farmer(db, "rcm", 2, " "),
                lambda: farmers.create_farmer(db, "rcm", 2, "X", None, "nope")):
        with pytest.raises(PortalError):
            bad()
    assert all(x["farmer_id"] != fid for x in farmers.list_farmers(db, "dorans", 4))   # tenant-scoped


def test_staff_place_offer_for_farmer_without_login(db):
    fid = farmers.create_farmer(db, "rcm", 2, "Phone Caller", "555-0000")
    oid = data.place_offer_for_farmer(db, "rcm", 2, fid, **TERMS)
    o = [x for x in data.list_offers(db, "rcm", user(db, 2)) if x["offer_id"] == oid][0]
    assert (o["status"], o["farmer_name"], o["farmer_phone"], o["placed_by"], o["farmer_user_id"]) == \
        ("submitted", "Phone Caller", "555-0000", 2, None)
    ev = db.query("SELECT detail, actor_user_id FROM offer_events WHERE offer_id = %s", (oid,))[0]
    assert ev["actor_user_id"] == 2 and '"on_behalf": true' in ev["detail"]
    assert [x["open_offers"] for x in farmers.list_farmers(db, "rcm", 2) if x["farmer_id"] == fid] == [1]


def test_placement_permissions_and_tenant_scope(db):
    fid = farmers.create_farmer(db, "rcm", 2, "Pat")
    for uid, tenant, f in ((1, "rcm", fid), (4, "rcm", fid), (4, "dorans", fid)):
        with pytest.raises(PortalError):
            data.place_offer_for_farmer(db, tenant, uid, f, **TERMS)
    with pytest.raises(PortalError):
        data.place_offer_for_farmer(db, "rcm", 2, 9999, **TERMS)             # no such farmer
    farmers.set_farmer_active(db, "rcm", 2, fid, False)
    with pytest.raises(PortalError):
        data.place_offer_for_farmer(db, "rcm", 2, fid, **TERMS)             # inactive farmer
    with pytest.raises(PortalError):                                         # same validation as self-placed
        data.place_offer_for_farmer(db, "rcm", 2, farmers.create_farmer(db, "rcm", 2, "Q"),
                                    **{**TERMS, "futures_symbol": None})


def test_staff_accept_an_offer_placed_for_a_phone_farmer(db):
    fid = farmers.create_farmer(db, "rcm", 2, "Phone Caller")
    oid = data.place_offer_for_farmer(db, "rcm", 2, fid, **TERMS)
    assert data.act_on_offer(db, "rcm", user(db, 2), oid, "hit") == "hit"
    assert db.query("SELECT COUNT(*) AS n FROM confirmations WHERE offer_id = %s", (oid,))[0]["n"] == 1


def test_on_behalf_is_staff_only_and_only_for_withdrawals(db):
    fid = farmers.create_farmer(db, "rcm", 2, "Phone Caller")
    oid = data.place_offer_for_farmer(db, "rcm", 2, fid, **TERMS)
    with pytest.raises(PortalError):                                         # staff can't accept AS the farmer
        data.act_on_offer(db, "rcm", user(db, 2), oid, "hit", on_behalf=True)
    with pytest.raises(PortalError):
        data.act_on_offer(db, "rcm", user(db, 1), oid, "withdraw", on_behalf=True)   # farmer can't use it
    assert data.act_on_offer(db, "rcm", user(db, 2), oid, "withdraw", on_behalf=True) == "withdrawn"
    ev = db.query("SELECT detail FROM offer_events WHERE offer_id = %s AND event_type = 'withdrawn'", (oid,))[0]
    assert '"on_behalf": true' in ev["detail"]


def test_invite_links_login_to_existing_farmer_and_shows_staff_placed_offers(db):
    fid = farmers.create_farmer(db, "rcm", 2, "Pat Farmer", "555-1212", "pat@x.com")
    oid = data.place_offer_for_farmer(db, "rcm", 2, fid, **TERMS)
    tok = people.create_invite(db, "rcm", 2, "pat@x.com", farmer_id=fid)
    u = people.accept_invite(db, tok, "rcm", "Pat")
    assert [o["offer_id"] for o in data.list_offers(db, "rcm", u)] == [oid]   # sees the offer staff placed
    assert farmers.list_farmers(db, "rcm", 2)[0]["user_id"] == u["user_id"] or \
        any(x["user_id"] == u["user_id"] for x in farmers.list_farmers(db, "rcm", 2))
    assert data.act_on_offer(db, "rcm", u, oid, "withdraw") == "withdrawn"           # the farmer now acts for themselves
    with pytest.raises(PortalError):
        people.create_invite(db, "rcm", 2, "other@x.com", farmer_id=fid)      # already has a login
    with pytest.raises(PortalError):
        people.create_invite(db, "rcm", 2, "o2@x.com", farmer_id=9999)


def test_self_signup_creates_a_farmer_record_and_self_offers_are_not_on_behalf(db):
    tok = people.create_invite(db, "rcm", 2, "new@x.com")
    u = people.accept_invite(db, tok, "rcm", "New Person")
    assert any(x["user_id"] == u["user_id"] for x in farmers.list_farmers(db, "rcm", 2))
    oid = data.create_offer(db, "rcm", u, **TERMS)
    o = data.list_offers(db, "rcm", u)[0]
    assert o["offer_id"] == oid and o["placed_by"] is None and o["farmer_name"] == "New Person"
    other = user(db, 1)
    assert data.list_offers(db, "rcm", other) == []                          # farmers never see each other's offers
