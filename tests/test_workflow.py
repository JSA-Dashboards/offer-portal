from conftest import goto, pages, stage, stages  # noqa: F401
import pytest
from portal import data, farmers
from portal.data import PortalError
from conftest import user
from test_app import _as, app_db  # noqa: F401  (fixture + signed-in session helper)
import conftest

HTA = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=10000, basis_cents=-30, futures_symbol="ZCZ26")
FLAT = dict(grain="Corn", contract_type="CASH", price_type="FIXED", bushels=5000, price=4.30, futures_symbol="ZCZ26")


def _st(db, oid, uid=2):
    return [o for o in data.list_offers(db, "rcm", user(db, uid)) if o["offer_id"] == oid][0]


def _hit(db, **kw):
    oid = data.create_offer(db, "rcm", user(db, 1), **{**HTA, **kw})
    data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")
    return oid


# ── hit -> acknowledged -> completed ─────────────────────────────────────────
def test_acknowledging_records_who_and_when_and_unlocks_completion(db):
    db.execute("INSERT INTO users (user_id, tenant_id, email, name, role) VALUES (20, 'rcm', 's@rcm.com', 'Sam Staff', 'elevator_staff')")
    oid = _hit(db)
    assert _st(db, oid)["status"] == "hit" and _st(db, oid)["acknowledged_at"] is None
    data.acknowledge_offer(db, "rcm", 20, oid)
    o = _st(db, oid)
    assert (o["status"], o["acknowledged_by"], o["acknowledged_by_name"]) == ("acknowledged", 20, "Sam Staff") and o["acknowledged_at"]
    with pytest.raises(PortalError):                                    # contract number still required
        data.complete_offer(db, "rcm", 20, oid, "")
    data.complete_offer(db, "rcm", 20, oid, "C-9001")
    assert _st(db, oid)["status"] == "completed"
    assert [r["event_type"] for r in db.query("SELECT event_type FROM offer_events WHERE offer_id = %s ORDER BY event_id", (oid,))] \
        == ["submitted", "hit", "acknowledged", "completed"]


def test_only_a_hit_offer_can_be_acknowledged_and_only_by_staff_of_the_tenant(db):
    outstanding = data.create_offer(db, "rcm", user(db, 1), **HTA)
    with pytest.raises(PortalError):
        data.acknowledge_offer(db, "rcm", 2, outstanding)               # not hit yet
    oid = _hit(db)
    for uid, tenant in ((1, "rcm"), (4, "rcm"), (4, "dorans")):
        with pytest.raises(PortalError):
            data.acknowledge_offer(db, tenant, uid, oid)                # farmer / another tenant's admin
    data.acknowledge_offer(db, "rcm", 2, oid)
    with pytest.raises(PortalError):
        data.acknowledge_offer(db, "rcm", 2, oid)                       # already acknowledged
    with pytest.raises(PortalError):
        data.acknowledge_offer(db, "rcm", 2, 987654321)


def test_an_offer_hit_automatically_also_waits_for_acknowledgement(db):
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", basis_cents=-38, cash_price=4.50)
    data.add_auto_hit_rule(db, "rcm", 2)
    oid = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=data.list_bids(db, "rcm")[0]["bid_id"])
    assert _st(db, oid)["status"] == "hit" and _st(db, oid)["auto_hit"]
    with pytest.raises(PortalError):
        data.complete_offer(db, "rcm", 2, oid, "C-1")                   # automation never skips the human steps
    data.acknowledge_offer(db, "rcm", 2, oid)
    data.complete_offer(db, "rcm", 2, oid, "C-1")


# ── cancel after a hit -> admin hedge adjustment ─────────────────────────────
def test_cancelling_a_hit_offer_needs_a_reason_and_flags_the_hedge_for_admins(db):
    db.execute("INSERT INTO users (user_id, tenant_id, email, name, role) VALUES (20, 'rcm', 's@rcm.com', 'Sam Staff', 'elevator_staff')")
    oid = _hit(db)
    data.mark_hedged(db, "rcm", 2, [oid])                                # the elevator had already hedged it
    for bad in ("", "   ", "x" * 201):
        with pytest.raises(PortalError):
            data.cancel_offer(db, "rcm", 20, oid, bad)
    data.cancel_offer(db, "rcm", 20, oid, " Farmer called to cancel ")
    o = _st(db, oid)
    assert (o["status"], o["cancel_reason"], o["cancelled_by"], o["hedge_adjust_needed"]) == ("cancelled", "Farmer called to cancel", 20, 1)
    adj = data.hedge_adjustments(db, "rcm", 2)
    assert [(a["offer_id"], a["was_hedged"], a["contracts"]) for a in adj] == [(oid, True, 2.0)]
    ev = db.query("SELECT detail FROM offer_events WHERE offer_id = %s AND event_type = 'cancelled'", (oid,))[0]["detail"]
    assert '"was_hedged": true' in ev and '"from_status": "hit"' in ev
    assert data.hedge_summary(db, "rcm", 2)["now"] == []                 # a cancelled offer is no longer to be hedged
    assert [a["offer_id"] for a in data.hedge_summary(db, "rcm", 2)["adjustments"]] == [oid]


