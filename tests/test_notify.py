import pytest
from portal import data, farmers, notify
from conftest import user

HTA = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=10000, basis_cents=-30, futures_symbol="ZCZ26")


def _setup(db):
    """rcm: farmer 1 (login), admin 2, staff 20 + 21; staff and admins have emails."""
    db.execute("UPDATE users SET email = 'admin@rcm.example', name = 'Alex Admin' WHERE user_id = 2")
    db.execute("UPDATE users SET email = 'farmer@a.example', name = 'Pat Farmer' WHERE user_id = 1")
    db.execute("INSERT INTO users (user_id, tenant_id, email, name, role) VALUES (20, 'rcm', 'sam@rcm.example', 'Sam', 'elevator_staff')")
    db.execute("INSERT INTO users (user_id, tenant_id, email, name, role) VALUES (21, 'rcm', 'sue@rcm.example', 'Sue', 'elevator_staff')")


def _box(db, kind=None):
    sql = "SELECT kind, to_email, subject, body_html FROM notifications"
    return db.query(sql + (" WHERE kind = %s ORDER BY notification_id" if kind else " ORDER BY notification_id"), (kind,) if kind else ())


def _to(db, kind):
    return sorted(r["to_email"] for r in _box(db, kind))


# ── the outbox ───────────────────────────────────────────────────────────────
def test_queue_skips_bad_addresses_and_duplicates(db):
    assert notify.queue(db, "rcm", "k", {"email": "a@b.example"}, "s", "<p>b</p>", dedupe_key="x:1")
    assert not notify.queue(db, "rcm", "k", {"email": "A@B.example"}, "s", "<p>b</p>", dedupe_key="x:1")   # same key + address
    assert notify.queue(db, "rcm", "k", {"email": "c@d.example"}, "s", "<p>b</p>", dedupe_key="x:1")      # other address
    assert notify.queue(db, "dorans", "k", {"email": "a@b.example"}, "s", "<p>b</p>", dedupe_key="x:1")   # other tenant
    for bad in ("", None, "no-at-sign", "a@b"):
        assert not notify.queue(db, "rcm", "k", {"email": bad}, "s", "b")
    assert len(_box(db)) == 3


def test_send_pending_marks_sent_retries_failures_then_gives_up(db):
    notify.queue(db, "rcm", "k", {"email": "ok@x.example"}, "ok", "<p>1</p>")
    notify.queue(db, "rcm", "k", {"email": "bad@x.example"}, "bad", "<p>2</p>")
    seen = []

    def sender(to, subject, html):
        seen.append(to)
        if to == "bad@x.example":
            raise RuntimeError("mailbox full")
    assert notify.send_pending(db, send=sender) == {"sent": 1, "failed": 1}
    st = {r["to_email"]: (r["status"], r["attempts"]) for r in db.query("SELECT to_email, status, attempts FROM notifications")}
    assert st == {"ok@x.example": ("sent", 1), "bad@x.example": ("failed", 1)}
    notify.send_pending(db, send=sender)
    notify.send_pending(db, send=sender)
    assert notify.send_pending(db, send=sender) == {"sent": 0, "failed": 0}              # attempts used up, left as failed
    assert seen.count("ok@x.example") == 1 and seen.count("bad@x.example") == notify.MAX_ATTEMPTS
    assert "mailbox full" in db.query("SELECT error FROM notifications WHERE to_email = 'bad@x.example'")[0]["error"]


def test_nothing_is_sent_without_graph_and_the_queue_is_kept(db, monkeypatch):
    for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_SENDER", "GRAPH_ENV_FILE"):
        monkeypatch.delenv(k, raising=False)
    notify.queue(db, "rcm", "k", {"email": "ok@x.example"}, "s", "<p>1</p>")
    out = notify.send_pending(db)
    assert out["sent"] == 0 and "not configured" in out["skipped"]
    assert _box(db)[0] is not None and db.query("SELECT status FROM notifications")[0]["status"] == "queued"


# ── who is told about what ───────────────────────────────────────────────────
def test_a_new_offer_goes_to_staff_and_admins_but_not_to_whoever_entered_it(db):
    _setup(db)
    data.create_offer(db, "rcm", user(db, 1), **HTA)                                     # farmer's own offer
    assert _to(db, "offer_submitted") == ["admin@rcm.example", "sam@rcm.example", "sue@rcm.example"]
    fid = farmers.create_farmer(db, "rcm", 20, "Phone Caller", email="caller@x.example")
    data.place_offer_for_farmer(db, "rcm", 20, fid, **HTA)                                # Sam enters it for a farmer
    placed = [r["to_email"] for r in _box(db, "offer_submitted")][3:]
    assert sorted(placed) == ["admin@rcm.example", "sue@rcm.example"]                     # Sam isn't told about his own entry


def test_a_hit_tells_the_farmer_staff_and_admins_to_acknowledge(db):
    _setup(db)
    oid = data.create_offer(db, "rcm", user(db, 1), **HTA)
    data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")
    assert _to(db, "offer_hit") == ["admin@rcm.example", "farmer@a.example", "sam@rcm.example", "sue@rcm.example"]
    staff_mail = [r for r in _box(db, "offer_hit") if r["to_email"] == "sam@rcm.example"][0]
    assert staff_mail["subject"].startswith("HIT: 10,000 bu Corn") and "Acknowledge it in the portal" in staff_mail["body_html"]
    farmer_mail = [r for r in _box(db, "offer_hit") if r["to_email"] == "farmer@a.example"][0]
    assert "Your offer was hit" in farmer_mail["subject"] and "Acknowledge" not in farmer_mail["body_html"]
    assert "substantial risk of loss" in farmer_mail["body_html"]                          # the standard disclaimer is in every email


