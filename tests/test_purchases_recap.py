from datetime import date, timedelta

import pytest
from portal import data, farmers, notify, purchases, recap
from portal.data import PortalError
from conftest import user

HTA = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=10000, basis_cents=-30, futures_symbol="ZCZ26")
FLAT = dict(grain="Corn", contract_type="CASH", price_type="FIXED", bushels=5000, price=4.30, futures_symbol="ZCZ26")


def _setup(db):
    db.execute("UPDATE users SET email = 'admin@rcm.example', name = 'Alex Admin' WHERE user_id = 2")
    db.execute("INSERT INTO users (user_id, tenant_id, email, name, role) VALUES (20, 'rcm', 'sam@rcm.example', 'Sam Staff', 'elevator_staff')")
    db.execute("INSERT INTO users (user_id, tenant_id, email, name, role) VALUES (21, 'rcm', 'sue@rcm.example', 'Sue Staff', 'elevator_staff')")
    db.execute("INSERT INTO locations (location_id, tenant_id, name, state) VALUES (1, 'rcm', 'Riverton', 'IL')")


def _buy(db, uid=20, **kw):
    base = dict(grain="Corn", bushels=5000, counterparty="Smith Farms", price=4.25, delivery_period="2026-12", location_id=1)
    return purchases.add_purchase(db, "rcm", uid, **{**base, **kw})


# ── logging a purchase ───────────────────────────────────────────────────────
def test_staff_log_a_purchase_by_farmer_record_or_typed_name(db):
    _setup(db)
    fid = farmers.create_farmer(db, "rcm", 20, "Pat Farmer", entity_name="Farmer Bros LLC", client_id="10442")
    a = _buy(db, farmer_id=fid, counterparty=None, basis_cents=-30, futures_symbol="zcz26", price=None)
    b = _buy(db, counterparty="  Walk-in Joe ")
    rows = {p["purchase_id"]: p for p in purchases.list_purchases(db, "rcm", user(db, 20))}
    assert rows[a]["counterparty"] == "Farmer Bros LLC (Pat Farmer)" and rows[a]["farmer_id"] == fid
    assert (rows[a]["futures_symbol"], rows[a]["basis_cents"], rows[a]["price"]) == ("ZCZ26", -30, None)
    assert rows[b]["counterparty"] == "Walk-in Joe" and rows[b]["delivery_location"] == "Riverton" and rows[b]["staff_name"] == "Sam Staff"


def test_purchase_validation(db):
    _setup(db)
    bad = [dict(grain="Barley"), dict(bushels=0), dict(bushels=-5), dict(price=None), dict(price=0), dict(counterparty=" "),
           dict(counterparty="x" * 121), dict(basis_cents=-20, price=None), dict(delivery_period="Dec 2026"),
           dict(delivery_period="2026-13"), dict(purchase_date=date.today() + timedelta(days=2)), dict(location_id=999),
           dict(farmer_id=999, counterparty=None), dict(contract_ref="r" * 61)]
    for kw in bad:
        with pytest.raises(PortalError):
            _buy(db, **kw)
    _buy(db, price=None, basis_cents=-20, futures_symbol="ZCZ26")           # basis with a futures month is fine
    _buy(db, delivery_period=None, location_id=None)                         # location and period are optional


def test_only_staff_of_the_tenant_log_or_see_purchases(db):
    _setup(db)
    for uid, tenant in ((1, "rcm"), (4, "rcm")):                              # a farmer; another tenant's admin
        with pytest.raises(PortalError):
            purchases.add_purchase(db, tenant, uid, grain="Corn", bushels=1000, counterparty="x", price=4.0)
        with pytest.raises(PortalError):
            purchases.list_purchases(db, tenant, user(db, uid))
    _buy(db)
    assert purchases.list_purchases(db, "dorans", user(db, 4)) == []


