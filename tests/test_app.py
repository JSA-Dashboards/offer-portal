import sqlite3
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from portal import auth, data
from portal.db import Db
import conftest


@pytest.fixture
def app_db(monkeypatch):
    st.cache_resource.clear()          # get_db() caches the connection across tests
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(conftest.DDL)
    d = Db(conn, "sqlite")
    d.execute("INSERT INTO tenants (tenant_id, name) VALUES ('rcm', 'RCM Coop')")
    d.execute("INSERT INTO locations (location_id, tenant_id, name) VALUES (1, 'rcm', 'Riverton')")
    d.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (2, 'rcm', 'admin@rcm.com', 'elevator_admin')")
    d.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (1, 'rcm', 'f@a.com', 'farmer')")
    monkeypatch.setattr("portal.db.connect", lambda: d)
    return d


def run(**params):
    at = AppTest.from_file("app.py", default_timeout=20)
    for k, v in params.items():
        at.query_params[k] = v
    return at.run()


def test_no_tenant_link(app_db):
    at = run()
    assert not at.exception
    assert any("link your elevator" in i.value for i in at.info)


def test_unknown_tenant(app_db):
    at = run(t="nope")
    assert any("link your elevator" in i.value for i in at.info)


def test_login_page_for_tenant(app_db):
    at = run(t="rcm")
    assert not at.exception
    assert any("RCM Coop" in m.value and "dash-header" in m.value for m in at.markdown)      # branded header
    assert at.text_input[0].label == "Email"


def _sign_in(at, db, email):
    box = []
    auth.request_code(db, email, "rcm", lambda e, c: box.append(c))
    at.session_state["login_step"] = "code"
    at.session_state["login_email"] = email
    at.run()
    at.text_input[0].set_value(box[0])
    at.button[0].click().run()
    return at


def _as(db, uid):
    """Start an already-signed-in session (AppTest can't re-run a page reached via st.rerun())."""
    at = AppTest.from_file("app.py", default_timeout=20)
    at.query_params["t"] = "rcm"
    at.session_state["user"] = conftest.user(db, uid)
    at.session_state["tenant_id"] = "rcm"
    return at.run()


def test_sign_in_flow_reaches_the_app(app_db):
    at = _sign_in(run(t="rcm"), app_db, "admin@rcm.com")
    assert not at.exception
    assert at.session_state["user"]["role"] == "elevator_admin"


def test_staff_lands_on_offers_inbox_and_can_mark_hit(app_db):
    oid = data.create_offer(app_db, "rcm", conftest.user(app_db, 1), grain="Corn", contract_type="HTA",
                            price_type="BASIS", bushels=5000, basis_cents=-30, futures_symbol="ZCZ26")
    at = _as(app_db, 2)
    assert not at.exception
    assert any("5,000 bu Corn" in m.value for m in at.markdown)
    next(b for b in at.button if b.label == "Mark hit").click().run()
    assert not at.exception
    assert data.list_offers(app_db, "rcm", conftest.user(app_db, 2))[0]["status"] == "hit"


def test_staff_bid_board_has_post_controls(app_db):
    data.publish_bid(app_db, "rcm", 2, grain="Corn", delivery_month="2026-12", location_id=1, basis_cents=-38)
    at = _as(app_db, 2)
    at.radio[0].set_value("Bid board").run()
    assert not at.exception
    assert at.dataframe and at.expander


