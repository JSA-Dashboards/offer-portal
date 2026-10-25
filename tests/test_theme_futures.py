from datetime import datetime, timezone

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
        for view in list(at.radio[0].options)[:4]:
            at.radio[0].set_value(view).run()
            assert not at.exception, (view, at.exception)
            assert DISCLAIMER_START in "".join(m.value for m in at.markdown), view


# ── Massive client (placeholder, fabricated payload) ─────────────────────────
def test_symbol_to_massive_ticker():
    assert massive.to_ticker("ZCZ26") == "ZCZ6" and massive.to_ticker("zsx27") == "ZSX7" and massive.to_ticker("KEH27") == "KEH7"
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


# ── quotes are stored, bids are sticky ───────────────────────────────────────
def _bid(db, **kw):
    kw = {**dict(grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", location_id=1, basis_cents=-38,
                 cash_price=4.18), **kw}
    data.publish_bid(db, "rcm", 2, **kw)
    return data.list_bids(db, "rcm")[0]


def _q(price):
    return {"price": price, "as_of": datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)}


def _riverton(db):
    db.execute("INSERT INTO locations (location_id, tenant_id, name) VALUES (1, 'rcm', 'Riverton')")


def test_quotes_are_stored_one_row_per_symbol(db):
    assert futures.update_quotes(db, {"ZCZ26": _q(4.56), "ZSX26": _q(12.5)}) == 2
    futures.update_quotes(db, {"ZCZ26": _q(4.60)})
    got = futures.list_quotes(db)
    assert got["ZCZ26"]["price"] == 4.60 and got["ZSX26"]["price"] == 12.5
    assert db.query("SELECT COUNT(*) AS n FROM futures_quotes")[0]["n"] == 2
    assert set(futures.list_quotes(db, {"ZSX26"})) == {"ZSX26"}
    assert "2026-10-25 15:00" in futures.latest_stamp(got)


def test_a_futures_move_never_changes_a_posted_bid_an_offer_or_the_hedge(db):
    _riverton(db)
    bid = _bid(db)
    data.create_offer(db, "rcm", user(db, 1), **FLAT, bid_id=bid["bid_id"])

    def snapshot():
        b = data.list_bids(db, "rcm")
        return ([(x["bid_id"], x["cash_price"], x["basis_cents"], x["active"]) for x in b], data.hedge_summary(db, "rcm", 2),
                [(o["implied_futures"], o["status"]) for o in data.list_offers(db, "rcm", user(db, 2))])
    before = snapshot()
    futures.update_quotes(db, {"ZCZ26": _q(5.50)})                      # a big rally
    futures.suggest_cash_updates(db, "rcm")
    assert snapshot() == before                                          # bid, offer, implied futures, hedge: all unchanged


def test_indicated_cash_is_staged_for_confirmation_and_only_then_published(db):
    _riverton(db)
    bid = _bid(db)
    futures.update_quotes(db, {"ZCZ26": _q(4.60)})                      # 4.60 + (-0.38) = 4.22 vs posted 4.18
    assert futures.indicated_cash(bid, futures.list_quotes(db)["ZCZ26"]) == 4.22
    assert futures.suggest_cash_updates(db, "rcm")["staged"] == 1
    sug = data.list_suggestions(db, "rcm")
    assert (sug[0]["cash_price"], sug[0]["basis_cents"], sug[0]["source"]) == (4.22, -38, "massive:delayed")
    assert data.list_bids(db, "rcm")[0]["cash_price"] == 4.18            # still sticky
    assert futures.suggest_cash_updates(db, "rcm")["staged"] == 0         # nothing new to suggest
    futures.update_quotes(db, {"ZCZ26": _q(4.70)})
    assert futures.suggest_cash_updates(db, "rcm")["staged"] == 1         # a newer quote supersedes the open suggestion
    assert [s["cash_price"] for s in data.list_suggestions(db, "rcm")] == [4.32]
    data.review_suggestion(db, "rcm", 2, data.list_suggestions(db, "rcm")[0]["suggestion_id"], True, cash_price=4.30)
    live = data.list_bids(db, "rcm")
    assert len(live) == 1 and live[0]["cash_price"] == 4.30               # the admin's edit, only after confirming


def test_small_moves_and_scraped_tenants_get_no_suggestion(db):
    _riverton(db)
    _bid(db)
    futures.update_quotes(db, {"ZCZ26": _q(4.5610)})                      # indicated 4.181 vs posted 4.18: under a quarter cent
    assert futures.suggest_cash_updates(db, "rcm")["staged"] == 0
    futures.update_quotes(db, {"ZCZ26": _q(5.00)})
    db.execute("INSERT INTO scrape_sources (tenant_id, kind, url) VALUES ('rcm', 'agricharts', 'https://x.example')")
    assert "skipped" in futures.suggest_cash_updates(db, "rcm")           # its own site's bids are the better signal
    assert data.list_suggestions(db, "rcm") == []