def test_staff_can_cancel_hit_and_acknowledged_but_only_admins_cancel_a_completed_offer(db):
    db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 's@rcm.com', 'elevator_staff')")
    hit, ack, done = _hit(db), _hit(db), _hit(db)
    data.acknowledge_offer(db, "rcm", 2, ack)
    data.acknowledge_offer(db, "rcm", 2, done)
    data.complete_offer(db, "rcm", 2, done, "C-1")
    data.cancel_offer(db, "rcm", 20, hit, "reason")
    data.cancel_offer(db, "rcm", 20, ack, "reason")
    with pytest.raises(PortalError):
        data.cancel_offer(db, "rcm", 20, done, "reason")                 # completed: admin only
    data.cancel_offer(db, "rcm", 2, done, "contract voided")
    assert {_st(db, x)["status"] for x in (hit, ack, done)} == {"cancelled"}


def test_cancel_is_for_offers_that_were_hit_and_for_this_tenants_staff(db):
    outstanding = data.create_offer(db, "rcm", user(db, 1), **HTA)
    with pytest.raises(PortalError):
        data.cancel_offer(db, "rcm", 2, outstanding, "r")               # use decline / withdraw before a hit
    oid = _hit(db)
    for uid, tenant in ((1, "rcm"), (4, "rcm"), (4, "dorans")):
        with pytest.raises(PortalError):
            data.cancel_offer(db, tenant, uid, oid, "r")
    with pytest.raises(PortalError):
        data.cancel_offer(db, "rcm", 2, 987654321, "r")
    data.cancel_offer(db, "rcm", 2, oid, "r")
    with pytest.raises(PortalError):
        data.cancel_offer(db, "rcm", 2, oid, "r")                       # already cancelled


def test_admin_confirms_the_hedge_adjustment_and_the_list_clears(db):
    db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 's@rcm.com', 'elevator_staff')")
    a, b = _hit(db), _hit(db)
    data.cancel_offer(db, "rcm", 2, a, "r")
    data.cancel_offer(db, "rcm", 2, b, "r")
    for uid, tenant in ((20, "rcm"), (1, "rcm"), (4, "rcm")):          # staff / farmer / another tenant's admin
        with pytest.raises(PortalError):
            data.mark_hedge_adjusted(db, tenant, uid, [a])
        with pytest.raises(PortalError):
            data.hedge_adjustments(db, tenant, uid)
    assert data.mark_hedge_adjusted(db, "rcm", 2, [a, 987654321]) == 1
    assert data.mark_hedge_adjusted(db, "rcm", 2, [a]) == 0             # once only
    assert [x["offer_id"] for x in data.hedge_adjustments(db, "rcm", 2)] == [b]
    o = _st(db, a)
    assert o["hedge_adjusted_at"] is not None and o["status"] == "cancelled"
    assert "hedge_adjusted" in {r["event_type"] for r in db.query("SELECT event_type FROM offer_events WHERE offer_id = %s", (a,))}


def test_acknowledged_offers_still_count_as_to_hedge_until_marked_hedged(db):
    oid = _hit(db)
    data.acknowledge_offer(db, "rcm", 2, oid)
    assert data.hedge_summary(db, "rcm", 2)["now_totals"][0]["bushels"] == 10000
    assert data.mark_hedged(db, "rcm", 2, [oid]) == 1
    assert data.hedge_summary(db, "rcm", 2)["now"] == []
    other = _hit(db)
    data.acknowledge_offer(db, "rcm", 2, other)
    assert {o["offer_id"] for o in data.offer_summary(db, "rcm", 2)["offers"]} == {oid, other}   # summary lists every open stage
    assert len(data.offer_summary(db, "rcm", 2, statuses=("acknowledged",))["offers"]) == 2


# ── UI ───────────────────────────────────────────────────────────────────────
def test_staff_acknowledge_then_cancel_from_the_offer_cards(app_db):
    oid = _hit(app_db)
    at = _as(app_db, 2)
    stage(at, "Hit")
    assert not at.exception, at.exception
    assert any("Cancel this offer" == e.label for e in at.expander)
    next(t for t in at.text_input if t.label.startswith("Reason (required)")).set_value("Farmer sold elsewhere")
    next(b for b in at.button if b.label == "Cancel offer").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    o = _st(app_db, oid)
    assert (o["status"], o["cancel_reason"]) == ("cancelled", "Farmer sold elsewhere")
    stage(at, "Closed")
    assert any("Cancelled" in c.value and "Farmer sold elsewhere" in c.value and "hedge adjustment pending" in c.value for c in at.caption)


def test_cancel_without_a_reason_is_refused_on_screen(app_db):
    _hit(app_db)
    at = _as(app_db, 2)
    stage(at, "Hit")
    next(b for b in at.button if b.label == "Cancel offer").click().run()
    assert at.error and "reason" in at.error[0].value.lower()


def test_hedge_page_lists_cancelled_offers_until_the_admin_confirms(app_db):
    oid = _hit(app_db)
    data.cancel_offer(app_db, "rcm", 2, oid, "Farmer called to cancel")
    at = _as(app_db, 2)
    goto(at, "Hedge")
    assert not at.exception, at.exception
    assert any("Cancelled offers: adjust your hedges" in s.value for s in at.subheader)
    assert any("Farmer called to cancel" in m.value for m in at.markdown)
    next(b for b in at.button if b.label == "Hedge adjusted").click().run()
    assert not at.exception, at.exception
    assert data.hedge_adjustments(app_db, "rcm", 2) == []
    assert not any("Cancelled offers: adjust your hedges" in s.value for s in at.subheader)
