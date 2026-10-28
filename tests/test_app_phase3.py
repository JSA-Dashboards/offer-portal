from conftest import goto, pages  # noqa: F401
from portal import auth, data, farmers, purchases
from test_app import _as, app_db  # noqa: F401  (fixture + signed-in session helper)
import conftest

HTA = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=10000, basis_cents=-30, futures_symbol="ZCZ26")


def _staff(db):
    db.execute("UPDATE users SET email = 'admin@rcm.example', name = 'Alex Admin' WHERE user_id = 2")
    db.execute("UPDATE users SET email = 'farmer@a.example', name = 'Pat Farmer' WHERE user_id = 1")
    db.execute("INSERT INTO users (user_id, tenant_id, email, name, role) VALUES (20, 'rcm', 'sam@rcm.example', 'Sam Staff', 'elevator_staff')")
    db.execute("INSERT INTO users (user_id, tenant_id, email, name, role) VALUES (21, 'rcm', 'sue@rcm.example', 'Sue Staff', 'elevator_staff')")


def _go(at, view):
    goto(at, view)
    assert not at.exception, at.exception
    return at


def test_navigation_has_purchases_and_recap_for_staff_only(app_db):
    _staff(app_db)
    assert {"Purchases", "Recap"} <= set(pages(_as(app_db, 20)))
    assert not {"Purchases", "Recap"} & set(pages(_as(app_db, 1)))


def test_staff_log_a_purchase_and_see_it_summarised(app_db):
    _staff(app_db)
    at = _go(_as(app_db, 20), "Purchases")
    f = {t.label: t for t in at.text_input}
    f["…or type the entity or person"].set_value("Smith Farms")
    next(n for n in at.number_input if n.label == "Bushels").set_value(5000)
    next(n for n in at.number_input if n.label.startswith("Flat price")).set_value(4.25)
    next(b for b in at.button if b.label == "Log purchase").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    rows = purchases.list_purchases(app_db, "rcm", conftest.user(app_db, 20))
    assert [(p["counterparty"], p["bushels"], p["price"], p["staff_user_id"]) for p in rows] == [("Smith Farms", 5000, 4.25, 20)]
    assert any("Purchase logged." in s.value for s in at.success)
    assert any("Corn" in m.value and "5,000 bu" in m.value and r"avg \$4.2500" in m.value for m in at.markdown)
    assert list(at.dataframe[0].value["Bought from"]) == ["Smith Farms"]


def test_a_purchase_needs_a_price_or_basis_and_a_name(app_db):
    _staff(app_db)
    at = _go(_as(app_db, 20), "Purchases")
    next(n for n in at.number_input if n.label == "Bushels").set_value(5000)
    next(b for b in at.button if b.label == "Log purchase").click().run()
    assert at.error and "flat price" in at.error[0].value.lower()
    assert purchases.list_purchases(app_db, "rcm", conftest.user(app_db, 20)) == []


def test_admin_sees_every_staff_members_purchases_and_staff_only_their_own(app_db):
    _staff(app_db)
    purchases.add_purchase(app_db, "rcm", 20, grain="Corn", bushels=1000, counterparty="From Sam", price=4.0, location_id=1)
    purchases.add_purchase(app_db, "rcm", 21, grain="Corn", bushels=2000, counterparty="From Sue", price=4.1, delivery_period="2026-12")
    admin = _go(_as(app_db, 2), "Purchases")
    df = admin.dataframe[0].value
    assert sorted(df["Bought from"]) == ["From Sam", "From Sue"] and "Logged by" in df.columns
    assert sorted(df["Logged by"]) == ["Sam Staff", "Sue Staff"]
    mine = _go(_as(app_db, 20), "Purchases").dataframe[0].value
    assert list(mine["Bought from"]) == ["From Sam"] and "Logged by" not in mine.columns
    assert list(mine["Delivery location"]) == ["Riverton"]


def test_staff_can_void_their_own_entry(app_db):
    _staff(app_db)
    purchases.add_purchase(app_db, "rcm", 20, grain="Corn", bushels=1000, counterparty="Oops", price=4.0)
    at = _go(_as(app_db, 20), "Purchases")
    next(b for b in at.button if b.label == "Void this purchase").click().run()
    assert not at.exception, at.exception
    assert purchases.list_purchases(app_db, "rcm", conftest.user(app_db, 20)) == []


def test_recap_page_previews_the_email_and_can_send_it_to_me(app_db):
    _staff(app_db)
    purchases.add_purchase(app_db, "rcm", 20, grain="Corn", bushels=1000, counterparty="From Sam", price=4.0)
    at = _go(_as(app_db, 20), "Recap")
    assert any("1 purchase" in c.value for c in at.caption)
    assert not any(b.label == "Send everyone's recap now" for b in at.button)             # admin only
    next(b for b in at.button if b.label == "Email me this recap").click().run()
    assert not at.exception, at.exception
    q = app_db.query("SELECT to_email, kind, subject FROM notifications")
    assert [(r["to_email"], r["kind"]) for r in q] == [("sam@rcm.example", "daily_recap")]
    assert "daily recap" in q[0]["subject"]


def test_admin_can_send_everyones_recap_once(app_db):
    _staff(app_db)
    at = _go(_as(app_db, 2), "Recap")
    next(b for b in at.button if b.label == "Send everyone's recap now").click().run()
    assert not at.exception, at.exception
    assert any("Queued 3 recap emails" in s.value for s in at.success)                    # admin + two staff
    next(b for b in at.button if b.label == "Send everyone's recap now").click().run()
    assert any("already been sent" in s.value for s in at.success)


def test_the_sidebar_switch_turns_my_notifications_off(app_db):
    _staff(app_db)
    at = _as(app_db, 20)
    sw = next(t for t in at.toggle if t.label == "Email me notifications")
    assert sw.value is True
    sw.set_value(False).run()
    assert not at.exception, at.exception
    assert app_db.query("SELECT notify_email FROM users WHERE user_id = 20")[0]["notify_email"] == 0


def test_an_action_in_the_app_sends_the_queued_emails_right_away(app_db, monkeypatch):
    _staff(app_db)
    for k, v in dict(GRAPH_TENANT_ID="t", GRAPH_CLIENT_ID="c", GRAPH_CLIENT_SECRET="s", GRAPH_SENDER="x@y.example").items():
        monkeypatch.setenv(k, v)
    sent = []
    monkeypatch.setattr(auth, "send_via_graph", lambda to, subject, html: sent.append((to, subject)))
    data.create_offer(app_db, "rcm", conftest.user(app_db, 1), **HTA)                      # queues "new offer" emails
    assert sent == []                                                                       # not sent yet (queued only)
    at = _as(app_db, 20)
    next(b for b in at.button if b.label == "Accept Offer").click().run()
    assert not at.exception, at.exception
    got = sorted(to for to, _ in sent)
    assert got == sorted(["admin@rcm.example", "sam@rcm.example", "sue@rcm.example",         # the new-offer emails go out now;
                          "farmer@a.example"])                                               # + the farmer is told it was accepted
    # (the hit emails come later, when the price is reached)
    assert all(r["status"] == "sent" for r in app_db.query("SELECT status FROM notifications"))
