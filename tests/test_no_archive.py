from datetime import datetime, timezone

from portal import data, futures
from conftest import user

FLAT = dict(grain="Corn", contract_type="CASH", price_type="FIXED", bushels=5000, price=4.30, futures_symbol="ZCZ26")


def _q(price):
    return {"price": price, "as_of": datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)}


def _bid(db):
    db.execute("INSERT INTO locations (location_id, tenant_id, name) VALUES (1, 'rcm', 'Riverton')")
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", location_id=1,
                     basis_cents=-38, cash_price=4.18)
    return data.list_bids(db, "rcm")[0]


def _counts(db):
    return {t: db.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
            for t in ("futures_quotes", "bids", "offer_events", "bid_suggestions", "audit_log", "usage_events")}


def test_price_moves_that_hit_nothing_leave_no_trace(db):
    bid = _bid(db)
    data.create_offer(db, "rcm", user(db, 1), **{**FLAT, "price": 9.99}, bid_id=bid["bid_id"])    # never reached
    futures.update_quotes(db, {"ZCZ26": _q(4.50)})                        # the first quote creates the single row
    before = _counts(db)
    for i in range(200):                                                  # a day of delayed quotes
        futures.update_quotes(db, {"ZCZ26": _q(4.50 + (i % 20) * 0.01)})
        futures.run_update(db, fetch=lambda syms, i=i: {"ZCZ26": _q(4.50 + (i % 20) * 0.01)})
        data.list_bids(db, "rcm")                                         # the live flat bid is computed on read, not stored
    assert _counts(db) == before                                          # nothing grew: one latest row per futures month
    assert db.query("SELECT COUNT(*) AS n FROM futures_quotes")[0]["n"] == 1


def test_a_hit_keeps_the_price_that_triggered_it_even_when_staff_mark_it_by_hand(db):
    bid = _bid(db)
    futures.update_quotes(db, {"ZCZ26": _q(4.70)})                       # live flat bid = 4.32
    oid = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=bid["bid_id"])
    data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")                # manual hit
    ev = db.query("SELECT detail FROM offer_events WHERE offer_id = %s AND event_type = 'hit'", (oid,))[0]["detail"]
    terms = db.query("SELECT terms FROM confirmations WHERE offer_id = %s", (oid,))[0]["terms"]
    for blob in (ev, terms):
        assert '"hit_context"' in blob and '"flat_price": 4.32' in blob and '"futures_quote": 4.7' in blob
        assert '"flat_source": "futures"' in blob and '"basis_cents": -38' in blob


def test_prune_drops_replaced_bids_and_old_suggestions_but_keeps_what_an_offer_points_at(db):
    bid = _bid(db)
    oid = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=bid["bid_id"])
    data.act_on_offer(db, "rcm", user(db, 2), oid, "hit")                # hit offers keep the bid they were hit on
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", location_id=1,
                     basis_cents=-30, cash_price=4.26)                    # replaces it (retired, but referenced)
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", location_id=1,
                     basis_cents=-25, cash_price=4.31)                    # replaces that one (retired, unreferenced)
    data.stage_suggestion(db, "rcm", grain="Corn", delivery_month="2999-12", source="x", location_id=1, basis_cents=-1)
    db.execute("UPDATE bid_suggestions SET status = 'superseded'")
    db.execute("UPDATE bids SET published_at = '2000-01-01 00:00:00' WHERE NOT active")
    db.execute("UPDATE bid_suggestions SET scraped_at = '2000-01-01 00:00:00'")
    out = data.prune_price_history(db, "rcm")
    assert out == {"retired_bids_deleted": 1, "suggestions_deleted": 1}
    ids = {r["bid_id"] for r in db.query("SELECT bid_id FROM bids")}
    assert bid["bid_id"] in ids                                           # the bid the hit offer points at survives
    assert len(ids) == 2                                                  # that one + the live bid
    assert data.list_bids(db, "rcm")[0]["basis_cents"] == -25
    assert data.prune_price_history(db, "dorans") == {"retired_bids_deleted": 0, "suggestions_deleted": 0}
    assert data.prune_price_history(db, "rcm") == {"retired_bids_deleted": 0, "suggestions_deleted": 0}   # idempotent


def test_recent_rows_and_open_suggestions_are_never_pruned(db):
    _bid(db)
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", location_id=1,
                     basis_cents=-30, cash_price=4.26)
    data.stage_suggestion(db, "rcm", grain="Corn", delivery_month="2999-12", source="x", location_id=1, basis_cents=-1)
    db.execute("UPDATE bid_suggestions SET scraped_at = '2000-01-01 00:00:00'")      # old, but still waiting for review
    assert data.prune_price_history(db, "rcm") == {"retired_bids_deleted": 0, "suggestions_deleted": 0}
    assert len(data.list_suggestions(db, "rcm")) == 1