def test_staff_see_their_own_purchases_and_admins_see_everyones(db):
    _setup(db)
    mine, theirs = _buy(db, 20, counterparty="A"), _buy(db, 21, counterparty="B")
    assert [p["purchase_id"] for p in purchases.list_purchases(db, "rcm", user(db, 20))] == [mine]
    assert [p["purchase_id"] for p in purchases.list_purchases(db, "rcm", user(db, 21))] == [theirs]
    assert {p["purchase_id"] for p in purchases.list_purchases(db, "rcm", user(db, 2))} == {mine, theirs}
    assert [p["purchase_id"] for p in purchases.list_purchases(db, "rcm", user(db, 2), staff_id=21)] == [theirs]
    assert [p["purchase_id"] for p in purchases.list_purchases(db, "rcm", user(db, 21), staff_id=20)] == [theirs]   # a filter can't widen staff access


def test_purchases_are_listed_by_day_and_voided_ones_disappear(db):
    _setup(db)
    today, yesterday = _buy(db), _buy(db, purchase_date=date.today() - timedelta(days=1))
    assert [p["purchase_id"] for p in purchases.list_purchases(db, "rcm", user(db, 20))] == [today]
    assert [p["purchase_id"] for p in purchases.list_purchases(db, "rcm", user(db, 20), date.today() - timedelta(days=1))] == [yesterday]
    with pytest.raises(PortalError):
        purchases.void_purchase(db, "rcm", user(db, 21), today)               # not Sue's
    purchases.void_purchase(db, "rcm", user(db, 20), today)
    assert purchases.list_purchases(db, "rcm", user(db, 20)) == []
    with pytest.raises(PortalError):
        purchases.void_purchase(db, "rcm", user(db, 20), today)               # already void
    other = _buy(db, 21)
    purchases.void_purchase(db, "rcm", user(db, 2), other)                    # an admin can void anyone's


def test_summary_by_commodity_with_weighted_averages(db):
    _setup(db)
    _buy(db, bushels=10000, price=4.00)
    _buy(db, bushels=30000, price=4.40)
    _buy(db, bushels=5000, price=None, basis_cents=-40, futures_symbol="ZCZ26", counterparty="Basis Buyer")
    _buy(db, grain="Soybeans", bushels=2000, price=11.50, counterparty="Bean Co", delivery_period="2027-01")
    g = {x["grain"]: x for x in purchases.summarize(purchases.list_purchases(db, "rcm", user(db, 20)))}
    assert list(g) == ["Corn", "Soybeans"]
    assert (g["Corn"]["entries"], g["Corn"]["bushels"]) == (3, 45000)
    assert g["Corn"]["avg_price"] == pytest.approx(4.30) and g["Corn"]["avg_basis_cents"] == -40.0   # only priced / only basis rows count
    assert g["Soybeans"]["rows"][0]["counterparty"] == "Bean Co" and g["Soybeans"]["avg_basis_cents"] is None


# ── the recap ────────────────────────────────────────────────────────────────
def test_a_staff_recap_has_their_own_purchases_and_offers_plus_the_company_to_dos(db):
    _setup(db)
    _buy(db, 20, counterparty="Sam Bought From")
    _buy(db, 21, counterparty="Sue Bought From")
    f = farmers.create_farmer(db, "rcm", 20, "Phone Caller")
    mine = data.place_offer_for_farmer(db, "rcm", 20, f, **HTA)                                # Sam entered it, still working
    other = data.place_offer_for_farmer(db, "rcm", 21, f, **{**HTA, "bushels": 7000})           # Sue's
    hit = data.place_offer_for_farmer(db, "rcm", 21, f, **{**HTA, "bushels": 3000})
    data.act_on_offer(db, "rcm", user(db, 2), hit, "hit")                                      # hit, waiting for acknowledgement
    r = recap.build_recap(db, "rcm", dict(user_id=20, role="elevator_staff"))
    h = r["html"]
    assert "Sam Bought From" in h and "Sue Bought From" not in h
    assert "10,000" in h and "7,000" not in h                                                   # his offer, not Sue's
    assert "waiting for Confirm Purchase" in h and "3,000" in h                                 # company to-do list is shared
    assert r["counts"] == dict(hit_today=0, working=0, outstanding=1, need_ack=1, need_contract=0, purchases=1)
    assert "substantial risk of loss" in h and "your activity" in h and "Logged by" not in h
    assert mine and other


