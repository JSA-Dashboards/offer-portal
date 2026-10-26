import pytest
from streamlit.testing.v1 import AppTest

import conftest
from conftest import goto, pages  # noqa: F401
from portal import data, notify, platform, sms
from portal.data import PortalError
from test_app import _as, app_db  # noqa: F401  (fixture + signed-in session helper)

OFFER = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=5000, basis_cents=-30, futures_symbol="ZCZ26")


@pytest.fixture
def texting(db, monkeypatch):
    """rcm has texting on; a staff member and the rcm farmer (user 1) have opted in. Twilio settings are present."""
    for k, v in dict(SMS_ENABLED="1", TWILIO_ACCOUNT_SID="ACtest", TWILIO_AUTH_TOKEN="tok", TWILIO_FROM="+15550001111").items():
        monkeypatch.setenv(k, v)
    db.execute("UPDATE tenants SET sms_enabled = 1 WHERE tenant_id = 'rcm'")
    db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 's@rcm.com', 'elevator_staff')")
    for uid, phone in ((20, "(555) 200-0020"), (1, "555-100-0001"), (2, "5552000002")):
        notify.set_sms_optin(db, uid, phone, True)
    db.execute("DELETE FROM sms_queue")                                         # drop the confirmation texts
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", basis_cents=-38, futures_symbol="ZCZ26")
    return db


def _bid(db):
    return data.list_bids(db, "rcm")[0]["bid_id"]


def _queued(db):
    return db.query("SELECT kind, to_phone, body, from_number FROM sms_queue ORDER BY sms_id")


def test_phone_numbers_are_normalised_to_e164():
    assert sms.normalize_phone("(555) 123-4567") == "+15551234567" == sms.normalize_phone("1-555-123-4567")
    assert sms.normalize_phone("+44 20 7946 0958") == "+442079460958"
    for bad in ("", None, "12345", "055-123-4567", "(155) 123-4567", "+123"):
        assert sms.normalize_phone(bad) is None
    assert sms.pretty("+15551234567") == "(555) 123-4567"


def test_nothing_sends_unless_switched_on_configured_and_the_elevator_is_on(db, monkeypatch):
    assert not sms.ready()
    monkeypatch.setenv("SMS_ENABLED", "1")
    assert not sms.ready()                                                      # no Twilio settings yet
    for k, v in dict(TWILIO_ACCOUNT_SID="ACtest", TWILIO_AUTH_TOKEN="tok", TWILIO_FROM="+15550001111").items():
        monkeypatch.setenv(k, v)
    assert sms.ready() and notify.tenant_sms(db, "rcm") is None                 # elevator not switched on
    db.execute("UPDATE tenants SET sms_enabled = 1 WHERE tenant_id = 'rcm'")
    assert notify.tenant_sms(db, "rcm")["from"] is None
    monkeypatch.setenv("SMS_ENABLED", "0")
    assert notify.tenant_sms(db, "rcm") is None                                 # the master switch beats everything
    with pytest.raises(sms.SmsError):
        sms.send("+15551234567", "hi")


def test_opting_in_records_consent_and_texts_a_confirmation_once(texting):
    db = texting
    notify.set_sms_optin(db, 20, "555-200-0020", False)
    assert db.query("SELECT sms_opt_in_at FROM users WHERE user_id = 20")[0]["sms_opt_in_at"] is None
    notify.set_sms_optin(db, 20, "555-200-0020", True)
    row = db.query("SELECT phone, sms_opt_in_at FROM users WHERE user_id = 20")[0]
    assert row["phone"] == "+15552000020" and row["sms_opt_in_at"]
    q = _queued(db)
    assert [x["kind"] for x in q] == ["sms_optin"] and "STOP" in q[0]["body"] and "HELP" in q[0]["body"]
    notify.set_sms_optin(db, 20, "(555) 200-0020", True)                         # saving the same settings again
    assert len(_queued(db)) == 1
    with pytest.raises(PortalError):
        notify.set_sms_optin(db, 20, "123", True)
    with pytest.raises(PortalError):
        notify.set_sms_optin(db, 5, "555-200-0020", True)                       # JSA admins have no elevator to text for


