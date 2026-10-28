"""Contract type drives the offer: Cash (futures price + basis), Futures Only (HTA) (futures price), Basis Only (basis)."""
import pytest

from portal import data
from portal.data import PortalError
from conftest import user

BASE = dict(grain="Corn", bushels=5000, futures_symbol="ZCZ26")


def _bid(db, basis=-38):
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", basis_cents=basis, cash_price=4.18)
    return data.list_bids(db, "rcm")[0]["bid_id"]


def _one(db, oid):
    return [o for o in data.list_offers(db, "rcm", user(db, 2)) if o["offer_id"] == oid][0]


def test_the_three_contract_types_and_their_labels():
    assert data.CONTRACT_TYPES == ("CASH", "FUTURES_ONLY", "BASIS")
    assert [data.CONTRACT_LABELS[c] for c in data.CONTRACT_TYPES] == ["Cash", "Futures Only (HTA)", "Basis Only"]


def test_cash_is_a_futures_price_plus_a_basis(db):
    bid = _bid(db)
    oid = data.create_offer(db, "rcm", user(db, 1), **BASE, contract_type="CASH", futures_price=4.60, basis_cents=-30, bid_id=bid)
    o = _one(db, oid)
    assert (o["contract_type"], o["price_type"], o["price"], o["basis_cents"]) == ("CASH", "FIXED", 4.30, -30)
    assert (o["implied_futures"], o["basis_ref_cents"]) == (4.6, -30)                       # the farmer's own split
    _bid(db, basis=-20)                                                                        # the posted basis moves...
    assert (_one(db, oid)["implied_futures"], _one(db, oid)["price"]) == (4.6, 4.3)            # ...their terms do not
    flat = data.create_offer(db, "rcm", user(db, 1), **BASE, contract_type="CASH", price=4.30, bid_id=data.list_bids(db, "rcm")[0]["bid_id"])
    assert _one(db, flat)["implied_futures"] == 4.5                                            # a flat price alone still follows the bid (4.30 + .20)
    _bid(db, basis=-10)
    assert _one(db, flat)["implied_futures"] == 4.4


def test_futures_only_keeps_just_the_futures_price_and_basis_only_just_the_basis(db):
    _bid(db)
    f = data.create_offer(db, "rcm", user(db, 1), **BASE, contract_type="FUTURES_ONLY", futures_price=4.55, basis_cents=-99)
    b = data.create_offer(db, "rcm", user(db, 1), **BASE, contract_type="BASIS", basis_cents=-30, futures_price=9.99)
    of, ob = _one(db, f), _one(db, b)
    assert (of["price"], of["basis_cents"], of["price_type"], of["implied_futures"]) == (4.55, None, "FIXED", 4.55)
    assert (ob["price"], ob["basis_cents"], ob["price_type"], ob["implied_futures"]) == (None, -30, "BASIS", None)


@pytest.mark.parametrize("terms", [dict(contract_type="CASH"), dict(contract_type="CASH", futures_price=4.6),
                                   dict(contract_type="CASH", basis_cents=-30), dict(contract_type="FUTURES_ONLY"),
                                   dict(contract_type="FUTURES_ONLY", basis_cents=-30), dict(contract_type="BASIS"),
                                   dict(contract_type="BASIS", futures_price=4.6), dict(contract_type="SWAP", basis_cents=-30)])
def test_each_type_needs_its_own_prices(db, terms):
    with pytest.raises(PortalError):
        data.create_offer(db, "rcm", user(db, 1), **BASE, **terms)


def test_the_old_hta_name_means_basis_only_in_new_and_stored_offers(db):
    oid = data.create_offer(db, "rcm", user(db, 1), **BASE, contract_type="HTA", price_type="BASIS", basis_cents=-30)
    assert _one(db, oid)["contract_type"] == "BASIS"                                           # written as Basis Only
    db.execute("UPDATE offers SET contract_type = 'HTA' WHERE offer_id = %s", (oid,))          # a row from before the rename
    assert _one(db, oid)["contract_type"] == "BASIS"                                           # still reads as Basis Only


def test_an_offer_names_a_futures_month_not_a_delivery_window(db):
    far = dict(grain="Corn", bushels=5000, futures_symbol="ZCZ99")
    oid = data.create_offer(db, "rcm", user(db, 1), **far, contract_type="FUTURES_ONLY", futures_price=4.55)
    o = _one(db, oid)
    assert (str(o["delivery_start"]), str(o["delivery_end"])) == ("2099-12-01", "2099-12-31")   # the futures month
    bid = _bid(db)
    linked = data.create_offer(db, "rcm", user(db, 1), **BASE, contract_type="BASIS", basis_cents=-30, bid_id=bid)
    lo = _one(db, linked)
    assert lo["delivery_start"] is None and str(data.offer_delivery_end(lo)) == "2999-12-31"   # against a bid: the bid's window
    with pytest.raises(PortalError):                                                            # a futures month already over
        data.create_offer(db, "rcm", user(db, 1), grain="Corn", bushels=5000, futures_symbol="ZCH20",
                          contract_type="BASIS", basis_cents=-30)


def test_the_hedge_page_names_the_contract_types(db):
    _bid(db)
    for t in (dict(contract_type="CASH", futures_price=4.6, basis_cents=-30), dict(contract_type="FUTURES_ONLY", futures_price=4.5),
              dict(contract_type="BASIS", basis_cents=-30)):
        data.create_offer(db, "rcm", user(db, 1), **BASE, **t)
    kinds = sorted(b["kind"] for b in data.hedge_summary(db, "rcm", 2)["potential"])
    assert kinds == ["Basis only", "Flat price", "Futures only (HTA)"]