def test_farmer_makes_offer(app_db):
    at = _as(app_db, 1)
    at.radio[0].set_value("Make an offer").run()
    assert not at.exception, at.exception
    send = lambda: next(b for b in at.button if b.label == "Send offer")
    send().click().run()                                   # open price: no delivery period, no futures month yet
    assert at.error and "delivery period" in at.error[0].value.lower()           # the delivery period sets when it expires
    assert data.list_offers(app_db, "rcm", conftest.user(app_db, 1)) == []
    # Against a posted bid the delivery period (and symbol, basis) come from the bid, so it can be sent as it is.
    data.publish_bid(app_db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", location_id=1,
                     basis_cents=-30, cash_price=4.2)
    at = _as(app_db, 1)
    at.radio[0].set_value("Make an offer").run()
    at.session_state["offer_bid"] = data.list_bids(app_db, "rcm")[0]["bid_id"]                       # pick the bid (AppTest cannot drive a
    at.run()                                                                                         # selectbox that has a format_func)
    assert not at.exception, at.exception
    assert [str(d.value) for d in at.date_input[:2]] == ["2999-12-01", "2999-12-31"]      # prefilled from the bid
    send().click().run()
    assert not at.error, [e.value for e in at.error]
    offer = data.list_offers(app_db, "rcm", conftest.user(app_db, 1))[0]
    assert (offer["futures_symbol"], offer["basis_cents"], offer["status"]) == ("ZCZ26", -30, "submitted")
    assert str(offer["delivery_end"]) == "2999-12-31"


def test_farmer_view_is_read_only_board(app_db):
    data.publish_bid(app_db, "rcm", 2, grain="Corn", delivery_month="2026-12", location_id=1, basis_cents=-38)
    at = _sign_in(run(t="rcm"), app_db, "f@a.com")
    assert not at.exception
    assert at.dataframe
    assert not at.expander


def test_join_page_accepts_invite_and_signs_in(app_db):
    from portal import people
    tok = people.create_invite(app_db, "rcm", 2, "newbie@x.com")
    at = run(t="rcm", invite=tok)
    assert not at.exception
    assert any("newbie@x.com" in c.value for c in at.caption)
    at.text_input[0].set_value("Pat Farmer")
    next(b for b in at.button if b.label == "Join").click().run()
    assert not at.exception
    assert at.session_state["user"]["email"] == "newbie@x.com"
    assert app_db.query("SELECT role FROM users WHERE email = 'newbie@x.com'")[0]["role"] == "farmer"


def test_join_page_rejects_bad_token(app_db):
    at = run(t="rcm", invite="not-a-real-token")
    assert not at.exception
    assert any("invalid, expired" in e.value for e in at.error)


def test_staff_people_view_creates_invite_and_shows_link(app_db, monkeypatch):
    monkeypatch.setenv("OFFER_PORTAL_URL", "https://portal.example")
    at = _as(app_db, 2)
    at.radio[0].set_value("People").run()
    assert not at.exception
    at.text_input[0].set_value("fresh@x.com")
    next(b for b in at.button if b.label == "Create invite").click().run()
    assert not at.exception
    assert any("fresh@x.com" in r["email"] for r in app_db.query("SELECT email FROM invites"))
    assert any("https://portal.example/?t=rcm&invite=" in c.value for c in at.code)


def test_platform_view_only_for_platform_admin(app_db):
    app_db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (5, NULL, 'p@jsa.com', 'platform_admin')")
    assert "Platform" not in _as(app_db, 2).radio[0].options
    at = _as(app_db, 5)
    assert "Platform" in at.radio[0].options
    at.radio[0].set_value("Platform").run()
    assert not at.exception
    assert any(sb.label == "Tenant" for sb in at.selectbox)


def test_platform_admin_creates_tenant_from_the_console(app_db, monkeypatch):
    monkeypatch.setenv("OFFER_PORTAL_URL", "https://portal.example")
    app_db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (5, NULL, 'p@jsa.com', 'platform_admin')")
    at = _as(app_db, 5)
    at.radio[0].set_value("Platform").run()
    f = {t.label: t for t in reversed(at.text_input)}          # the create form comes first on the page
    f["Company name"].set_value("Newco Grain")
    f["Slug (the ?t= in their link)"].set_value("newco")
    f["First elevator admin's email"].set_value("boss@newco.com")
    next(t for t in at.text_area if t.label.startswith("Locations")).set_value("Main St, IL\nNorth")
    next(b for b in at.button if b.label == "Create tenant").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert [l["name"] for l in app_db.query("SELECT name FROM locations WHERE tenant_id = 'newco' ORDER BY name")] == ["Main St", "North"]
    assert any("https://portal.example/?t=newco&invite=" in c.value for c in at.code)
