import pytest
from portal import data, farmers
from portal.data import PortalError
from conftest import user

FLAT = dict(grain="Corn", contract_type="CASH", price_type="FIXED", bushels=5000, price=4.30, futures_symbol="ZCZ26")
HTA = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=5000, basis_cents=-30, futures_symbol="ZCZ26")


def _bid(db, basis=-38, cash=4.18, tenant="rcm", actor=2, **kw):
    kw = {**dict(grain="Corn", delivery_month="2026-12", futures_symbol="ZCZ26"), **kw}
    return data.publish_bid(db, tenant, actor, basis_cents=basis, cash_price=cash, **kw)


def _offers(db, tenant="rcm", uid=2):
    return {o["offer_id"]: o for o in data.list_offers(db, tenant, user(db, uid))}


def _live_bid(db):
    return data.list_bids(db, "rcm")[0]["bid_id"]


# ── flat-price offers follow the basis ───────────────────────────────────────
def test_flat_offer_gets_implied_futures_from_the_bid_it_was_made_against(db):
    _bid(db, basis=-38)
    oid = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=_live_bid(db))
    o = _offers(db)[oid]
    assert (o["basis_ref_cents"], o["implied_futures"]) == (-38, 4.68)          # 4.30 flat - (-0.38)


def test_basis_change_reprices_outstanding_flat_offers_better_or_worse(db):
    _bid(db, basis=-38)
    oid = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=_live_bid(db))
    assert _bid(db, basis=-30) == 1                                              # basis improved
    o = _offers(db)[oid]
    assert (o["price"], o["basis_ref_cents"], o["implied_futures"]) == (4.30, -30, 4.60)   # flat price untouched
    assert _bid(db, basis=-45) == 1                                              # basis got worse
    o = _offers(db)[oid]
    assert (o["price"], o["basis_ref_cents"], o["implied_futures"]) == (4.30, -45, 4.75)
    assert o["bid_id"] == _live_bid(db)                                          # still linked to the live bid
    ev = db.query("SELECT detail, actor_user_id FROM offer_events WHERE offer_id = %s AND event_type = 'basis_updated' "
                  "ORDER BY event_id", (oid,))
    assert len(ev) == 2 and ev[1]["actor_user_id"] == 2
    assert '"basis_after": -45' in ev[1]["detail"] and '"implied_futures_after": 4.75' in ev[1]["detail"]
    assert len(data.list_bids(db, "rcm")) == 1                                   # one live bid per slot


def test_republishing_the_same_basis_changes_nothing(db):
    _bid(db, basis=-38)
    data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=_live_bid(db))
    assert _bid(db, basis=-38) == 0


def test_only_outstanding_flat_offers_are_repriced(db):
    _bid(db, basis=-38)
    bid = _live_bid(db)
    flat_hit = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=bid)
    data.act_on_offer(db, "rcm", user(db, 2), flat_hit, "hit")                  # locked at the basis it was hit on
    hta = data.create_offer(db, "rcm", user(db, 1), **HTA, bid_id=bid)
    fut = data.create_offer(db, "rcm", user(db, 1), **{**FLAT, "contract_type": "FUTURES_ONLY", "price": 4.55}, bid_id=bid)
    flat = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=bid)
    _bid(db, basis=-30)
    o = _offers(db)
    assert o[flat]["implied_futures"] == 4.60
    assert o[flat_hit]["implied_futures"] == 4.68 and o[flat_hit]["basis_ref_cents"] == -38
    assert o[hta]["implied_futures"] is None and o[hta]["basis_ref_cents"] is None
    assert o[fut]["implied_futures"] == 4.55                                     # a futures-only offer IS the futures price


def test_other_tenants_offers_are_never_repriced(db):
    _bid(db, basis=-38)
    data.publish_bid(db, "dorans", 4, grain="Corn", delivery_month="2026-12", futures_symbol="ZCZ26",
                     basis_cents=-20, cash_price=4.36)
    theirs = data.create_offer(db, "dorans", user(db, 3), **FLAT, bid_id=data.list_bids(db, "dorans")[0]["bid_id"])
    before = _offers(db, "dorans", 4)[theirs]["implied_futures"]
    _bid(db, basis=-50)
    assert _offers(db, "dorans", 4)[theirs]["implied_futures"] == before == 4.50