def test_an_automatic_hit_says_so(db):
    _setup(db)
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", basis_cents=-38, cash_price=4.50)
    data.add_auto_hit_rule(db, "rcm", 2)
    data.create_offer(db, "rcm", user(db, 1), grain="Corn", contract_type="CASH", price_type="FIXED", bushels=5000, price=4.30,
                      futures_symbol="ZCZ26", bid_id=data.list_bids(db, "rcm")[0]["bid_id"])
    assert any("automatically by your auto-hit rules" in r["body_html"] for r in _box(db, "offer_hit"))


def test_completion_tells_the_farmer_the_contract_number(db):
    _setup(db)
    oid = data.create_offer(db, "rcm", user(db, 1), **HTA)
    data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")
    data.acknowledge_offer(db, "rcm", 20, oid)
    assert _box(db, "offer_acknowledged") == []                                            # acknowledging is internal: no email
    data.complete_offer(db, "rcm", 20, oid, "C-5531")
    m = _box(db, "offer_completed")
    assert [r["to_email"] for r in m] == ["farmer@a.example"] and "C-5531" in m[0]["subject"] and "C-5531" in m[0]["body_html"]


def test_decline_and_expiry_tell_the_farmer_and_a_farmer_withdrawal_tells_staff(db):
    _setup(db)
    declined = data.create_offer(db, "rcm", user(db, 1), **HTA)
    data.act_on_offer(db, "rcm", user(db, 2), declined, "decline")
    assert _to(db, "offer_declined") == ["farmer@a.example"]
    expired = data.create_offer(db, "rcm", user(db, 1), **HTA, expires_at="2000-01-01 00:00:00")
    data.expire_stale(db, "rcm")
    assert _to(db, "offer_expired") == ["farmer@a.example"] and expired
    withdrawn = data.create_offer(db, "rcm", user(db, 1), **HTA)
    data.act_on_offer(db, "rcm", user(db, 1), withdrawn, "withdraw")
    assert _to(db, "offer_withdrawn") == ["admin@rcm.example", "sam@rcm.example", "sue@rcm.example"]
    again = data.create_offer(db, "rcm", user(db, 1), **HTA)
    data.act_on_offer(db, "rcm", user(db, 20), again, "withdraw", on_behalf=True)          # staff did it: nobody needs telling
    assert len(_box(db, "offer_withdrawn")) == 3


def test_cancelling_after_a_hit_tells_admins_to_adjust_the_hedge_and_the_farmer(db):
    _setup(db)
    oid = data.create_offer(db, "rcm", user(db, 1), **HTA)
    data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")
    data.cancel_offer(db, "rcm", 20, oid, "Farmer sold elsewhere")
    assert _to(db, "offer_cancelled") == ["admin@rcm.example", "farmer@a.example"]       # admins, NOT staff, plus the farmer
    admin = [r for r in _box(db, "offer_cancelled") if r["to_email"] == "admin@rcm.example"][0]
    assert admin["subject"].startswith("ADJUST HEDGE") and "2.0 contracts of ZCZ26" in admin["body_html"]
    assert "Farmer sold elsewhere" in admin["body_html"]


def test_opt_outs_and_farmers_without_a_login(db):
    _setup(db)
    notify.set_notify(db, 21, False)                                                       # Sue opts out
    notify.set_notify(db, 1, False)                                                        # the farmer opts out
    fid = farmers.create_farmer(db, "rcm", 20, "Phone Caller", email="caller@x.example")
    quiet = farmers.create_farmer(db, "rcm", 20, "Quiet Farmer", email="quiet@x.example")
    notify.set_farmer_notify(db, "rcm", 20, quiet, False)
    data.act_on_offer(db, "rcm", user(db, 2), data.create_offer(db, "rcm", user(db, 1), **HTA), "hit")
    assert _to(db, "offer_hit") == ["admin@rcm.example", "sam@rcm.example"]               # no Sue, no opted-out farmer
    for f in (fid, quiet):
        o = data.place_offer_for_farmer(db, "rcm", 20, f, **HTA)
        data.act_on_offer(db, "rcm", user(db, 2), o, "hit")
    got = _to(db, "offer_hit")
    assert "caller@x.example" in got and "quiet@x.example" not in got                      # record email used when no login
    with pytest.raises(Exception):
        notify.set_farmer_notify(db, "rcm", 1, fid, False)                                 # a farmer can't edit farmer records


def test_each_event_emails_once_even_if_it_is_reported_twice(db):
    _setup(db)
    oid = data.create_offer(db, "rcm", user(db, 1), **HTA)
    before = len(_box(db))
    assert notify.offer_event(db, "rcm", "offer_submitted", oid) == 0 and len(_box(db)) == before


def test_a_notification_failure_never_breaks_the_action(db, monkeypatch):
    _setup(db)

    def boom(*a, **k):
        raise RuntimeError("outbox exploded")
    monkeypatch.setattr(notify, "offer_event", boom)
    oid = data.create_offer(db, "rcm", user(db, 1), **HTA)
    assert data.act_on_offer(db, "rcm", user(db, 2), oid, "hit") == "hit"
    assert _box(db) == []


def test_emails_are_scoped_to_the_tenant(db):
    _setup(db)
    db.execute("INSERT INTO users (user_id, tenant_id, email, name, role) VALUES (30, 'dorans', 'x@dorans.example', 'D', 'elevator_staff')")
    data.act_on_offer(db, "rcm", user(db, 2), data.create_offer(db, "rcm", user(db, 1), **HTA), "hit")
    assert all(not r["to_email"].endswith("@dorans.example") for r in _box(db))