def test_farmers_are_texted_on_hit_completed_cancelled_expired_only(texting):
    db = texting
    farmer = conftest.user(db, 1)
    mk = lambda: data.create_offer(db, "rcm", farmer, **OFFER, bid_id=_bid(db))
    o = mk()
    assert [x for x in _queued(db) if x["to_phone"] == "+15551000001"] == []     # a new offer does not text the farmer
    data.act_on_offer(db, "rcm", conftest.user(db, 2), o, "hit")
    got = [x for x in _queued(db) if x["to_phone"] == "+15551000001"]
    assert [x["kind"] for x in got] == ["offer_hit"] and "5,000 bu Corn" in got[0]["body"] and got[0]["body"].endswith("Reply STOP to end texts.")
    data.acknowledge_offer(db, "rcm", 20, o)
    data.complete_offer(db, "rcm", 20, o, "C-9")
    assert "offer_completed" in [x["kind"] for x in _queued(db) if x["to_phone"] == "+15551000001"]
    d = mk()
    data.act_on_offer(db, "rcm", conftest.user(db, 2), d, "decline")
    assert "offer_declined" not in [x["kind"] for x in _queued(db)]              # declined / withdrawn stay email-only
    e = mk()
    data.act_on_offer(db, "rcm", conftest.user(db, 2), e, "hit")
    data.cancel_offer(db, "rcm", 2, e, "changed my mind")
    assert "offer_cancelled" in [x["kind"] for x in _queued(db) if x["to_phone"] == "+15551000001"]
    x = mk()
    db.execute("UPDATE offers SET delivery_end = '2000-01-01' WHERE offer_id = %s", (x,))
    data.expire_stale(db, "rcm")
    assert "offer_expired" in [y["kind"] for y in _queued(db) if y["to_phone"] == "+15551000001"]


def test_staff_and_admins_are_texted_on_new_offer_and_hit_but_not_the_person_who_placed_it(texting):
    db = texting
    farmer = conftest.user(db, 1)
    o = data.create_offer(db, "rcm", farmer, **OFFER, bid_id=_bid(db))
    to = sorted(x["to_phone"] for x in _queued(db) if x["kind"] == "offer_submitted")
    assert to == ["+15552000002", "+15552000020"] and all("new offer" in x["body"] for x in _queued(db))
    db.execute("DELETE FROM sms_queue")
    data.act_on_offer(db, "rcm", conftest.user(db, 2), o, "hit")
    kinds = {(x["to_phone"], x["kind"]) for x in _queued(db)}
    assert ("+15552000020", "offer_hit") in kinds and ("+15552000002", "offer_hit") in kinds
    db.execute("DELETE FROM sms_queue")
    from portal import farmers
    pat = farmers.create_farmer(db, "rcm", 20, "Pat Phone", "555-1212")           # staff member 20 takes a call
    data.place_offer_for_farmer(db, "rcm", 20, pat, **OFFER, bid_id=_bid(db))
    to = [x["to_phone"] for x in _queued(db) if x["kind"] == "offer_submitted"]
    assert to == ["+15552000002"]                                                  # the admin, not the staffer who entered it


def test_people_who_have_not_opted_in_or_whose_elevator_is_off_are_not_texted(texting):
    db = texting
    notify.set_sms_optin(db, 1, None, False)                                     # the farmer opts out
    o = data.create_offer(db, "rcm", conftest.user(db, 1), **OFFER, bid_id=_bid(db))
    data.act_on_offer(db, "rcm", conftest.user(db, 2), o, "hit")
    assert "+15551000001" not in [x["to_phone"] for x in _queued(db)]
    db.execute("UPDATE tenants SET sms_enabled = 0 WHERE tenant_id = 'rcm'")
    db.execute("DELETE FROM sms_queue")
    o2 = data.create_offer(db, "rcm", conftest.user(db, 1), **OFFER, bid_id=_bid(db))
    data.act_on_offer(db, "rcm", conftest.user(db, 2), o2, "hit")
    assert _queued(db) == []


def test_an_elevators_own_number_beats_the_shared_one(texting):
    db = texting
    platform.update_tenant(db, 5, "rcm", name="RCM", logo_url=None, primary_color="#32373c", accent_color="#0693e3",
                           support_email=None, from_name=None, open_pricing=True, hedging_enabled=False,
                           sms_enabled=True, sms_from="(555) 777-0100")
    assert notify.tenant_sms(db, "rcm")["from"] == "+15557770100"
    data.create_offer(db, "rcm", conftest.user(db, 1), **OFFER, bid_id=_bid(db))
    assert {x["from_number"] for x in _queued(db)} == {"+15557770100"}
    sent = []
    notify.send_pending_sms(db, send=lambda to, body, frm: sent.append((to, frm)) or "SM1")
    assert {f for _, f in sent} == {"+15557770100"}
    with pytest.raises(PortalError):
        platform.update_tenant(db, 5, "rcm", name="RCM", logo_url=None, primary_color="#32373c", accent_color="#0693e3",
                               support_email=None, from_name=None, open_pricing=True, hedging_enabled=False,
                               sms_enabled=True, sms_from="nope")