def test_an_admin_recap_has_everything_the_hedge_and_pending_adjustments(db):
    _setup(db)
    _buy(db, 20, counterparty="Sam Bought From")
    _buy(db, 21, counterparty="Sue Bought From")
    f = farmers.create_farmer(db, "rcm", 20, "Phone Caller")
    o = data.place_offer_for_farmer(db, "rcm", 20, f, **HTA)
    data.act_on_offer(db, "rcm", user(db, 2), o, "hit")
    data.acknowledge_offer(db, "rcm", 20, o)
    c = data.place_offer_for_farmer(db, "rcm", 20, f, **HTA)
    data.act_on_offer(db, "rcm", user(db, 2), c, "hit")
    data.cancel_offer(db, "rcm", 20, c, "gone")
    r = recap.build_recap(db, "rcm", dict(user_id=2, role="elevator_admin"))
    h = r["html"]
    assert "Sam Bought From" in h and "Sue Bought From" in h and "Logged by" in h and "all company activity" in h
    assert "Hedge position (admin)" in h and "ZCZ26" in h and "waiting for a contract number" in h
    assert "1</b> cancelled offer still need a hedge adjustment" in h
    assert r["counts"]["purchases"] == 2 + 1 and r["counts"]["hit_today"] == 2       # the acknowledged offer is a purchase too
    assert "includes offers acknowledged today" in h and ">Offer<" in h


def test_hits_are_dated_in_central_time_and_only_today_counts(db):
    _setup(db)
    f = farmers.create_farmer(db, "rcm", 20, "Phone Caller")
    o = data.place_offer_for_farmer(db, "rcm", 20, f, **HTA)
    data.act_on_offer(db, "rcm", user(db, 2), o, "hit")
    db.execute("UPDATE offers SET hit_at = '2026-10-26 02:30:00' WHERE offer_id = %s", (o,))      # 9:30 pm Central on the 25th
    admin = dict(user_id=2, role="elevator_admin")
    assert recap.build_recap(db, "rcm", admin, date(2026, 10, 25))["counts"]["hit_today"] == 1
    assert recap.build_recap(db, "rcm", admin, date(2026, 10, 26))["counts"]["hit_today"] == 0


def test_recap_escapes_names_and_says_so_when_there_is_nothing(db):
    _setup(db)
    _buy(db, counterparty="<script>alert(1)</script>")
    h = recap.build_recap(db, "rcm", dict(user_id=20, role="elevator_staff"))["html"]
    assert "<script>alert(1)</script>" not in h and "&lt;script&gt;" in h
    empty = recap.build_recap(db, "rcm", dict(user_id=21, role="elevator_staff"))["html"]
    assert "No offers filled today." in empty and "No outstanding offers." in empty and "No working offers." in empty
    assert "No purchases were logged today." in empty


def test_daily_recaps_go_to_staff_and_admins_once_a_day(db):
    _setup(db)
    db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (30, 'dorans', 'd@dorans.example', 'elevator_staff')")
    assert recap.send_daily_recaps(db, date(2026, 10, 26), "rcm") == dict(day="2026-10-26", queued=3)
    to = sorted(r["to_email"] for r in db.query("SELECT to_email FROM notifications WHERE kind = 'daily_recap'"))
    assert to == ["admin@rcm.example", "sam@rcm.example", "sue@rcm.example"]                   # not farmers, not other tenants
    assert recap.send_daily_recaps(db, date(2026, 10, 26), "rcm")["queued"] == 0              # safe to re-run
    assert recap.send_daily_recaps(db, date(2026, 10, 27), "rcm")["queued"] == 3              # a new day
    notify.set_notify(db, 21, False)
    assert recap.send_daily_recaps(db, date(2026, 10, 28), "rcm")["queued"] == 2              # Sue opted out
    assert recap.send_daily_recaps(db, date(2026, 10, 28))["queued"] == 2                      # all tenants: dorans' staffer and admin
    sent = []
    assert notify.send_pending(db, send=lambda t, s, h: sent.append(t))["sent"] == 3 + 3 + 2 + 2
    assert "admin@rcm.example" in sent


