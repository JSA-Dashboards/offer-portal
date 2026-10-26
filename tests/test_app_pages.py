from conftest import goto, pages, stage, stages  # noqa: F401
from portal import contracts, data, farmers
from test_app import _as, app_db  # noqa: F401  (fixture + signed-in session helper)
import conftest

FLAT = dict(grain="Corn", contract_type="CASH", price_type="FIXED", bushels=5000, price=4.30, futures_symbol="ZCZ26")


def _views(at):
    return list(pages(at))


def _go(at, view):
    goto(at, view)
    assert not at.exception, at.exception
    return at


def _hit_offer(db):
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2026-12", futures_symbol="ZCZ26",
                     basis_cents=-38, cash_price=4.18)
    oid = data.create_offer(db, "rcm", conftest.user(db, 1), **FLAT, bid_id=data.list_bids(db, "rcm")[0]["bid_id"])
    data.act_on_offer(db, "rcm", conftest.user(db, 2), oid, "hit")
    return oid


def test_navigation_by_role(app_db):
    assert _views(_as(app_db, 2))[:3] == ["Offers", "Summary", "Hedge"]                 # elevator admin
    app_db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 's@rcm.com', 'elevator_staff')")
    staff = _views(_as(app_db, 20))
    assert "Summary" in staff and "Hedge" not in staff                                   # staff don't get the hedge page
    farmer = _views(_as(app_db, 1))
    assert not {"Summary", "Hedge", "Farmers", "Place offer"} & set(farmer)


def test_offers_page_has_outstanding_hit_completed_closed_stages_with_counts(app_db):
    _hit_offer(app_db)
    data.create_offer(app_db, "rcm", conftest.user(app_db, 1), **FLAT)
    at = _as(app_db, 2)
    assert stages(at) == ["Outstanding (1)", "Hit (1)", "Acknowledged (0)", "Completed (0)", "Closed (0)"]
    assert any("5,000 bu Corn" in m.value for m in at.markdown)
    assert any(b.label == "Mark hit" for b in at.button)                                  # outstanding stage
    stage(at, "Hit")
    assert not at.exception, at.exception
    assert any(b.label == "Acknowledge" for b in at.button)                              # hit stage: acknowledge first
    assert not any(b.label == "Complete and archive" for b in at.button)


def test_hit_offer_needs_a_contract_number_then_moves_to_completed(app_db):
    oid = _hit_offer(app_db)
    at = _as(app_db, 2)
    stage(at, "Hit")
    assert any("implied futures 4.6800" in m.value for m in at.markdown)
    next(b for b in at.button if b.label == "Acknowledge").click().run()                # staff takes it on ...
    assert not at.exception, at.exception
    assert data.list_offers(app_db, "rcm", conftest.user(app_db, 2))[0]["status"] == "acknowledged"
    stage(at, "Acknowledged")                                          # ... then must enter the contract number
    next(b for b in at.button if b.label == "Complete and archive").click().run()
    assert at.error and "contract number" in at.error[0].value.lower()
    assert data.list_offers(app_db, "rcm", conftest.user(app_db, 2))[0]["status"] == "acknowledged"   # still not completed
    next(t for t in at.text_input if t.label.startswith("Contract number")).set_value("C-5531")
    next(b for b in at.button if b.label == "Complete and archive").click().run()
    assert not at.exception, at.exception
    o = data.list_offers(app_db, "rcm", conftest.user(app_db, 2))[0]
    assert (o["offer_id"], o["status"], o["contract_ref"]) == (oid, "completed", "C-5531")
    stage(at, "Completed")
    assert any("Contract # C-5531" in c.value for c in at.caption)
    assert not any(b.label == "Complete and archive" for b in at.button)                  # archived: read-only


