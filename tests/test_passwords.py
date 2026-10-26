from datetime import datetime, timedelta, timezone

import pytest
from streamlit.testing.v1 import AppTest

import conftest
from portal import auth
from portal.data import PortalError
from test_app import app_db  # noqa: F401  (fixture)

GOOD = "correct horse battery"


def test_policy_and_hashing(db):
    assert auth.check_policy("short") and auth.check_policy("password123") and auth.check_policy("aaaaaaaaaaaa")
    assert auth.check_policy("admin@rcm.com", "admin@rcm.com") and auth.check_policy("farmer1234", "farmer1234@a.com")
    assert auth.check_policy(GOOD, "admin@rcm.com") is None
    h1, h2 = auth.hash_password(GOOD, rounds=1000), auth.hash_password(GOOD, rounds=1000)
    assert h1 != h2 and h1.startswith("pbkdf2_sha256$1000$") and GOOD not in h1          # salted, never plain
    assert auth._matches(GOOD, h1) and not auth._matches("wrong", h1) and not auth._matches(GOOD, "garbage")


def test_set_password_then_sign_in_with_it(db):
    assert not auth.has_password(db, 2)
    auth.set_password(db, 2, GOOD)
    assert auth.has_password(db, 2)
    u = auth.verify_password(db, " Admin@RCM.com ", GOOD, "rcm")
    assert u["user_id"] == 2 and not {"password_hash", "failed_logins", "locked_until"} & set(u)
    assert auth.verify_password(db, "admin@rcm.com", "wrong password!", "rcm") is None
    with pytest.raises(PortalError):
        auth.set_password(db, 2, "short")
    with pytest.raises(PortalError):
        auth.set_password(db, 999, GOOD)


def test_a_password_only_works_on_its_own_elevators_page_and_jsa_works_anywhere(db):
    auth.set_password(db, 2, GOOD)
    auth.set_password(db, 5, GOOD + " jsa")
    assert auth.verify_password(db, "admin@rcm.com", GOOD, "dorans") is None
    assert auth.verify_password(db, "admin@rcm.com", GOOD, None) is None
    for page in ("rcm", "dorans", None):
        assert auth.verify_password(db, "kpostin@jpsi.com", GOOD + " jsa", page)["role"] == "platform_admin"


def test_failures_look_alike_and_five_in_a_row_pause_password_sign_in(db):
    auth.set_password(db, 2, GOOD)
    assert auth.verify_password(db, "nobody@x.com", GOOD, "rcm") is None                 # unknown email
    assert auth.verify_password(db, "farmer@a.com", GOOD, "rcm") is None                 # real user, no password set
    for _ in range(auth.MAX_FAILS):
        assert auth.verify_password(db, "admin@rcm.com", "not the password", "rcm") is None
    assert auth.verify_password(db, "admin@rcm.com", GOOD, "rcm") is None                # right password, but paused
    row = db.query("SELECT locked_until, failed_logins FROM users WHERE user_id = 2")[0]
    assert row["locked_until"] and row["failed_logins"] == 0
    assert auth.request_code(db, "admin@rcm.com", "rcm", lambda e, c: None) is None      # codes still work as the way back in
    db.execute("UPDATE users SET locked_until = %s WHERE user_id = 2",
               ((datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),))
    assert auth.verify_password(db, "admin@rcm.com", GOOD, "rcm")["user_id"] == 2        # the pause ends by itself
    assert db.query("SELECT locked_until, failed_logins FROM users WHERE user_id = 2")[0]["locked_until"] is None


def test_wrong_attempts_below_the_limit_reset_on_success(db):
    auth.set_password(db, 2, GOOD)
    for _ in range(auth.MAX_FAILS - 1):
        auth.verify_password(db, "admin@rcm.com", "nope nope nope", "rcm")
    assert auth.verify_password(db, "admin@rcm.com", GOOD, "rcm")
    assert db.query("SELECT failed_logins FROM users WHERE user_id = 2")[0]["failed_logins"] == 0


def test_the_app_still_signs_in_by_code_when_the_password_columns_are_not_there_yet(db, monkeypatch):
    real = db.query
    monkeypatch.setattr(db, "query", lambda sql, params=(): (_ for _ in ()).throw(RuntimeError("no such column"))
                        if "password_hash" in sql else real(sql, params))
    assert auth.passwords_ready(db) is False
    assert auth.verify_password(db, "admin@rcm.com", GOOD, "rcm") is None
    assert auth.has_password(db, 2) is False
    with pytest.raises(PortalError):
        auth.set_password(db, 2, GOOD)
    sent = []
    auth.request_code(db, "admin@rcm.com", "rcm", lambda e, c: sent.append(c))          # codes are unaffected
    assert auth.verify_code(db, "admin@rcm.com", sent[0], "rcm")["user_id"] == 2


def _login(app_db, **state):
    at = AppTest.from_file("app.py", default_timeout=20)
    at.query_params["t"] = "rcm"
    for k, v in state.items():
        at.session_state[k] = v
    return at.run()


def test_login_page_defaults_to_password_and_signs_in(app_db):
    auth.set_password(app_db, 2, GOOD)
    at = _login(app_db)
    assert not at.exception and any(f.label == "Password" for f in at.text_input)
    next(t for t in at.text_input if t.label == "Email").set_value("admin@rcm.com")
    next(t for t in at.text_input if t.label == "Password").set_value("wrong wrong wrong")
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert any("didn't match" in e.value for e in at.error)
    next(t for t in at.text_input if t.label == "Password").set_value(GOOD)
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert at.session_state["user"]["user_id"] == 2 and "password_hash" not in at.session_state["user"]


def test_after_an_email_code_sign_in_the_app_offers_a_password_once(app_db):
    at = _login(app_db, login_step="code", login_email="admin@rcm.com")
    box = []
    auth.request_code(app_db, "admin@rcm.com", "rcm", lambda e, c: box.append(c))
    at.text_input[0].set_value(box[0])
    at.button[0].click().run()
    assert any("Set a password for next time" in s.value for s in at.subheader)
    at.text_input[0].set_value(GOOD).run()
    at.text_input[1].set_value(GOOD).run()
    next(b for b in at.button if b.label == "Save password").click().run()
    assert auth.has_password(app_db, 2) and not at.exception
    assert "pw_offer" not in at.session_state
