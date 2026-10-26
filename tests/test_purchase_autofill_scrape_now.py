from conftest import goto, pages  # noqa: F401
import scrape_run
from portal import data, purchases, scrapers
from test_app import _as, app_db  # noqa: F401  (fixture + signed-in session helper)
import conftest


def _post(db, **kw):
    kw = {**dict(grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ99", location_id=1, basis_cents=-38,
                 cash_price=4.18), **kw}
    data.publish_bid(db, "rcm", 2, **kw)


def test_purchase_form_fills_market_and_basis_from_the_approved_bid(app_db):
    _post(app_db, futures_symbol="ZCZ26")
    _post(app_db, grain="Soybeans", futures_symbol="ZSX26", basis_cents=-55, cash_price=11.0)
    at = goto(_as(app_db, 2), "Purchases")
    assert not at.exception, at.exception
    sel = {s.label: s for s in at.selectbox}
    assert sel["Delivery period"].options == ["2999-12"]                           # only periods with an approved bid
    assert [o.replace("·", "-")[:4] for o in sel["Commodity and futures month"].options] == ["Corn", "Soyb"]
    basis = next(n for n in at.number_input if n.label == "Basis (¢)")
    assert basis.value == -38                                                      # the first bid (Corn) fills the basis
    next(t for t in at.text_input if t.label.startswith("…or type")).set_value("Smith Farms")
    next(n for n in at.number_input if n.label == "Bushels").set_value(5000)
    next(b for b in at.button if b.label == "Log purchase").click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    p = purchases.list_purchases(app_db, "rcm", conftest.user(app_db, 2))[0]
    assert (p["grain"], p["futures_symbol"], p["basis_cents"], p["delivery_period"], p["delivery_location_id"] if "delivery_location_id" in p else 1) == \
           ("Corn", "ZCZ26", -38, "2999-12", 1)


def test_purchase_form_without_a_bid_asks_for_a_manual_basis(app_db):
    at = goto(_as(app_db, 2), "Purchases")
    assert not at.exception
    assert any("No approved bid" in c.value for c in at.caption)
    assert next(n for n in at.number_input if n.label == "Basis (¢)").value is None


def test_admin_scrape_now_button_runs_the_tenant_scrape(app_db, monkeypatch):
    app_db.execute("INSERT INTO scrape_sources (source_id, tenant_id, kind, url, active) "
                   "VALUES (1, 'rcm', 'agricharts', 'https://x.example', 1)")
    items = [dict(location_name="Riverton", grain="Corn", delivery_month="2999-12", futures_symbol="ZCZ99",
                  basis_cents=-20, cash_price=4.60, delivery_start="2999-12-01", delivery_end="2999-12-31")]
    monkeypatch.setattr(scrapers, "fetch_source", lambda src: items)
    monkeypatch.setattr(scrape_run, "run", lambda db, only=None, fetch=lambda s: items, _r=scrape_run.run: _r(db, only, fetch))
    at = goto(_as(app_db, 2), "Review scraped bids")
    assert any("Not scraped yet" in c.value for c in at.caption)
    next(b for b in at.button if b.label == "Scrape bids now").click().run()
    assert not at.exception, at.exception
    assert any("Scrape finished" in s.value for s in at.success)
    assert len(data.list_suggestions(app_db, "rcm")) == 1
    assert any("Last scraped" in c.value for c in at.caption)


def test_only_the_open_tab_runs(app_db):
    at = _as(app_db, 2)                                       # opens on Offers: other pages must not have run
    assert not at.exception
    assert not at.dataframe and not [e for e in at.expander if e.label == "Post a bid"]
    assert [e for e in goto(at, "Bid board").expander if e.label == "Post a bid"]