def test_summary_page_filters_by_commodity_period_entity_and_individual(app_db):
    pat = farmers.create_farmer(app_db, "rcm", 2, "Pat Farmer", entity_name="Farmer Bros LLC", client_id="1")
    joe = farmers.create_farmer(app_db, "rcm", 2, "Joe Solo", entity_name="Smith Farms", client_id="2")
    data.place_offer_for_farmer(app_db, "rcm", 2, pat, **FLAT, delivery_start="2026-12-05")
    data.place_offer_for_farmer(app_db, "rcm", 2, joe, **{**FLAT, "grain": "Soybeans", "futures_symbol": "ZSX26"},
                                delivery_start="2027-01-10")
    at = _go(_as(app_db, 2), "Summary")
    assert {m.label: m.value for m in at.metric} == {"Offers": "2", "Bushels": "10,000"}
    assert at.dataframe
    {m.label: m for m in at.multiselect}["Commodity"].set_value(["Soybeans"]).run()
    assert {m.label: m.value for m in at.metric}["Offers"] == "1"
    {m.label: m for m in at.multiselect}["Commodity"].set_value([]).run()
    {t.label: t for t in at.text_input}["Entity name contains"].set_value("farmer bros").run()
    assert {m.label: m.value for m in at.metric}["Offers"] == "1"
    {t.label: t for t in at.text_input}["Entity name contains"].set_value("").run()
    {t.label: t for t in at.text_input}["Individual name contains"].set_value("joe").run()
    assert {m.label: m.value for m in at.metric}["Offers"] == "1"
    {t.label: t for t in at.text_input}["Individual name contains"].set_value("nobody").run()
    assert any("No offers match" in i.value for i in at.info)


def test_hedge_page_shows_contracts_and_marks_offers_hedged(app_db):
    oid = _hit_offer(app_db)
    data.create_offer(app_db, "rcm", conftest.user(app_db, 1), **{**FLAT, "bushels": 10000, "price": 4.60})
    at = _go(_as(app_db, 2), "Hedge")
    assert len(at.dataframe) >= 4                                                          # now + potential, 2 tables each
    assert any("implied futures" in c.value for c in at.caption)
    now_totals = at.dataframe[0].value
    assert list(now_totals["Contracts"]) == [1.0] and list(now_totals["Bushels"]) == [5000]
    {m.label: m for m in at.multiselect}["Hit offers not yet hedged"].select(
        data.hedge_summary(app_db, "rcm", 2)["now_offers"][0]).run()
    next(b for b in at.button if b.label == "Mark selected as hedged").click().run()
    assert not at.exception, at.exception
    assert [o["hedged_at"] is not None for o in data.list_offers(app_db, "rcm", conftest.user(app_db, 2))
            if o["offer_id"] == oid] == [True]
    assert any("No hit offers are waiting" in i.value for i in at.info)


def test_posting_a_bid_reports_how_many_flat_offers_were_repriced(app_db):
    # The form posts for the first location (id 1), so the live bid must be that location's slot.
    data.publish_bid(app_db, "rcm", 2, grain="Corn", delivery_month=data_month(), futures_symbol=contracts.options("Corn")[0],
                     location_id=1, basis_cents=-38, cash_price=4.18)
    bid = data.list_bids(app_db, "rcm")[0]["bid_id"]
    hit = data.create_offer(app_db, "rcm", conftest.user(app_db, 1), **FLAT, bid_id=bid)
    data.act_on_offer(app_db, "rcm", conftest.user(app_db, 2), hit, "hit")                 # locked, not repriced
    data.create_offer(app_db, "rcm", conftest.user(app_db, 1), **FLAT, bid_id=bid)
    at = _go(_as(app_db, 2), "Bid board")
    next(s for s in at.selectbox if s.label.startswith("Commodity and futures month")).set_value("Corn|" + contracts.options("Corn")[0])
    next(n for n in at.number_input if n.label.startswith("Basis")).set_value(-30)
    next(s for s in at.selectbox if s.label == "Delivery month").set_value(data_month())
    next(b for b in at.button if b.label == "Post bid").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert any("1 outstanding flat-price offer" in s.value for s in at.success)


def data_month():
    """The bid form's first delivery-month option is the current month."""
    from datetime import date
    return date.today().strftime("%Y-%m")


# ── auto-hit page ────────────────────────────────────────────────────────────
def _reached_offer(db):
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2026-12", futures_symbol="ZCZ26",
                     basis_cents=-38, cash_price=4.50)
    return data.create_offer(db, "rcm", conftest.user(db, 1), **FLAT, bid_id=data.list_bids(db, "rcm")[0]["bid_id"])


