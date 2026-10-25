import pytest
from portal import data, farmers
from portal.data import PortalError
from conftest import user

TERMS = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=5000,
             basis_cents=-30, futures_symbol="ZCZ26")


def _f(db, fid):
    return [x for x in farmers.list_farmers(db, "rcm", 2, include_inactive=True) if x["farmer_id"] == fid][0]


def test_farmer_has_client_id_entity_and_individual_name(db):
    fid = farmers.create_farmer(db, "rcm", 2, "Pat Farmer", "555-1212", entity_name="Farmer Bros LLC",
                                client_id=" 10442 ")
    f = _f(db, fid)
    assert (f["client_id"], f["entity_name"], f["individual_name"], f["name"]) == \
        ("10442", "Farmer Bros LLC", "Pat Farmer", "Farmer Bros LLC (Pat Farmer)")
    only_entity = farmers.create_farmer(db, "rcm", 2, entity_name="Smith Farms", client_id="20001")
    assert _f(db, only_entity)["name"] == "Smith Farms"
    only_person = farmers.create_farmer(db, "rcm", 2, "Joe Solo")
    assert (_f(db, only_person)["name"], _f(db, only_person)["client_id"]) == ("Joe Solo", None)


def test_a_name_is_required_and_client_id_is_unique_per_tenant(db):
    with pytest.raises(PortalError):
        farmers.create_farmer(db, "rcm", 2, " ", entity_name=" ")                  # no name at all
    farmers.create_farmer(db, "rcm", 2, "A", client_id="C-1")
    with pytest.raises(PortalError):
        farmers.create_farmer(db, "rcm", 2, "B", client_id="c-1")                  # case-insensitive duplicate
    with pytest.raises(PortalError):
        farmers.create_farmer(db, "rcm", 2, "C", client_id="x" * 41)
    farmers.create_farmer(db, "dorans", 4, "Other tenant", client_id="C-1")        # same ID, other elevator: fine
    farmers.create_farmer(db, "rcm", 2, "D")                                       # blank IDs never collide
    farmers.create_farmer(db, "rcm", 2, "E")


def test_update_farmer_keeps_client_id_unique(db):
    a = farmers.create_farmer(db, "rcm", 2, "A", client_id="C-1")
    b = farmers.create_farmer(db, "rcm", 2, "B", client_id="C-2")
    farmers.update_farmer(db, "rcm", 2, a, "A Person", None, None, entity_name="A Farms", client_id="C-1")  # own ID ok
    assert _f(db, a)["name"] == "A Farms (A Person)"
    with pytest.raises(PortalError):
        farmers.update_farmer(db, "rcm", 2, b, "B", None, None, client_id="c-1")
    farmers.update_farmer(db, "rcm", 2, b, "B", None, None)                                # clearing it is fine
    assert _f(db, b)["client_id"] is None


def test_offers_carry_farmer_identity_notes_and_contract_ref(db):
    fid = farmers.create_farmer(db, "rcm", 2, "Pat Farmer", entity_name="Farmer Bros LLC", client_id="10442")
    oid = data.place_offer_for_farmer(db, "rcm", 2, fid, **TERMS, note="  Call before delivery ", contract_ref=" HTA-2026-118 ")
    o = [x for x in data.list_offers(db, "rcm", user(db, 2)) if x["offer_id"] == oid][0]
    assert (o["note"], o["contract_ref"], o["farmer_client_id"], o["farmer_entity"], o["farmer_individual"]) == \
        ("Call before delivery", "HTA-2026-118", "10442", "Farmer Bros LLC", "Pat Farmer")


def test_farmer_can_add_note_and_contract_ref_to_own_offer(db):
    oid = data.create_offer(db, "rcm", user(db, 1), **TERMS, note="Bins are full after the 20th", contract_ref="MY-REF")
    o = data.list_offers(db, "rcm", user(db, 1))[0]
    assert (o["offer_id"], o["note"], o["contract_ref"]) == (oid, "Bins are full after the 20th", "MY-REF")
    for bad in ({"note": "x" * 501}, {"contract_ref": "r" * 61}):
        with pytest.raises(PortalError):
            data.create_offer(db, "rcm", user(db, 1), **TERMS, **bad)


def test_staff_edit_note_and_contract_ref_any_time_and_it_is_logged(db):
    oid = data.create_offer(db, "rcm", user(db, 1), **TERMS, note="first")
    data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")
    data.update_offer_details(db, "rcm", 2, oid, note="first; confirmed by phone", contract_ref="C-5531")
    o = data.list_offers(db, "rcm", user(db, 2))[0]
    assert (o["status"], o["note"], o["contract_ref"], o["bushels"]) == \
        ("hit", "first; confirmed by phone", "C-5531", 5000)               # status and terms untouched
    ev = db.query("SELECT detail, actor_user_id FROM offer_events WHERE offer_id = %s AND event_type = 'details_updated'",
                  (oid,))[0]
    assert ev["actor_user_id"] == 2 and "C-5531" in ev["detail"] and "first" in ev["detail"]
    data.update_offer_details(db, "rcm", 2, oid, note=None, contract_ref=None)           # clearing is allowed
    assert data.list_offers(db, "rcm", user(db, 2))[0]["contract_ref"] is None


def test_only_tenant_staff_may_edit_offer_details(db):
    oid = data.create_offer(db, "rcm", user(db, 1), **TERMS)
    for uid in (1, 4):                                                    # the farmer; another tenant's admin
        with pytest.raises(PortalError):
            data.update_offer_details(db, "rcm", uid, oid, note="x", contract_ref="y")
    with pytest.raises(PortalError):
        data.update_offer_details(db, "dorans", 4, oid, note="x", contract_ref="y")   # offer isn't theirs
    with pytest.raises(PortalError):
        data.update_offer_details(db, "rcm", 2, 9999, note="x", contract_ref="y")
