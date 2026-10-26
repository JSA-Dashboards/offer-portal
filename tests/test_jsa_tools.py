import pytest
from streamlit.testing.v1 import AppTest

import conftest
from portal import auth, farmers, platform
from portal.data import PortalError
from test_app import app_db  # noqa: F401  (fixture)


def test_platform_admins_are_managed_by_platform_admins_only(db):
    with pytest.raises(PortalError):
        platform.add_platform_admin(db, 2, "ross@jpsi.com")                  # an elevator admin
    platform.add_platform_admin(db, 5, "Ross@JPSI.com", "Ross")
    assert [a["email"] for a in platform.list_platform_admins(db, 5)] == ["kpostin@jpsi.com", "ross@jpsi.com"]
    for bad in ("nobody@gmail.com", "not-an-email", "ross@jpsi.com", "admin@rcm.com"):
        with pytest.raises(PortalError):
            platform.add_platform_admin(db, 5, bad)
    rid = db.query("SELECT user_id FROM users WHERE email = 'ross@jpsi.com'")[0]["user_id"]
    with pytest.raises(PortalError):
        platform.set_platform_admin_active(db, 5, 5, False)                  # never your own
    platform.set_platform_admin_active(db, 5, rid, False)
    assert not auth._allowed_user(db, "ross@jpsi.com", None)                 # turned off: can't sign in
    platform.set_platform_admin_active(db, 5, rid, True)
    assert auth._allowed_user(db, "ross@jpsi.com", None)


def test_jsa_admin_can_sign_in_without_a_tenant_link_but_others_cannot(db):
    sent = []
    auth.request_code(db, "kpostin@jpsi.com", None, lambda e, c: sent.append(c))
    auth.request_code(db, "admin@rcm.com", None, lambda e, c: sent.append(c))      # a tenant admin: silently no code
    assert len(sent) == 1
    assert auth.verify_code(db, "kpostin@jpsi.com", sent[0], None)["role"] == "platform_admin"


def test_preview_farmers_lists_only_farmers_with_a_login(db):
    farmers.create_farmer(db, "rcm", 2, "No Login Farms")
    fid = farmers.create_farmer(db, "rcm", 2, "Login Farms", email="farmer@a.com")
    db.execute("UPDATE farmers SET user_id = 1 WHERE farmer_id = %s", (fid,))
    assert [u["farmer_name"] for u in platform.preview_farmers(db, 5, "rcm")] == ["Login Farms"]
    with pytest.raises(PortalError):
        platform.preview_farmers(db, 2, "rcm")


def _jsa(app_db, tenant="rcm"):
    app_db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (5, NULL, 'kpostin@jpsi.com', 'platform_admin')")
    at = AppTest.from_file("app.py", default_timeout=20)
    if tenant:
        at.query_params["t"] = tenant
    at.session_state["user"] = conftest.user(app_db, 5)
    at.session_state["tenant_id"] = tenant
    return at.run()


def test_root_page_without_a_link_offers_jsa_sign_in(app_db):
    at = AppTest.from_file("app.py", default_timeout=20).run()
    assert not at.exception
    assert any("link your elevator gave you" in i.value for i in at.info)
    assert [e for e in at.expander if e.label == "JSA staff sign in"]


def test_jsa_console_lists_tenants_without_a_link(app_db):
    at = _jsa(app_db, tenant=None)
    assert not at.exception and any("JSA Offer Portal Management" in s.value for s in at.subheader)


def test_view_as_each_level(app_db):
    at = _jsa(app_db)
    assert not at.exception and "Platform" in list(at.radio[1].options) or "Platform" in [o for r in at.radio for o in r.options]
    lens = next(r for r in at.radio if r.label == "View as")
    views = lambda a: next(r for r in a.radio if r.label == "View").options
    lens.set_value("Elevator staff").run()
    assert not at.exception and "Hedge" not in views(at) and "Platform" not in views(at) and "Offers" in views(at)
    assert any("Previewing as elevator staff" in w.value for w in at.warning)
    next(r for r in at.radio if r.label == "View as").set_value("Elevator admin").run()
    assert "Hedge" in views(at) and "Platform" not in views(at)
    next(r for r in at.radio if r.label == "View as").set_value("Farmer").run()
    assert not at.exception                                                  # no farmer has a login: a prompt, not a crash
    next(r for r in at.radio if r.label == "View as").set_value("JSA admin (me)").run()
    assert "Platform" in views(at) and not at.warning


def test_view_as_a_farmer_shows_their_pages_and_blocks_writes(app_db):
    fid = farmers.create_farmer(app_db, "rcm", 2, "Login Farms", email="f@a.com")
    app_db.execute("UPDATE farmers SET user_id = 1 WHERE farmer_id = %s", (fid,))
    at = _jsa(app_db)
    next(r for r in at.radio if r.label == "View as").set_value("Farmer").run()
    assert not at.exception, at.exception
    views = next(r for r in at.radio if r.label == "View").options
    assert list(views) == ["Bid board", "Make an offer", "My offers", "USDA calendar"]
    assert any("Previewing as farmer Login Farms" in w.value for w in at.warning)
    assert not any(t.label == "Email me notifications" for t in at.toggle)
