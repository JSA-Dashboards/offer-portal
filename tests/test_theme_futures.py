from conftest import goto, pages  # noqa: F401
from datetime import datetime, timedelta, timezone

from portal import data, futures, massive, theme
from conftest import user
from test_app import _as, app_db, run  # noqa: F401  (fixture + helpers)

FLAT = dict(grain="Corn", contract_type="CASH", price_type="FIXED", bushels=5000, price=4.30, futures_symbol="ZCZ26")
DISCLAIMER_START = "Trading commodity futures, options on futures, cash commodities"


# ── theme: header slots, footer disclaimer ───────────────────────────────────
def test_footer_carries_the_standard_jsa_disclaimer_and_copyright_year():
    f = theme.footer_html({"name": "RCM Coop", "support_email": "help@rcm.example"})
    assert DISCLAIMER_START in f and "does not accept orders to buy or sell any financial instruments via email" in f
    assert f"&copy; John Stewart &amp; Associates, Inc. {datetime.now().year}" in f
    assert "Delayed quotes" not in f                                   # only on pages that show futures
    assert "help@rcm.example" in f and "logo-full.png" in f           # white-label support line + JSA mark
    assert "Delayed quotes" in theme.footer_html({"name": "RCM Coop"}, delayed=True)


def test_header_has_a_white_label_slot_and_jsa_powered_by():
    h = theme.header_html({"name": "RCM Coop", "logo_url": "https://rcm.example/logo.png"})
    assert 'class="slot-tenant"' in h and 'src="https://rcm.example/logo.png"' in h and "RCM Coop" in h
    assert "Powered by" in h and "jpsi.com" in h                       # JSA mark integrated
    assert "http://insecure" not in theme.header_html({"name": "X", "logo_url": "http://insecure.example/x.png"})
    assert "&lt;script&gt;" in theme.header_html({"name": "<script>"})   # tenant text is escaped
    assert "Powered by" not in theme.header_html(None) and "jpsi.com" in theme.header_html(None)   # landing page = JSA's own


def test_tenant_colors_fall_back_to_jsa_palette():
    assert theme.colors({"primary_color": "#32373c", "accent_color": "#0693e3"})["primary"] == theme.JSA_GREEN
    assert theme.colors({"primary_color": "#112233", "accent_color": "bad"}) == {"primary": "#112233", "accent": theme.JSA_BLUE}
    assert "--tenant-primary:#112233" in theme.css({"primary_color": "#112233"})


def test_jsa_branding_switch_removes_the_logo_but_not_the_disclaimer(monkeypatch):
    monkeypatch.setattr(theme, "JSA_BRANDING", False)
    h, f = theme.header_html({"name": "RCM Coop"}), theme.footer_html({"name": "RCM Coop"})
    assert "Powered by" not in h and "logo-full.png" not in f and DISCLAIMER_START in f


def test_disclaimer_shown_at_the_bottom_of_every_page(app_db):
    assert DISCLAIMER_START in "".join(m.value for m in run().markdown)                 # no tenant link
    assert DISCLAIMER_START in "".join(m.value for m in run(t="nope").markdown)         # unknown tenant
    assert DISCLAIMER_START in "".join(m.value for m in run(t="rcm").markdown)          # sign-in page
    for uid in (1, 2):                                                                    # farmer and elevator admin
        at = _as(app_db, uid)
        assert DISCLAIMER_START in "".join(m.value for m in at.markdown)
        for view in list(pages(at))[:4]:
            goto(at, view)
            assert not at.exception, (view, at.exception)
            assert DISCLAIMER_START in "".join(m.value for m in at.markdown), view


# ── Massive client (placeholder, fabricated payload) ─────────────────────────
def test_symbol_to_massive_ticker():
    assert massive.to_ticker("ZCZ26") == "ZCZ6" and massive.to_ticker("zsx27") == "ZSX7" and massive.to_ticker("KEH27") == "KEH7"
    assert massive.to_ticker("MWEH27") is None and massive.to_ticker("ZWZ26") == "ZWZ6"      # MIAX wheat is not on the feed
    assert massive.to_ticker("ZC:BF H7") is None and massive.to_ticker("") is None and massive.to_ticker("CORN") is None