def test_sending_marks_rows_and_a_stop_reply_clears_the_opt_in(texting):
    db = texting
    data.create_offer(db, "rcm", conftest.user(db, 1), **OFFER, bid_id=_bid(db))
    n = len(_queued(db))

    def send(to, body, frm):
        if to == "+15552000020":
            raise sms.StopRequested("Twilio 21610: unsubscribed", 21610)
        if to == "+15552000002":
            raise sms.SmsError("Twilio 30003: unreachable", 30003)
        return "SMabc"

    out = notify.send_pending_sms(db, send=send)
    assert out == dict(sent=n - 2, failed=2)
    st = {r["to_phone"]: r["status"] for r in db.query("SELECT to_phone, status FROM sms_queue")}
    assert st["+15552000020"] == "opted_out" and st["+15552000002"] == "failed"
    assert db.query("SELECT sms_opt_in_at FROM users WHERE user_id = 20")[0]["sms_opt_in_at"] is None      # never texted again
    assert db.query("SELECT sms_opt_in_at FROM users WHERE user_id = 2")[0]["sms_opt_in_at"] is not None   # a plain failure keeps consent
    assert notify.send_pending_sms(db, send=lambda *a: "SMx")["sent"] == 1                                    # the failed one is retried


def test_twilio_request_shape_and_error_mapping(monkeypatch):
    for k, v in dict(SMS_ENABLED="1", TWILIO_ACCOUNT_SID="ACtest", TWILIO_AUTH_TOKEN="tok", TWILIO_FROM="+15550001111").items():
        monkeypatch.setenv(k, v)

    class R:
        def __init__(self, status, body):
            self.status_code, self._b = status, body

        def json(self):
            return self._b

    calls = []

    class S:
        def __init__(self, resp):
            self.resp = resp

        def post(self, url, data=None, auth=None, timeout=None):
            calls.append((url, data, auth))
            return self.resp

    assert sms.send("+15551234567", "hello", session=S(R(201, {"sid": "SM123"}))) == "SM123"
    url, payload, auth = calls[0]
    assert url.endswith("/Accounts/ACtest/Messages.json") and payload == {"To": "+15551234567", "Body": "hello", "From": "+15550001111"}
    assert auth == ("ACtest", "tok")
    sms.send("+15551234567", "hi", from_number="+15557770100", session=S(R(201, {"sid": "SM1"})))
    assert calls[1][1]["From"] == "+15557770100"
    monkeypatch.delenv("TWILIO_FROM")
    monkeypatch.setenv("TWILIO_MESSAGING_SERVICE_SID", "MG1")
    sms.send("+15551234567", "hi", session=S(R(201, {"sid": "SM2"})))
    assert calls[2][1].get("MessagingServiceSid") == "MG1" and "From" not in calls[2][1]
    with pytest.raises(sms.StopRequested):
        sms.send("+15551234567", "hi", session=S(R(400, {"code": 21610, "message": "unsubscribed"})))
    with pytest.raises(sms.SmsError) as e:
        sms.send("+15551234567", "hi", session=S(R(400, {"code": 21211, "message": "bad number"})))
    assert not isinstance(e.value, sms.StopRequested) and e.value.code == 21211


def test_the_code_runs_before_sql_12_and_the_queue_is_left_alone(db, monkeypatch):
    real = db.query
    monkeypatch.setattr(db, "query", lambda sql, params=(): (_ for _ in ()).throw(RuntimeError("no such column"))
                        if ("sms_opt_in_at" in sql or "sms_enabled" in sql or "sms_queue" in sql) else real(sql, params))
    assert notify.sms_ready(db) is False
    assert notify.tenant_sms(db, "rcm") is None
    assert notify.send_pending_sms(db, send=lambda *a: "x")["skipped"]
    with pytest.raises(PortalError):
        notify.set_sms_optin(db, 1, "555-100-0001", True)


def test_sidebar_text_settings_appear_only_when_the_elevator_is_on_and_save_consent(app_db, monkeypatch):
    for k, v in dict(SMS_ENABLED="1", TWILIO_ACCOUNT_SID="ACtest", TWILIO_AUTH_TOKEN="tok", TWILIO_FROM="+15550001111").items():
        monkeypatch.setenv(k, v)
    at = _as(app_db, 1)
    assert not [e for e in at.expander if e.label.startswith("Text alerts")]             # elevator not switched on
    app_db.execute("UPDATE tenants SET sms_enabled = 1 WHERE tenant_id = 'rcm'")
    at = _as(app_db, 1)
    assert [e for e in at.expander if e.label.startswith("Text alerts")]
    next(t for t in at.text_input if t.label == "Mobile number").set_value("(555) 100-0001").run()
    next(c for c in at.checkbox if c.label.startswith("I agree to receive text messages")).check().run()
    next(b for b in at.button if b.label == "Save text settings").click().run()
    assert not at.exception, at.exception
    assert app_db.query("SELECT phone, sms_opt_in_at FROM users WHERE user_id = 1")[0]["phone"] == "+15551000001"
    assert [e for e in at.expander if e.label == "Text alerts (on)"]
