from datetime import date

import pytest
from portal import contracts, data
from portal.data import PortalError
from conftest import user


def test_five_markets_with_their_own_contracts():
    assert contracts.GRAINS == ("Corn", "Soybeans", "SRW Wheat", "HRW Wheat", "HRS Wheat")
    assert [contracts.SPECS[g]["root"] for g in contracts.GRAINS] == ["ZC", "ZS", "ZW", "KE", "MWE"]
    assert contracts.SPECS["HRW Wheat"]["exchange"] == "KC" and contracts.SPECS["HRS Wheat"]["exchange"] == "MIAX"
    assert contracts.canon_grain("Wheat") == "SRW Wheat"          # the first build's single wheat is CBOT soft red


@pytest.mark.parametrize("grain,symbol", [("Corn", "ZCZ26"), ("Soybeans", "zsx26"), ("SRW Wheat", "ZWN27"),
                                          ("HRW Wheat", "KEK27"), ("HRS Wheat", "MWEU27"), ("Wheat", "ZWZ26")])
def test_listed_months_pass(grain, symbol):
    assert contracts.check_symbol(grain, symbol) == symbol.upper()


@pytest.mark.parametrize("grain,symbol", [("Corn", None), ("Corn", ""), ("Corn", "ZSX26"),      # missing / wrong market
                                          ("Corn", "ZCQ26"), ("Soybeans", "ZSM26"),              # month not listed
                                          ("HRW Wheat", "ZWZ26"), ("Corn", "ZC26"), ("Corn", "nonsense")])
def test_missing_wrong_or_unlisted_months_are_refused(grain, symbol):
    with pytest.raises(ValueError):
        contracts.check_symbol(grain, symbol)


def test_labels_order_and_options():
    assert contracts.futures_label("ZCZ26") == "Dec 2026" and contracts.futures_label("MWEH27") == "Mar 2027"
    syms = ["ZCH27", "ZCZ26", "ZCU26"]
    assert sorted(syms, key=contracts.sort_key) == ["ZCU26", "ZCZ26", "ZCH27"]
    assert contracts.options("Corn", date(2026, 10, 25), 3) == ["ZCZ26", "ZCH27", "ZCK27"]
    assert contracts.options("Soybeans", date(2026, 10, 25), 2) == ["ZSX26", "ZSF27"]
    assert contracts.options("HRS Wheat", date(2026, 12, 3), 2) == ["MWEZ26", "MWEH27"]


def test_a_bid_without_a_valid_futures_month_is_refused(db):
    for bad in (None, "ZSX26", "ZCQ26"):
        with pytest.raises(PortalError):
            data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", basis_cents=-38, futures_symbol=bad)
    assert data.list_bids(db, "rcm") == []


def test_each_wheat_class_hedges_in_its_own_market(db):
    for grain, sym in (("SRW Wheat", "ZWZ26"), ("HRW Wheat", "KEZ26"), ("HRS Wheat", "MWEZ26")):
        data.publish_bid(db, "rcm", 2, grain=grain, delivery_month="2999-12", basis_cents=-20, futures_symbol=sym)
    assert {b["grain"]: b["futures_symbol"] for b in data.list_bids(db, "rcm")} == {
        "SRW Wheat": "ZWZ26", "HRW Wheat": "KEZ26", "HRS Wheat": "MWEZ26"}
    with pytest.raises(PortalError):
        data.publish_bid(db, "rcm", 2, grain="HRW Wheat", delivery_month="2999-12", basis_cents=-20, futures_symbol="ZWZ26")


def test_offer_needs_a_listed_month_and_hedge_summary_groups_by_futures_month(db):
    farmer = user(db, 1)
    base = dict(grain="Corn", contract_type="HTA", price_type="BASIS", bushels=5000, basis_cents=-30)
    with pytest.raises(PortalError):
        data.create_offer(db, "rcm", farmer, **base, futures_symbol="ZCQ26")
    for sym, bu in (("ZCH27", 5000), ("ZCZ26", 10000), ("ZCZ26", 5000)):
        data.create_offer(db, "rcm", farmer, **{**base, "bushels": bu}, futures_symbol=sym)
    data.create_offer(db, "rcm", farmer, grain="HRW Wheat", contract_type="HTA", price_type="BASIS", bushels=5000,
                      basis_cents=-40, futures_symbol="KEH27")
    h = data.hedge_summary(db, "rcm", 2)
    rows = [(t["grain"], t["futures_symbol"], t["futures_month"], t["exchange"], t["bushels"], t["contracts"])
            for t in h["potential_totals"]]
    assert rows == [("Corn", "ZCZ26", "Dec 2026", "CBOT", 15000, 3.0),            # nearest futures month first
                    ("Corn", "ZCH27", "Mar 2027", "CBOT", 5000, 1.0),
                    ("HRW Wheat", "KEH27", "Mar 2027", "KC", 5000, 1.0)]


def test_hedge_price_levels_carry_the_offers_behind_them(db):
    from portal import farmers
    fid = farmers.create_farmer(db, "rcm", 2, "Pat Farmer", "555", entity_name="Farmer Bros LLC", client_id="10442")
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", basis_cents=-38, futures_symbol="ZCZ26")
    bid = data.list_bids(db, "rcm")[0]["bid_id"]
    mk = lambda basis, bu: data.place_offer_for_farmer(db, "rcm", 2, fid, grain="Corn", contract_type="HTA", price_type="BASIS",
                                                       bushels=bu, basis_cents=basis, futures_symbol="ZCZ26", bid_id=bid)
    a, b, c = mk(-30, 5000), mk(-30, 10000), mk(-35, 5000)
    lines = data.hedge_summary(db, "rcm", 2)["potential"]
    by_level = {l["level"]: l for l in lines}
    assert sorted(o["offer_id"] for o in by_level[-30]["offer_list"]) == sorted([a, b])
    assert [o["offer_id"] for o in by_level[-35]["offer_list"]] == [c]
    assert all(l["offers"] == len(l["offer_list"]) and l["bushels"] == sum(o["bushels"] for o in l["offer_list"]) for l in lines)
    assert by_level[-30]["offer_list"][0]["farmer_entity"] == "Farmer Bros LLC"