def test_parse_snapshot_converts_cents_and_falls_back():
    payload = {"results": [
        {"details": {"ticker": "ZCZ6"}, "last_trade": {"price": 456.0, "last_updated": 1_761_400_000_000_000_000}},
        {"details": {"ticker": "ZSX6"}, "last_trade": {}, "session": {"settlement_price": 1256.5}},
        {"details": {"ticker": "ZWZ6"}, "session": {"close": 0}},                         # no usable price
        {"details": {"ticker": "ZMZ6"}, "last_trade": {"price": 300}}]}                   # not requested
    out = massive.parse_snapshot(payload, {"ZCZ6": "ZCZ26", "ZSX6": "ZSX26", "ZWZ6": "ZWZ26"})
    assert out["ZCZ26"]["price"] == 4.56 and out["ZCZ26"]["as_of"].year == 2025
    assert out["ZSX26"]["price"] == 12.565 and out["ZSX26"]["as_of"] is None
    assert set(out) == {"ZCZ26", "ZSX26"}


def test_fetch_quotes_without_a_key_does_nothing(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    assert massive.configured() is False and massive.fetch_quotes(["ZCZ26"]) == {}


def test_fetch_quotes_chunks_and_sends_the_bearer_key():
    calls = []

    class R:
        def __init__(self, tickers):
            self.t = tickers

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"details": {"ticker": t}, "last_trade": {"price": 450.0}} for t in self.t]}

    class S:
        def get(self, url, params, headers, timeout):
            calls.append((params["ticker.any_of"], headers["Authorization"]))
            return R(params["ticker.any_of"].split(","))
    syms = [f"ZC{m}{y}" for y in (26, 27, 28) for m in "HKNUZ"] + [f"ZS{m}{y}" for y in (26, 27, 28) for m in "FHKNQUX"]
    out = massive.fetch_quotes(syms, api_key="k", session=S())
    assert len(out) == len(syms) and len(calls) == 2 and all(c[1] == "Bearer k" for c in calls)   # 36 tickers -> 2 chunks of <=25


# ── quotes are stored; the flat bid is live (futures + sticky basis) ─────────────────────────
def _bid(db, **kw):
    kw = {**dict(grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", location_id=1, basis_cents=-38,
                 cash_price=4.18), **kw}
    data.publish_bid(db, "rcm", 2, **kw)
    return [b for b in data.list_bids(db, "rcm") if b["futures_symbol"] == kw["futures_symbol"]][0]


def _q(price):
    return {"price": price, "as_of": datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)}


def _riverton(db):
    db.execute("INSERT INTO locations (location_id, tenant_id, name) VALUES (1, 'rcm', 'Riverton')")


