import scrape_run
from portal import data, scrapers

AGRI = [
    {"name": "Riverton", "hide_on_sites_and_apis": "0", "cashbids": [
        {"name": "Corn", "symbol": "ZCZ26", "basis": -38, "price": "$4.18", "delivery_start": "09/01/2999"},
        {"name": "Soybeans", "symbol": "ZSX26", "basis": -45, "price": "$11.90", "delivery_start": "10/01/2999"}]},
    {"name": "Hidden", "hide_on_sites_and_apis": "1", "cashbids": [
        {"name": "Corn", "symbol": "ZCZ26", "basis": -1, "delivery_start": "09/01/2999"}]},
    {"name": "Futures Only", "cashbids": [
        {"name": "Corn", "symbol": "ZCZ26", "basis": 0, "delivery_start": "09/01/2999"}]},
]
BUSHEL = """<div class='cbCommodity'><h3>Corn</h3>
<ul><li class='c1'>Delivery</li><li class='c2'>Bid</li></ul>
<ul><li class='c1'>Harvest 2099</li><li class='c2'>4.26</li><li class='c3'>-0.30</li><li class='c6'>Dec 99 Corn</li></ul>
<ul><li class='c1'>March 2099</li><li class='c2'>4.50</li><li class='c3'>-0.20</li><li class='c6'>Mar 99 Corn</li></ul>
<ul><li class='c1'>Bad row</li><li class='c3'>n/a</li></ul></div>"""


def test_parse_agricharts_skips_hidden_and_futures_only():
    out = scrapers.parse_agricharts(AGRI)
    assert [(o["location_name"], o["grain"], o["delivery_month"], o["basis_cents"], o["cash_price"])
            for o in out] == [("Riverton", "Corn", "2999-09", -38, 4.18),
                              ("Riverton", "Soybeans", "2999-10", -45, 11.90)]


def test_parse_bushelsites():
    out = scrapers.parse_bushelsites(BUSHEL, "Dorans")
    assert [(o["delivery_month"], o["futures_symbol"], o["basis_cents"], o["cash_price"]) for o in out] == \
        [("2099-10", "ZCZ99", -30, 4.26), ("2099-03", "ZCH99", -20, 4.50)]


def _loc(db, tenant, name):
    db.execute("INSERT INTO locations (tenant_id, name) VALUES (%s, %s)", (tenant, name))


def test_sync_stages_dedupes_and_supersedes(db):
    _loc(db, "rcm", "Riverton")
    items = scrapers.parse_agricharts(AGRI) + [
        dict(location_name="Nowhere", grain="Corn", delivery_month="2999-09",
             futures_symbol="ZCZ26", basis_cents=0, cash_price=None),
        dict(location_name="Riverton", grain="Corn", delivery_month="2001-01",
             futures_symbol="ZCH01", basis_cents=0, cash_price=None)]
    assert data.sync_suggestions(db, "rcm", "t", items) == dict(staged=2, unchanged=0, unmatched=1, stale=1)
    assert data.sync_suggestions(db, "rcm", "t", items)["staged"] == 0               # re-run: nothing new
    items[0] = {**items[0], "basis_cents": -40}
    assert data.sync_suggestions(db, "rcm", "t", items)["staged"] == 1               # changed -> new one
    open_corn = [s for s in data.list_suggestions(db, "rcm") if s["grain"] == "Corn"]
    assert [s["basis_cents"] for s in open_corn] == [-40]                            # old superseded
    assert data.list_bids(db, "rcm") == []                                           # never auto-published
    assert data.list_suggestions(db, "dorans") == []                                 # tenant-scoped


def test_sync_skips_what_is_already_live(db):
    _loc(db, "rcm", "Riverton")
    lid = data.list_locations(db, "rcm")[0]["location_id"]
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2999-09", location_id=lid,
                     futures_symbol="ZCZ26", basis_cents=-38, cash_price=4.18)
    n = data.sync_suggestions(db, "rcm", "t", scrapers.parse_agricharts(AGRI))
    assert (n["staged"], n["unchanged"]) == (1, 1)


def test_runner_isolates_failures_and_records_status(db):
    _loc(db, "rcm", "Riverton")
    db.execute("INSERT INTO scrape_sources (tenant_id, kind, url) VALUES ('rcm','agricharts','https://x')")
    db.execute("INSERT INTO scrape_sources (tenant_id, kind, url, location_name) "
               "VALUES ('dorans','bushelsites','https://y','Dorans')")

    def fake(src):
        if src["tenant_id"] == "dorans":
            raise TimeoutError("site down")
        return scrapers.parse_agricharts(AGRI)
    res = scrape_run.run(db, fetch=fake)
    assert res[("rcm", "agricharts")]["staged"] == 2
    st = {r["tenant_id"]: r["last_status"] for r in db.query("SELECT tenant_id, last_status FROM scrape_sources")}
    assert st["rcm"].startswith("ok staged=2") and st["dorans"].startswith("error: TimeoutError")
    assert len(data.list_suggestions(db, "rcm")) == 2