def test_open_priced_flat_offer_follows_the_bid_for_its_futures_and_delivery_month(db):
    _bid(db, basis=-38)
    match = data.create_offer(db, "rcm", user(db, 1), **FLAT, delivery_start="2026-12-05")
    wrong_month = data.create_offer(db, "rcm", user(db, 1), **FLAT, delivery_start="2027-01-05")
    wrong_symbol = data.create_offer(db, "rcm", user(db, 1), **{**FLAT, "futures_symbol": "ZCH27"}, delivery_start="2026-12-05")
    no_dates = data.create_offer(db, "rcm", user(db, 1), **FLAT)
    o = _offers(db)
    assert o[match]["implied_futures"] == 4.68 and o[match]["bid_id"] is None
    assert all(o[x]["implied_futures"] is None for x in (wrong_month, wrong_symbol, no_dates))   # nothing prices them yet
    assert _bid(db, basis=-30) == 1
    o = _offers(db)
    assert o[match]["implied_futures"] == 4.60 and o[match]["basis_ref_cents"] == -30
    assert all(o[x]["implied_futures"] is None for x in (wrong_month, wrong_symbol, no_dates))
    _bid(db, basis=-25, delivery_month="2027-01")                                # a bid for the later month prices that one
    assert _offers(db)[wrong_month]["implied_futures"] == 4.55


def test_every_offer_needs_a_futures_month(db):
    for kw in (dict(futures_symbol=None), dict(futures_symbol=" ")):
        with pytest.raises(PortalError):
            data.create_offer(db, "rcm", user(db, 1), **{**FLAT, **kw})


# ── hit -> completed (archive) ───────────────────────────────────────────────
def test_complete_requires_a_contract_number_and_a_hit_offer(db):
    oid = data.create_offer(db, "rcm", user(db, 1), **HTA)
    with pytest.raises(PortalError):                                             # not hit yet
        data.complete_offer(db, "rcm", 2, oid, "C-1")
    data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")
    with pytest.raises(PortalError):                                             # hit but not acknowledged yet
        data.complete_offer(db, "rcm", 2, oid, "C-1")
    data.acknowledge_offer(db, "rcm", 2, oid)
    for blank in ("", "   ", None):
        with pytest.raises(PortalError):
            data.complete_offer(db, "rcm", 2, oid, blank)
    for uid, tenant in ((1, "rcm"), (4, "rcm"), (4, "dorans")):                 # farmer / other tenant's admin
        with pytest.raises(PortalError):
            data.complete_offer(db, tenant, uid, oid, "C-1")
    data.complete_offer(db, "rcm", 2, oid, " C-5531 ")
    o = _offers(db)[oid]
    assert (o["status"], o["contract_ref"], o["completed_by"]) == ("completed", "C-5531", 2) and o["completed_at"]
    with pytest.raises(PortalError):                                             # can't complete twice
        data.complete_offer(db, "rcm", 2, oid, "C-5531")
    with pytest.raises(PortalError):                                             # an archived offer keeps its number
        data.update_offer_details(db, "rcm", 2, oid, note="x", contract_ref="")
    data.update_offer_details(db, "rcm", 2, oid, note="corrected", contract_ref="C-5532")
    assert _offers(db)[oid]["contract_ref"] == "C-5532"
    assert {"hit", "acknowledged", "completed"} <= {r["event_type"] for r in db.query(
        "SELECT event_type FROM offer_events WHERE offer_id = %s", (oid,))}


def test_a_contract_number_entered_earlier_is_kept_and_a_hit_offer_cannot_be_declined(db):
    oid = data.create_offer(db, "rcm", user(db, 1), **HTA, contract_ref="PRE-1")
    data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")
    with pytest.raises(PortalError):
        data.act_on_offer(db, "rcm", user(db, 2), oid, "decline")
    with pytest.raises(PortalError):
        data.act_on_offer(db, "rcm", user(db, 1), oid, "withdraw")
    data.acknowledge_offer(db, "rcm", 2, oid)
    with pytest.raises(PortalError):
        data.act_on_offer(db, "rcm", user(db, 2), oid, "decline")                # still not declinable once acknowledged
    data.complete_offer(db, "rcm", 2, oid, "PRE-1")
    assert _offers(db)[oid]["status"] == "completed"