def _age_quotes(db, minutes):
    db.execute("UPDATE futures_quotes SET fetched_at = %s",
               ((datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"),))


def test_quotes_are_stored_one_row_per_symbol(db):
    assert futures.update_quotes(db, {"ZCZ26": _q(4.56), "ZSX26": _q(12.5)}) == 2
    futures.update_quotes(db, {"ZCZ26": _q(4.60)})
    got = futures.list_quotes(db)
    assert got["ZCZ26"]["price"] == 4.60 and got["ZSX26"]["price"] == 12.5
    assert db.query("SELECT COUNT(*) AS n FROM futures_quotes")[0]["n"] == 2
    assert set(futures.list_quotes(db, {"ZSX26"})) == {"ZSX26"}
    assert "2026-10-25 15:00" in futures.latest_stamp(got)


def test_flat_bid_is_futures_plus_the_sticky_basis_and_moves_with_every_quote(db):
    _riverton(db)
    bid = _bid(db)
    assert (bid["flat_price"], bid["flat_source"]) == (4.18, "posted")             # no futures feed yet: as posted
    futures.update_quotes(db, {"ZCZ26": _q(4.60)})
    b = data.list_bids(db, "rcm")[0]
    assert (b["flat_price"], b["flat_source"], b["quote_price"]) == (4.22, "futures", 4.60)       # 4.60 + (-0.38)
    futures.update_quotes(db, {"ZCZ26": _q(5.50)})
    assert data.list_bids(db, "rcm")[0]["flat_price"] == 5.12                      # follows futures, always
    futures.update_quotes(db, {"ZCZ26": _q(4.10)})
    assert data.list_bids(db, "rcm")[0]["flat_price"] == 3.72                      # ... down as well as up
    stored = db.query("SELECT basis_cents, cash_price FROM bids WHERE bid_id = %s", (bid["bid_id"],))[0]
    assert (stored["basis_cents"], stored["cash_price"]) == (-38, 4.18)           # the stored bid never changed
    assert _bid(db, basis_cents=-30)["flat_price"] == 3.80                         # a confirmed new basis re-bases the flat bid
    assert _bid(db, futures_symbol="ZSX26", grain="Soybeans", basis_cents=None, cash_price=11.8)["flat_source"] == "posted"   # cash-only bid


def test_a_futures_move_changes_no_stored_bid_offer_implied_futures_or_hedge(db):
    _riverton(db)
    bid = _bid(db)
    data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=bid["bid_id"])

    def snapshot():
        raw = db.query("SELECT bid_id, basis_cents, cash_price, active FROM bids ORDER BY bid_id")
        return (raw, data.hedge_summary(db, "rcm", 2),
                [(o["implied_futures"], o["status"]) for o in data.list_offers(db, "rcm", user(db, 2))])
    before = snapshot()
    futures.update_quotes(db, {"ZCZ26": _q(5.50)})                                 # a big rally
    assert snapshot() == before


def test_offers_are_compared_with_the_live_flat_bid(db):
    _riverton(db)
    bid = _bid(db)
    oid = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=bid["bid_id"])    # sells at 4.30
    reached = lambda: [o for o in data.list_offers(db, "rcm", user(db, 2)) if o["offer_id"] == oid][0]["bid_reached"]
    assert reached() is False                                                          # posted cash 4.18 < 4.30
    futures.update_quotes(db, {"ZCZ26": _q(4.56)})                                    # live flat 4.18
    assert reached() is False
    futures.update_quotes(db, {"ZCZ26": _q(4.70)})                                    # live flat 4.32 >= 4.30
    assert reached() is True
    futures.update_quotes(db, {"ZCZ26": _q(4.60)})                                    # falls back to 4.22: no longer reached
    assert reached() is False


def test_a_stale_quote_prices_nothing_and_hits_nothing(db):
    _riverton(db)
    bid = _bid(db)
    data.add_auto_hit_rule(db, "rcm", 2)
    futures.update_quotes(db, {"ZCZ26": _q(4.70)})
    data.act_on_offer(db, "rcm", user(db, 2), data.create_offer(db, "rcm", user(db, 1), **{**FLAT, "price": 9.99},
                                                               bid_id=bid["bid_id"]), "decline")   # unrelated, just seeding
    oid = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=bid["bid_id"])
    assert [o for o in data.list_offers(db, "rcm", user(db, 2)) if o["offer_id"] == oid][0]["status"] == "hit"   # fresh: hit
    oid2 = data.create_offer(db, "rcm", user(db, 1), **{**FLAT, "bushels": 6000}, bid_id=bid["bid_id"])
    _age_quotes(db, data.QUOTE_MAX_AGE_MIN + 5)                                       # the feed has stopped
    b = data.list_bids(db, "rcm")[0]
    assert (b["flat_price"], b["flat_source"]) == (None, "stale")
    assert data.run_auto_hit(db, "rcm") == 0
    assert [o for o in data.list_offers(db, "rcm", user(db, 2)) if o["offer_id"] == oid2][0]["bid_reached"] is False


def test_run_update_hits_offers_the_new_flat_bid_reaches_only_when_a_rule_allows_it(db):
    _riverton(db)
    bid = _bid(db)
    oid = data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=bid["bid_id"])    # sells at 4.30
    status = lambda: [o for o in data.list_offers(db, "rcm", user(db, 2)) if o["offer_id"] == oid][0]["status"]
    out = futures.run_update(db, fetch=lambda syms: {"ZCZ26": _q(4.70)})
    assert out["status"] == "ok" and out["tenants"]["rcm"] == {"auto_hit": 0} and status() == "submitted"   # no rule: flagged only
    data.add_auto_hit_rule(db, "rcm", 2)                                                # adding the rule hits it at once
    assert status() == "hit"
    ev = db.query("SELECT detail FROM offer_events WHERE offer_id = %s AND event_type = 'hit'", (oid,))[0]["detail"]
    assert '"auto_hit": true' in ev and '"bid_flat_price": 4.32' in ev and '"flat_source": "futures"' in ev   # the live flat bid that triggered it
    assert '"futures_quote": 4.7' in ev


