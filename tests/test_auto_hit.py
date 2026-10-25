import pytest
from portal import data, farmers
from portal.data import PortalError
from conftest import user

FLAT = dict(grain="Corn", contract_type="CASH", price_type="FIXED", bushels=5000, price=4.30, futures_symbol="ZCZ26")
HTA = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=5000, basis_cents=-30, futures_symbol="ZCZ26")


def _bid(db, basis=-38, cash=4.18, tenant="rcm", actor=2, **kw):
    kw = {**dict(grain="Corn", delivery_month="2026-12", futures_symbol="ZCZ26"), **kw}
    return data.publish_bid(db, tenant, actor, basis_cents=basis, cash_price=cash, **kw)


def _bid_id(db, tenant="rcm"):
    return data.list_bids(db, tenant)[0]["bid_id"]


def _offer(db, uid=1, tenant="rcm", **kw):
    return data.create_offer(db, tenant, user(db, uid), **{**FLAT, **kw})


def _status(db, oid, tenant="rcm", uid=2):
    return [o for o in data.list_offers(db, tenant, user(db, uid)) if o["offer_id"] == oid][0]


def _rule(db, tenant="rcm", admin=2, **kw):
    return data.add_auto_hit_rule(db, tenant, admin, **kw)


def test_no_rules_means_nothing_is_ever_hit_automatically(db):
    _bid(db, cash=4.50)
    oid = _offer(db, bid_id=_bid_id(db))                         # offer 4.30 < bid 4.50: reached
    assert _status(db, oid)["bid_reached"] is True and _status(db, oid)["status"] == "submitted"
    assert data.run_auto_hit(db, "rcm") == 0


def test_posting_a_bid_that_reaches_offers_hits_them_automatically(db):
    _bid(db, basis=-38, cash=4.18)
    bid = _bid_id(db)
    flat_hi = _offer(db, price=4.40, bid_id=bid)                 # bid 4.18 < 4.40: not reached
    flat_lo = _offer(db, price=4.30, bid_id=bid)
    hta_ok = _offer(db, bid_id=bid, contract_type="HTA", price_type="BASIS", price=None, basis_cents=-35)
    hta_no = _offer(db, bid_id=bid, contract_type="HTA", price_type="BASIS", price=None, basis_cents=-20)
    assert _rule(db) == 0                                        # no offer is reached yet
    r = _bid(db, basis=-30, cash=4.35)                           # bid improves to 4.35 / -30
    assert (int(r), r.auto_hit) == (2, 2)                        # 2 flat offers re-priced, 2 offers hit
    got = {k: _status(db, v) for k, v in dict(flat_hi=flat_hi, flat_lo=flat_lo, hta_ok=hta_ok, hta_no=hta_no).items()}
    assert {k: o["status"] for k, o in got.items()} == dict(flat_hi="submitted", flat_lo="hit", hta_ok="hit",
                                                            hta_no="submitted")
    lo = got["flat_lo"]
    assert (lo["auto_hit"], lo["hit_by"], lo["auto_hit_rule_id"]) == (1, None, 1) and lo["hit_at"]
    assert lo["implied_futures"] == 4.60 and lo["basis_ref_cents"] == -30            # locked at the NEW basis


def test_auto_hit_writes_the_same_records_as_a_manual_hit_and_says_it_was_automatic(db):
    _bid(db, cash=4.50)
    _rule(db)
    oid = _offer(db, bid_id=_bid_id(db))                         # reached on entry, so hit at once
    assert _status(db, oid)["status"] == "hit"
    ev = db.query("SELECT actor_user_id, detail FROM offer_events WHERE offer_id = %s AND event_type = 'hit'", (oid,))
    assert len(ev) == 1 and ev[0]["actor_user_id"] is None
    assert '"auto_hit": true' in ev[0]["detail"] and '"rule_id": 1' in ev[0]["detail"] and '"bid_cash_price": 4.5' in ev[0]["detail"]
    c = db.query("SELECT hit_by, auto_hit, terms FROM confirmations WHERE offer_id = %s", (oid,))
    assert len(c) == 1 and (c[0]["hit_by"], c[0]["auto_hit"]) == (None, 1) and '"price": 4.3' in c[0]["terms"]
    assert db.query("SELECT COUNT(*) AS n FROM usage_events WHERE event_type = 'offer_hit' AND ref_id = %s", (oid,))[0]["n"] == 1
    assert data.run_auto_hit(db, "rcm") == 0                     # idempotent: nothing left to hit
    assert db.query("SELECT COUNT(*) AS n FROM confirmations WHERE offer_id = %s", (oid,))[0]["n"] == 1