# ── the posted bid reaching an offer ─────────────────────────────────────────
def test_bid_reached_flags_offers_the_market_has_come_up_to(db):
    _bid(db, basis=-38, cash=4.18)
    bid = _live_bid(db)
    cheap = data.create_offer(db, "rcm", user(db, 1), **{**FLAT, "price": 4.10}, bid_id=bid)    # bid 4.18 >= 4.10
    dear = data.create_offer(db, "rcm", user(db, 1), **{**FLAT, "price": 4.40}, bid_id=bid)     # bid 4.18 < 4.40
    basis_ok = data.create_offer(db, "rcm", user(db, 1), **{**HTA, "basis_cents": -40}, bid_id=bid)  # -38 >= -40
    basis_no = data.create_offer(db, "rcm", user(db, 1), **{**HTA, "basis_cents": -30}, bid_id=bid)  # -38 < -30
    o = _offers(db)
    assert [o[x]["bid_reached"] for x in (cheap, dear, basis_ok, basis_no)] == [True, False, True, False]
    data.act_on_offer(db, "rcm", user(db, 2), cheap, "hit")
    assert _offers(db)[cheap]["bid_reached"] is False                            # only outstanding offers are flagged


# ── summary page ────────────────────────────────────────────────────────────
def _seed_summary(db):
    pat = farmers.create_farmer(db, "rcm", 2, "Pat Farmer", entity_name="Farmer Bros LLC", client_id="1")
    joe = farmers.create_farmer(db, "rcm", 2, "Joe Solo", entity_name="Smith Farms", client_id="2")
    _bid(db, basis=-38)
    place = lambda fid, **kw: data.place_offer_for_farmer(db, "rcm", 2, fid, **{**FLAT, **kw})
    return dict(
        a=place(pat, bushels=5000, price=4.30, delivery_start="2026-12-05"),
        b=place(pat, bushels=15000, price=4.50, delivery_start="2026-12-20"),
        c=place(joe, grain="Soybeans", futures_symbol="ZSX26", bushels=10000, price=12.00, delivery_start="2027-01-10"),
        d=data.place_offer_for_farmer(db, "rcm", 2, joe, **HTA, delivery_start="2026-12-01"))


def test_summary_groups_by_commodity_and_delivery_period_with_weighted_averages(db):
    ids = _seed_summary(db)
    res = data.offer_summary(db, "rcm", 2)
    g = {(x["grain"], x["period"]): x for x in res["groups"]}
    assert set(g) == {("Corn", "2026-12"), ("Soybeans", "2027-01")}
    corn = g[("Corn", "2026-12")]
    assert (corn["offers"], corn["bushels"]) == (3, 25000)
    assert corn["avg_flat_price"] == pytest.approx((5000 * 4.30 + 15000 * 4.50) / 20000)        # weighted by bushels
    assert corn["avg_basis_cents"] == -30.0                                                       # the HTA offer
    assert len(res["offers"]) == 4
    assert [x["grain"] for x in res["groups"]] == ["Corn", "Soybeans"]                            # sorted
    assert ids["a"] in {o["offer_id"] for o in res["offers"]}


def test_summary_filters(db):
    _seed_summary(db)
    q = lambda **kw: data.offer_summary(db, "rcm", 2, **kw)["offers"]
    assert len(q(grains=["Soybeans"])) == 1
    assert len(q(periods=["2026-12"])) == 3
    assert len(q(entity="farmer bros")) == 2                                      # case-insensitive contains
    assert len(q(individual="joe")) == 2
    assert len(q(entity="smith", grains=["Corn"])) == 1                           # filters combine
    assert q(entity="nobody") == []
    assert q(statuses=("completed",)) == []
    one = q(grains=["Soybeans"])[0]["offer_id"]
    data.act_on_offer(db, "rcm", user(db, 2), one, "hit")
    assert len(q(statuses=("hit",))) == 1 and len(q()) == 4                       # default view includes outstanding + hit


def test_summary_is_staff_only_and_tenant_scoped(db):
    _seed_summary(db)
    for uid, tenant in ((1, "rcm"), (4, "rcm")):
        with pytest.raises(PortalError):
            data.offer_summary(db, tenant, uid)
    assert data.offer_summary(db, "dorans", 4)["offers"] == []


# ── hedge page ──────────────────────────────────────────────────────────────
def test_hedge_summary_counts_contracts_by_futures_month_and_price_level(db):
    _bid(db, basis=-38)
    bid = _live_bid(db)
    f = user(db, 1)
    hit = [data.create_offer(db, "rcm", f, **{**FLAT, "bushels": b, "price": p}, bid_id=bid)
           for b, p in ((5000, 4.30), (7500, 4.30), (5000, 4.40))]
    for oid in hit:
        data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")
    data.create_offer(db, "rcm", f, **{**HTA, "bushels": 10000}, bid_id=bid)           # outstanding basis offer
    data.create_offer(db, "rcm", f, **{**FLAT, "bushels": 5000, "price": 4.60}, bid_id=bid)   # outstanding flat offer
    h = data.hedge_summary(db, "rcm", 2)
    t = h["now_totals"][0]
    assert (t["grain"], t["futures_symbol"], t["bushels"], t["contracts"], t["whole_contracts"], t["remainder_bushels"]) == \
        ("Corn", "ZCZ26", 17500, 3.5, 3, 2500)
    ladder = {(b["kind"], b["level"]): (b["offers"], b["bushels"], b["contracts"]) for b in h["now"]}
    assert ladder == {("Flat price", 4.68): (2, 12500, 2.5), ("Flat price", 4.78): (1, 5000, 1.0)}   # implied futures levels
    pot = {(b["kind"], b["level"]): b["bushels"] for b in h["potential"]}
    assert pot == {("Basis (HTA)", -30): 10000, ("Flat price", 4.98): 5000}
    assert h["potential_totals"][0]["contracts"] == 3.0