def test_run_update_without_a_key_changes_nothing(db, monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    _riverton(db)
    _bid(db)
    assert futures.run_update(db)["status"].startswith("not configured")
    assert db.query("SELECT COUNT(*) AS n FROM futures_quotes")[0]["n"] == 0


def test_run_update_fetches_only_months_in_use_and_reports_gaps(db):
    _riverton(db)
    _bid(db)
    data.create_offer(db, "rcm", user(db, 1), **{**FLAT, "futures_symbol": "ZSX26", "grain": "Soybeans"})
    seen = []
    out = futures.run_update(db, fetch=lambda syms: (seen.append(syms), {"ZCZ26": _q(4.60)})[1])
    assert seen == [["ZCZ26", "ZSX26"]] and out["status"] == "ok" and out["missing"] == ["ZSX26"] and out["quotes"] == 1


# ── UI: the flat bid is shown to everyone with the notice; the futures column only when switched on ──
def _app_bid(app_db):
    app_db.execute("INSERT INTO locations (location_id, tenant_id, name) VALUES (2, 'rcm', 'RPA')")
    data.publish_bid(app_db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", location_id=1,
                     basis_cents=-38, cash_price=4.18)


def test_staff_see_the_live_flat_bid_delayed_futures_and_the_notice(app_db):
    _app_bid(app_db)
    futures.update_quotes(app_db, {"ZCZ26": _q(4.60)})
    at = _as(app_db, 2)
    goto(at, "Bid board")
    assert not at.exception, at.exception
    text = "".join(m.value for m in at.markdown)
    assert "Delayed futures." in text and "Delayed quotes." in text                    # banner + footer
    assert "the basis stays as posted until an elevator administrator updates and confirms" in text
    df = at.dataframe[0].value
    assert list(df["Flat bid ($/bu)"]) == [4.22] and list(df["Basis (¢)"]) == [-38]
    assert list(df["Futures, delayed ($/bu)"]) == [4.60] and list(df["Flat bid from"]) == ["futures + basis (delayed)"]


def test_farmers_see_the_flat_bid_but_futures_only_when_the_tenant_allows_it(app_db):
    _app_bid(app_db)
    futures.update_quotes(app_db, {"ZCZ26": _q(4.60)})
    at = _as(app_db, 1)
    assert "Delayed futures." in "".join(m.value for m in at.markdown)                 # a flat bid from futures carries the notice
    df = at.dataframe[0].value
    assert list(df["Flat bid ($/bu)"]) == [4.22] and "Futures, delayed ($/bu)" not in df.columns
    app_db.execute("UPDATE tenants SET show_futures = 1 WHERE tenant_id = 'rcm'")
    assert "Futures, delayed ($/bu)" in _as(app_db, 1).dataframe[0].value.columns


def test_a_bid_with_no_futures_feed_shows_as_posted_and_no_banner(app_db):
    _app_bid(app_db)
    at = _as(app_db, 1)
    assert "Delayed futures." not in "".join(m.value for m in at.markdown)
    df = at.dataframe[0].value
    assert list(df["Flat bid ($/bu)"]) == [4.18] and list(df["Flat bid from"]) == ["as posted"]


def test_review_screen_edits_the_basis_and_posts_it_with_the_scraped_delivery_range(app_db):
    _app_bid(app_db)
    data.stage_suggestion(app_db, "rcm", grain="Corn", delivery_month="2999-12", source="agricharts", location_id=1,
                          futures_symbol="ZCZ26", basis_cents=-35, cash_price=4.21,
                          delivery_start="2999-12-01", delivery_end="2999-12-15")           # the source gave a clear range
    at = _as(app_db, 2)
    goto(at, "Review scraped bids")
    assert not at.exception, at.exception
    text = "".join(m.value for m in at.markdown)
    assert "Posted now: basis -38¢" in text and "Delivery Dec 01 – Dec 15, 2999" in text
    assert not any(n.label == "Cash ($/bu)" for n in at.number_input)
    next(n for n in at.number_input if n.label == "Basis (¢)").set_value(-33)
    next(b for b in at.button if b.label == "Approve").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    b = data.list_bids(app_db, "rcm")[0]
    assert (b["basis_cents"], str(b["window_start"]), str(b["window_end"])) == (-33, "2999-12-01", "2999-12-15")


def test_review_screen_refuses_to_approve_a_suggestion_with_no_clear_delivery_range(app_db):
    _app_bid(app_db)
    data.stage_suggestion(app_db, "rcm", grain="Corn", delivery_month="2999-12", source="bushelsites", location_id=1,
                          futures_symbol="ZCZ26", basis_cents=-35, cash_price=4.21)          # e.g. a "Harvest 2026" label
    at = _as(app_db, 2)
    goto(at, "Review scraped bids")
    assert "gave no clear delivery range" in "".join(m.value for m in at.markdown)
    next(b for b in at.button if b.label == "Approve").click().run()
    assert not at.exception, at.exception
    assert at.error and "delivery date range" in at.error[0].value                           # staff must enter one
    assert data.list_bids(app_db, "rcm")[0]["basis_cents"] == -38 and len(data.list_suggestions(app_db, "rcm")) == 1


def test_platform_admin_can_switch_on_futures_for_farmers(app_db):
    app_db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (5, NULL, 'p@jsa.com', 'platform_admin')")
    at = _as(app_db, 5)
    goto(at, "Platform")
    assert not at.exception, at.exception
    next(t for t in at.toggle if t.label.startswith("Farmers can see delayed futures")).set_value(True)
    next(b for b in at.button if b.label == "Save settings").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert app_db.query("SELECT show_futures FROM tenants WHERE tenant_id = 'rcm'")[0]["show_futures"] == 1