def test_suggestions_are_tenant_scoped(db):
    _riverton(db)
    _bid(db)
    futures.update_quotes(db, {"ZCZ26": _q(5.0)})
    futures.suggest_cash_updates(db, "rcm")
    assert data.list_suggestions(db, "dorans") == [] and len(data.list_suggestions(db, "rcm")) == 1


def test_run_update_without_a_key_changes_nothing(db, monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    _riverton(db)
    _bid(db)
    assert futures.run_update(db)["status"].startswith("not configured")
    assert db.query("SELECT COUNT(*) AS n FROM futures_quotes")[0]["n"] == 0 and data.list_suggestions(db, "rcm") == []


def test_run_update_fetches_only_months_in_use_and_reports_gaps(db):
    _riverton(db)
    _bid(db)
    data.create_offer(db, "rcm", user(db, 1), **{**FLAT, "futures_symbol": "ZSX26", "grain": "Soybeans"})
    seen = []
    out = futures.run_update(db, fetch=lambda syms: (seen.append(syms), {"ZCZ26": _q(4.60)})[1])
    assert seen == [["ZCZ26", "ZSX26"]] and out["status"] == "ok" and out["missing"] == ["ZSX26"] and out["quotes"] == 1
    assert out["tenants"]["rcm"]["staged"] == 1


# ── UI: delayed futures shown with the notice, and farmers only when switched on ──
def _app_bid(app_db):
    app_db.execute("INSERT INTO locations (location_id, tenant_id, name) VALUES (2, 'rcm', 'RPA')")
    data.publish_bid(app_db, "rcm", 2, grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ26", location_id=1,
                     basis_cents=-38, cash_price=4.18)


def test_staff_see_delayed_futures_with_the_notice_and_the_footer_repeats_it(app_db):
    _app_bid(app_db)
    futures.update_quotes(app_db, {"ZCZ26": _q(4.60)})
    at = _as(app_db, 2)
    at.radio[0].set_value("Bid board").run()
    assert not at.exception, at.exception
    text = "".join(m.value for m in at.markdown)
    assert "Delayed futures." in text and "Delayed quotes." in text            # banner + footer
    assert "stays as posted until an elevator administrator updates and confirms" in text
    df = at.dataframe[0].value
    assert list(df["Futures, delayed ($/bu)"]) == [4.60] and list(df["Indicated cash ($/bu)"]) == [4.22]
    assert list(df["Cash ($/bu)"]) == [4.18]                                    # the posted bid is unchanged


def test_farmers_see_futures_only_when_the_tenant_allows_it(app_db):
    _app_bid(app_db)
    futures.update_quotes(app_db, {"ZCZ26": _q(4.60)})
    at = _as(app_db, 1)
    assert "Delayed futures." not in "".join(m.value for m in at.markdown)
    assert "Futures, delayed ($/bu)" not in at.dataframe[0].value.columns
    app_db.execute("UPDATE tenants SET show_futures = 1 WHERE tenant_id = 'rcm'")
    at = _as(app_db, 1)
    assert "Delayed futures." in "".join(m.value for m in at.markdown)
    assert "Futures, delayed ($/bu)" in at.dataframe[0].value.columns


def test_review_screen_shows_the_posted_bid_next_to_a_futures_suggestion_and_edits_cash(app_db):
    _app_bid(app_db)
    futures.update_quotes(app_db, {"ZCZ26": _q(4.60)})
    futures.suggest_cash_updates(app_db, "rcm")
    at = _as(app_db, 2)
    at.radio[0].set_value("Review scraped bids").run()
    assert not at.exception, at.exception
    text = "".join(m.value for m in at.markdown)
    assert "From delayed futures 4.6000" in text and "Posted now: \\$4.18" in text        # $ escaped so it never becomes math
    next(n for n in at.number_input if n.label == "Cash ($/bu)").set_value(4.25)
    next(b for b in at.button if b.label == "Approve").click().run()
    assert not at.exception, at.exception
    assert data.list_bids(app_db, "rcm")[0]["cash_price"] == 4.25


def test_platform_admin_can_switch_on_futures_for_farmers(app_db):
    app_db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (5, NULL, 'p@jsa.com', 'platform_admin')")
    at = _as(app_db, 5)
    at.radio[0].set_value("Platform").run()
    assert not at.exception, at.exception
    next(t for t in at.toggle if t.label.startswith("Farmers can see delayed futures")).set_value(True)
    next(b for b in at.button if b.label == "Save settings").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert app_db.query("SELECT show_futures FROM tenants WHERE tenant_id = 'rcm'")[0]["show_futures"] == 1