def test_auto_hit_page_is_admin_only(app_db):
    assert "Auto-hit" in _views(_as(app_db, 2))
    app_db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 's@rcm.com', 'elevator_staff')")
    assert "Auto-hit" not in _views(_as(app_db, 20))
    assert "Auto-hit" not in _views(_as(app_db, 1))


def test_adding_a_rule_hits_offers_the_bid_already_reached(app_db):
    oid = _reached_offer(app_db)
    at = _go(_as(app_db, 2), "Auto-hit")
    assert any("No rules yet" in i.value for i in at.info)
    next(b for b in at.button if b.label == "Add rule").click().run()               # default = any scope, no size limit
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert any("Rule added. 1 outstanding offer" in s.value for s in at.success)
    o = [x for x in data.list_offers(app_db, "rcm", conftest.user(app_db, 2)) if x["offer_id"] == oid][0]
    assert (o["status"], o["auto_hit"]) == ("hit", 1)
    assert at.dataframe and list(at.dataframe[0].value["Bushels"]) == [5000]          # listed under "recently hit"
    goto(at, "Offers")
    stage(at, "Hit")
    assert any("Hit automatically" in c.value for c in at.caption)


def test_rule_toggle_and_check_now(app_db):
    data.add_auto_hit_rule(app_db, "rcm", 2, max_bushels=1000)                        # too small to cover a 5,000 bu offer
    oid = _reached_offer(app_db)
    at = _go(_as(app_db, 2), "Auto-hit")
    assert any("up to 1,000 bu per offer" in m.value for m in at.markdown)
    next(b for b in at.button if b.label == "Check outstanding offers now").click().run()
    assert any("Checked. 0 offers hit." in s.value for s in at.success)
    assert data.list_offers(app_db, "rcm", conftest.user(app_db, 2))[0]["status"] == "submitted"


def test_posting_a_bid_says_how_many_offers_were_hit_automatically(app_db):
    data.add_auto_hit_rule(app_db, "rcm", 2)
    data.publish_bid(app_db, "rcm", 2, grain="Corn", delivery_month=data_month(), futures_symbol=contracts.options("Corn")[0],
                     location_id=1, basis_cents=-38, cash_price=4.18)
    oid = data.create_offer(app_db, "rcm", conftest.user(app_db, 1), **FLAT,
                            bid_id=data.list_bids(app_db, "rcm")[0]["bid_id"])           # 4.30 > bid 4.18: outstanding
    assert data.list_offers(app_db, "rcm", conftest.user(app_db, 2))[0]["status"] == "submitted"
    at = _go(_as(app_db, 2), "Bid board")
    next(s for s in at.selectbox if s.label.startswith("Commodity and futures month")).set_value("Corn|" + contracts.options("Corn")[0])
    next(n for n in at.number_input if n.label.startswith("Basis")).set_value(-30)
    next(n for n in at.number_input if n.label.startswith("Cash")).set_value(4.35)
    next(s for s in at.selectbox if s.label == "Delivery month").set_value(data_month())
    next(b for b in at.button if b.label == "Post bid").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    assert any("1 offer was hit automatically" in s.value for s in at.success)
    assert data.list_offers(app_db, "rcm", conftest.user(app_db, 2))[0]["status"] == "hit"


def test_each_role_sees_its_own_set_of_pages(app_db):
    app_db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 's@rcm.com', 'elevator_staff')")
    farmer, staff, admin = (set(_views(_as(app_db, u))) for u in (1, 20, 2))
    assert farmer == {"Bid board", "Make an offer", "My offers", "USDA calendar"}
    assert {"Offers", "Purchases", "Place offer", "Bid board", "USDA calendar"} <= staff
    assert not staff & {"Hedge", "Auto-hit", "Review scraped bids"}            # admin only
    assert staff < admin and {"Hedge", "Auto-hit", "Review scraped bids"} <= admin


def test_the_calendar_page_renders_and_staff_cannot_edit_bids(app_db):
    app_db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 's@rcm.com', 'elevator_staff')")
    at = _go(_as(app_db, 1), "USDA calendar")
    assert not at.exception and at.dataframe
    at = _go(_as(app_db, 20), "Bid board")
    assert not at.exception and not [e for e in at.expander if e.label in ("Post a bid", "Retire a bid")]
    at = _go(_as(app_db, 2), "Bid board")
    assert [e for e in at.expander if e.label == "Post a bid"]