def test_a_manual_hit_is_marked_as_staff_and_still_works(db):
    oid = _offer(db)
    data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")
    o = _status(db, oid)
    assert (o["status"], o["auto_hit"], o["hit_by"], o["auto_hit_rule_id"]) == ("hit", 0, 2, None) and o["hit_at"]
    c = db.query("SELECT hit_by, auto_hit FROM confirmations WHERE offer_id = %s", (oid,))[0]
    assert (c["hit_by"], c["auto_hit"]) == (2, 0)


def test_staff_placed_offers_hit_on_entry_too(db):
    _bid(db, cash=4.50)
    _rule(db)
    fid = farmers.create_farmer(db, "rcm", 2, "Phone Caller")
    oid = data.place_offer_for_farmer(db, "rcm", 2, fid, **FLAT, bid_id=_bid_id(db))
    o = _status(db, oid)
    assert (o["status"], o["auto_hit"], o["placed_by"]) == ("hit", 1, 2)


def test_rule_scope_commodity_contract_type_location_and_size(db):
    db.execute("INSERT INTO locations (location_id, tenant_id, name) VALUES (1, 'rcm', 'Riverton')")
    db.execute("INSERT INTO locations (location_id, tenant_id, name) VALUES (2, 'rcm', 'RPA')")
    _bid(db, cash=4.50, location_id=1)
    _bid(db, cash=4.50, location_id=2)
    _bid(db, cash=11.00, basis=-40, grain="Soybeans", futures_symbol="ZSX26", location_id=1)
    bids = {(b["grain"], b["location_id"]): b["bid_id"] for b in data.list_bids(db, "rcm")}
    _rule(db, location_id=1, grain="Corn", contract_type="CASH", max_bushels=10000)
    mk = lambda bid_loc, **kw: _offer(db, bid_id=bids[("Corn", bid_loc)], location_id=bid_loc, **kw) \
        if "grain" not in kw else _offer(db, bid_id=bids[("Soybeans", 1)], location_id=1, **{**kw, "futures_symbol": "ZSX26", "price": 10.0})
    hit = mk(1)
    too_big = mk(1, bushels=15000)
    wrong_loc = mk(2)
    wrong_grain = mk(1, grain="Soybeans")
    hta = _offer(db, bid_id=bids[("Corn", 1)], location_id=1, contract_type="HTA", price_type="BASIS",
                 price=None, basis_cents=-45)
    states = {k: _status(db, v)["status"] for k, v in dict(hit=hit, too_big=too_big, wrong_loc=wrong_loc,
                                                           wrong_grain=wrong_grain, hta=hta).items()}
    assert states == dict(hit="hit", too_big="submitted", wrong_loc="submitted", wrong_grain="submitted",
                          hta="submitted")
    assert _status(db, too_big)["bid_reached"] is True           # reached, but left for staff: flagged only


def test_open_priced_offer_uses_the_reference_bids_location_for_rule_scope(db):
    db.execute("INSERT INTO locations (location_id, tenant_id, name) VALUES (1, 'rcm', 'Riverton')")
    _bid(db, cash=4.50, location_id=1)
    _rule(db, location_id=1)
    oid = _offer(db, delivery_start="2026-12-05")                # no bid_id / location on the offer
    assert _status(db, oid)["status"] == "hit"


def test_adding_or_switching_on_a_rule_applies_to_offers_already_reached(db):
    _bid(db, cash=4.50)
    a = _offer(db, bid_id=_bid_id(db))
    b = _offer(db, bid_id=_bid_id(db))
    assert {_status(db, x)["status"] for x in (a, b)} == {"submitted"}
    assert _rule(db) == 2
    c = _offer(db)                                               # no bid link, not matched: stays outstanding
    assert _status(db, c)["status"] == "submitted"
    rid = data.list_auto_hit_rules(db, "rcm", 2)[0]["rule_id"]
    assert data.set_auto_hit_rule_active(db, "rcm", 2, rid, False) == 0
    d = _offer(db, bid_id=_bid_id(db))                           # reached, but the only rule is off
    assert _status(db, d)["status"] == "submitted"
    assert data.set_auto_hit_rule_active(db, "rcm", 2, rid, True) == 1
    assert _status(db, d)["status"] == "hit"