# ── acknowledged offers become the day's purchases ───────────────────────────
def _offer_hit(db, **terms):
    f = farmers.create_farmer(db, "rcm", 20, "Pat Phone", "555-1212")
    o = data.place_offer_for_farmer(db, "rcm", 20, f, **{**HTA, **terms})
    data.act_on_offer(db, "rcm", user(db, 2), o, "hit")
    return o


def _todays(db, uid=2):
    return purchases.list_purchases(db, "rcm", user(db, uid))


def test_a_hit_offer_is_not_a_purchase_until_staff_acknowledge_it(db):
    _setup(db)
    o = _offer_hit(db)
    assert _todays(db) == []
    data.acknowledge_offer(db, "rcm", 20, o)
    (p,) = _todays(db)
    assert (p["offer_id"], p["staff_user_id"], p["counterparty"], p["grain"], p["bushels"], p["futures_symbol"]) == \
           (o, 20, "Pat Phone", "Corn", HTA["bushels"], "ZCZ26")
    assert p["basis_cents"] == HTA["basis_cents"] and p["price"] is None            # an HTA offer logs its basis
    assert "From offer" in p["note"]
    assert [x["offer_id"] for x in _todays(db, 20)] == [o]                           # the staffer who acknowledged it sees it
    assert _todays(db, 21) == []                                                     # another staffer does not


def test_acknowledging_twice_or_completing_never_makes_a_second_purchase_and_the_contract_number_follows(db):
    _setup(db)
    o = _offer_hit(db)
    data.acknowledge_offer(db, "rcm", 20, o)
    with pytest.raises(PortalError):
        data.acknowledge_offer(db, "rcm", 20, o)
    data.complete_offer(db, "rcm", 20, o, "C-7788")
    (p,) = _todays(db)
    assert p["contract_ref"] == "C-7788"
    # a manual entry for the same grain is separate, and the recap lists both with their source
    _buy(db, 20, counterparty="Walk-in Farms")
    h = recap.build_recap(db, "rcm", dict(user_id=2, role="elevator_admin"))["html"]
    assert "Pat Phone" in h and "Walk-in Farms" in h and ">Offer<" in h and ">Entered<" in h


def test_a_flat_price_offer_logs_its_price_and_the_basis_it_was_struck_against(db):
    _setup(db)
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", basis_cents=-38, futures_symbol="ZCZ26", location_id=1)
    bid = data.list_bids(db, "rcm")[0]["bid_id"]
    f = farmers.create_farmer(db, "rcm", 20, "Pat Phone", "555-1212")
    o = data.place_offer_for_farmer(db, "rcm", 20, f, grain="Corn", contract_type="CASH", price_type="FIXED", bushels=5000,
                                    price=4.30, futures_symbol="ZCZ26", bid_id=bid)
    data.act_on_offer(db, "rcm", user(db, 2), o, "hit")
    data.acknowledge_offer(db, "rcm", 20, o)
    (p,) = _todays(db)
    assert p["price"] == 4.30 and p["basis_cents"] == -38 and p["delivery_period"] == "2999-12" and p["delivery_location"]


def test_cancelling_an_offer_after_its_hit_voids_its_purchase(db):
    _setup(db)
    o = _offer_hit(db)
    data.acknowledge_offer(db, "rcm", 20, o)
    assert len(_todays(db)) == 1
    data.cancel_offer(db, "rcm", 20, o, "farmer changed mind")
    assert _todays(db) == []
    assert db.query("SELECT voided_at FROM purchases WHERE offer_id = %s", (o,))[0]["voided_at"] is not None
    assert [r["action"] for r in db.query("SELECT action FROM audit_log WHERE action = 'purchase_voided'")] == ["purchase_voided"]


def test_without_sql_13_acknowledging_still_works_and_just_skips_the_purchase(db, monkeypatch):
    _setup(db)
    real = db.query
    monkeypatch.setattr(db, "query", lambda sql, params=(): (_ for _ in ()).throw(RuntimeError("no such column"))
                        if "SELECT offer_id FROM purchases" in sql else real(sql, params))
    o = _offer_hit(db)
    data.acknowledge_offer(db, "rcm", 20, o)
    assert [x["status"] for x in db.query("SELECT status FROM offers WHERE offer_id = %s", (o,))] == ["acknowledged"]
    assert _todays(db) == []
