from conftest import goto, pages  # noqa: F401
from portal import data, farmers
from test_app import _as, app_db  # noqa: F401  (fixture + signed-in session helper)
import conftest


def _go(at, view):
    goto(at, view)
    assert not at.exception, at.exception
    return at


def test_staff_view_lists_the_new_screens(app_db):
    at = _as(app_db, 2)
    assert {"Place offer", "Farmers"} <= set(pages(at))
    assert not {"Place offer", "Farmers"} & set(pages(_as(app_db, 1)))       # farmers don't get them


def test_staff_adds_farmer_then_places_offer_for_them(app_db):
    data.publish_bid(app_db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", location_id=1,
                     basis_cents=-30, cash_price=4.2)
    at = _go(_as(app_db, 2), "Place offer")
    f = {t.label: t for t in at.text_input}
    f["Client ID (your account number)"].set_value("10442")
    f["Entity name"].set_value("Farmer Bros LLC")
    f["Individual name"].set_value("Pat Farmer")
    f["Phone"].set_value("555-0000")
    next(b for b in at.button if b.label == "Add farmer").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    got = farmers.list_farmers(app_db, "rcm", 2)[0]
    assert (got["name"], got["client_id"], got["entity_name"], got["individual_name"]) == \
        ("Farmer Bros LLC (Pat Farmer)", "10442", "Farmer Bros LLC", "Pat Farmer")
    # The new farmer is preselected, so the offer form is showing. Against the bid, dates / symbol / basis prefill.
    at.session_state[f"f{got['farmer_id']}_offer_bid"] = data.list_bids(app_db, "rcm")[0]["bid_id"]
    at.run()
    assert not at.exception, at.exception
    next(t for t in at.text_input if t.label == "Contract reference # (optional)").set_value("HTA-118")
    next(t for t in at.text_area if t.label == "Notes").set_value("Deliver to the north bin")
    next(b for b in at.button if b.label == "Enter offer").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    o = data.list_offers(app_db, "rcm", conftest.user(app_db, 2))[0]
    assert (o["farmer_name"], o["placed_by"], o["status"], o["basis_cents"], o["contract_ref"], o["note"]) == \
        ("Farmer Bros LLC (Pat Farmer)", 2, "submitted", -30, "HTA-118", "Deliver to the north bin")
    assert str(o["delivery_start"]) == "2999-12-01" and str(o["delivery_end"]) == "2999-12-31"


def test_offer_card_shows_client_id_and_staff_can_edit_notes_and_contract_ref(app_db):
    fid = farmers.create_farmer(app_db, "rcm", 2, "Pat Farmer", "555-0000", entity_name="Farmer Bros LLC",
                                client_id="10442")
    oid = data.place_offer_for_farmer(app_db, "rcm", 2, fid, grain="Corn", contract_type="HTA", price_type="BASIS",
                                      bushels=5000, basis_cents=-30, futures_symbol="ZCZ26")
    at = _as(app_db, 2)
    assert any("#10442" in m.value and "Farmer Bros LLC (Pat Farmer)" in m.value for m in at.markdown)
    next(t for t in at.text_input if t.label == "Contract reference #").set_value("C-5531")
    next(t for t in at.text_area if t.label == "Notes").set_value("Confirmed by phone")
    next(b for b in at.button if b.label == "Save").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    o = data.list_offers(app_db, "rcm", conftest.user(app_db, 2))[0]
    assert (o["offer_id"], o["contract_ref"], o["note"], o["status"]) == (oid, "C-5531", "Confirmed by phone", "submitted")
    assert any("Contract # C-5531" in c.value and "Confirmed by phone" in c.value for c in at.caption)


def test_farmer_sees_contract_ref_and_notes_but_cannot_edit_them(app_db):
    fid = farmers.create_farmer(app_db, "rcm", 2, "Pat Farmer", "555-0000", "pat@x.com")
    data.place_offer_for_farmer(app_db, "rcm", 2, fid, grain="Corn", contract_type="HTA", price_type="BASIS",
                                bushels=5000, basis_cents=-30, futures_symbol="ZCZ26", contract_ref="C-77", note="hello")
    app_db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (30, 'rcm', 'pat@x.com', 'farmer')")
    app_db.execute("UPDATE farmers SET user_id = 30 WHERE farmer_id = %s", (fid,))
    at = _as(app_db, 30)
    goto(at, "My offers")
    assert not at.exception, at.exception
    assert any("Contract # C-77" in c.value and "hello" in c.value for c in at.caption)
    assert not at.expander                                                  # no edit controls for farmers


def test_staff_accepts_and_withdraws_for_a_phone_farmer(app_db):
    fid = farmers.create_farmer(app_db, "rcm", 2, "Phone Caller", "555-0000")
    terms = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=5000,
                 basis_cents=-30, futures_symbol="ZCZ26")
    o1 = data.place_offer_for_farmer(app_db, "rcm", 2, fid, **terms)
    at = _as(app_db, 2)
    assert any("entered by" in m.value and "555-0000" in m.value for m in at.markdown)
    labels = {b.label for b in at.button}
    assert {"Mark hit", "Decline", "Withdraw for farmer"} <= labels
    assert not any("ounter" in b.label for b in at.button)                  # no counter-offers anywhere
    next(b for b in at.button if b.label == "Mark hit").click().run()
    assert not at.exception, at.exception
    assert data.list_offers(app_db, "rcm", conftest.user(app_db, 2))[0]["status"] == "hit"
    o2 = data.place_offer_for_farmer(app_db, "rcm", 2, fid, **terms)
    at = _as(app_db, 2)
    next(b for b in at.button if b.label == "Withdraw for farmer").click().run()
    assert {o["offer_id"]: o["status"] for o in data.list_offers(app_db, "rcm", conftest.user(app_db, 2))} == \
        {o1: "hit", o2: "withdrawn"}


def test_farmers_screen_invites_a_farmer_to_the_portal(app_db, monkeypatch):
    monkeypatch.setenv("OFFER_PORTAL_URL", "https://portal.example")
    farmers.create_farmer(app_db, "rcm", 2, "Pat Farmer", "555-1212", "pat@x.com")
    at = _go(_as(app_db, 2), "Farmers")
    next(b for b in at.button if b.label == "Invite to portal").click().run()
    assert not at.exception, at.exception
    assert any("https://portal.example/?t=rcm&invite=" in c.value for c in at.code)
    assert app_db.query("SELECT farmer_id FROM invites")[0]["farmer_id"] is not None