def test_hedge_levels_move_when_the_basis_moves_for_outstanding_offers_only(db):
    _bid(db, basis=-38)
    oid = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=_live_bid(db))
    assert data.hedge_summary(db, "rcm", 2)["potential"][0]["level"] == 4.68
    _bid(db, basis=-30)
    assert data.hedge_summary(db, "rcm", 2)["potential"][0]["level"] == 4.60
    data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")
    _bid(db, basis=-10)
    assert data.hedge_summary(db, "rcm", 2)["now"][0]["level"] == 4.60                  # locked once hit


def test_hedge_page_is_admin_only(db):
    db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 's@rcm.com', 'elevator_staff')")
    for uid, tenant in ((20, "rcm"), (1, "rcm"), (4, "rcm")):
        with pytest.raises(PortalError):
            data.hedge_summary(db, tenant, uid)
        with pytest.raises(PortalError):
            data.mark_hedged(db, tenant, uid, [1])
    data.hedge_summary(db, "rcm", 2)
    data.hedge_summary(db, "rcm", 5)                                                    # platform admin


def test_marking_offers_hedged_removes_them_from_the_to_hedge_list(db):
    _bid(db, basis=-38)
    bid = _live_bid(db)
    a = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=bid)
    b = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=bid)
    out = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=bid)
    for oid in (a, b):
        data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")
    assert data.mark_hedged(db, "rcm", 2, [a, out, 9999]) == 1                          # outstanding / unknown ids ignored
    assert data.mark_hedged(db, "rcm", 2, [a]) == 0                                     # already hedged
    h = data.hedge_summary(db, "rcm", 2)
    assert [o["offer_id"] for o in h["now_offers"]] == [b] and h["now_totals"][0]["bushels"] == 5000
    assert _offers(db)[a]["hedged_by"] == 2
    assert "hedged" in {r["event_type"] for r in db.query("SELECT event_type FROM offer_events WHERE offer_id = %s", (a,))}


def test_outstanding_offers_of_every_type_follow_a_replaced_bid(db):
    _bid(db, basis=-38)
    first = _live_bid(db)
    flat = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=first)
    hta = data.create_offer(db, "rcm", user(db, 1), **HTA, bid_id=first)
    hit = data.create_offer(db, "rcm", user(db, 1), **HTA, bid_id=first)
    data.act_on_offer(db, "rcm", user(db, 2), hit, "hit")
    _bid(db, basis=-30)
    new = _live_bid(db)
    o = _offers(db)
    assert new != first and o[flat]["bid_id"] == new and o[hta]["bid_id"] == new   # still priced off a live bid
    assert o[hit]["bid_id"] == first                                                 # a hit offer keeps the bid it was hit on


# ── ids are assigned by the app, never read back with MAX(id) (Snowflake IDENTITY is not monotonic) ──
def test_new_ids_are_unique_and_follow_creation_order():
    ids = [data.new_id() for _ in range(2000)]
    assert len(set(ids)) == len(ids)
    import time
    first = data.new_id()
    time.sleep(0.01)
    assert data.new_id() > first                                   # later ms -> larger id, so ORDER BY id is chronological


def test_every_returned_id_is_the_row_that_was_just_written(db):
    ids = [data.create_offer(db, "rcm", user(db, 1), **{**FLAT, "bushels": 1000 + i}) for i in range(25)]
    assert len(set(ids)) == 25
    got = {o["offer_id"]: o["bushels"] for o in data.list_offers(db, "rcm", user(db, 2))}
    assert [got[oid] for oid in ids] == [1000 + i for i in range(25)]      # each id points at its own offer
    ev = {r["offer_id"] for r in db.query("SELECT offer_id FROM offer_events WHERE event_type = 'submitted'")}
    assert set(ids) <= ev