def test_inactive_rule_and_check_now(db):
    _bid(db, cash=4.50)
    _rule(db)
    rid = data.list_auto_hit_rules(db, "rcm", 2)[0]["rule_id"]
    data.set_auto_hit_rule_active(db, "rcm", 2, rid, False)
    oid = _offer(db, bid_id=_bid_id(db))
    assert data.check_auto_hit_now(db, "rcm", 2) == 0 and _status(db, oid)["status"] == "submitted"
    data.set_auto_hit_rule_active(db, "rcm", 2, rid, True)
    assert _status(db, oid)["status"] == "hit"


def test_expired_offers_are_never_hit(db):
    _bid(db, cash=4.50)
    oid = _offer(db, bid_id=_bid_id(db), expires_at="2000-01-01 00:00:00")
    assert _rule(db) == 0
    assert _status(db, oid)["status"] == "expired"


def test_only_outstanding_offers_that_the_bid_reached_are_hit(db):
    _bid(db, cash=4.50)
    bid = _bid_id(db)
    withdrawn = _offer(db, bid_id=bid)
    data.act_on_offer(db, "rcm", user(db, 1), withdrawn, "withdraw")
    declined = _offer(db, bid_id=bid)
    data.act_on_offer(db, "rcm", user(db, 2), declined, "decline")
    futures_only = _offer(db, contract_type="FUTURES_ONLY", price=4.55)
    assert _rule(db) == 0
    assert [_status(db, x)["status"] for x in (withdrawn, declined, futures_only)] == ["withdrawn", "declined", "submitted"]


def test_tenants_are_isolated(db):
    _bid(db, cash=4.50)
    mine = _offer(db, bid_id=_bid_id(db))
    data.publish_bid(db, "dorans", 4, grain="Corn", delivery_month="2026-12", futures_symbol="ZCZ26",
                     basis_cents=-20, cash_price=4.60)
    theirs = _offer(db, uid=3, tenant="dorans", bid_id=_bid_id(db, "dorans"))
    assert _rule(db, tenant="dorans", admin=4) == 1              # dorans' rule hits dorans' offer only
    assert _status(db, theirs, "dorans", 4)["status"] == "hit"
    assert _status(db, mine)["status"] == "submitted"
    assert data.list_auto_hit_rules(db, "rcm", 2) == []          # and its rule isn't rcm's
    with pytest.raises(PortalError):
        data.set_auto_hit_rule_active(db, "rcm", 2, data.list_auto_hit_rules(db, "dorans", 4)[0]["rule_id"], False)


def test_rules_are_admin_only(db):
    db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 's@rcm.com', 'elevator_staff')")
    for uid, tenant in ((20, "rcm"), (1, "rcm"), (4, "rcm")):    # staff, farmer, another tenant's admin
        with pytest.raises(PortalError):
            data.add_auto_hit_rule(db, tenant, uid)
        with pytest.raises(PortalError):
            data.list_auto_hit_rules(db, tenant, uid)
        with pytest.raises(PortalError):
            data.check_auto_hit_now(db, tenant, uid)
        with pytest.raises(PortalError):
            data.list_auto_hits(db, tenant, uid)
    data.add_auto_hit_rule(db, "rcm", 5)                         # platform admin may


def test_rule_validation(db):
    db.execute("INSERT INTO locations (location_id, tenant_id, name) VALUES (9, 'dorans', 'Elsewhere')")
    for kw in (dict(grain="Barley"), dict(contract_type="FUTURES_ONLY"), dict(contract_type="SWAP"),
               dict(location_id=9), dict(location_id=123), dict(max_bushels=0), dict(max_bushels=-5)):
        with pytest.raises(PortalError):
            data.add_auto_hit_rule(db, "rcm", 2, **kw)
    assert data.list_auto_hit_rules(db, "rcm", 2) == []


def test_recent_auto_hits_list(db):
    _bid(db, cash=4.50)
    _rule(db)
    manual = _offer(db, price=5.00)                              # not reached, hit by hand
    data.act_on_offer(db, "rcm", user(db, 2), manual, "hit")
    auto = [_offer(db, bid_id=_bid_id(db)) for _ in range(3)]
    got = data.list_auto_hits(db, "rcm", 2, limit=2)
    assert len(got) == 2 and all(o["auto_hit"] and o["offer_id"] in auto for o in got)
    assert manual not in {o["offer_id"] for o in data.list_auto_hits(db, "rcm", 2)}
    assert data.list_auto_hits(db, "dorans", 4) == []
