import pytest
from portal import data, people, platform
from portal.data import PortalError

PA = 5          # platform admin in the shared fixture


def test_only_platform_admin(db):
    for uid in (1, 2, 4):
        with pytest.raises(PortalError):
            platform.list_tenants(db, uid)
        with pytest.raises(PortalError):
            platform.create_tenant(db, uid, "newco", "New Co", "a@b.com", [("Main", "IL")])
        with pytest.raises(PortalError):
            platform.set_tenant_status(db, uid, "rcm", False)


def test_create_tenant_makes_locations_and_admin_invite(db):
    tok = platform.create_tenant(db, PA, "Newco", " New Co ", "boss@newco.com", [("Main", "il"), ("", None)])
    assert [(l["name"], l["state"]) for l in data.list_locations(db, "newco")] == [("Main", "IL")]
    assert data.get_tenant(db, "newco")["name"] == "New Co"
    assert people.invite_preview(db, tok, "newco") == {"email": "boss@newco.com", "role": "elevator_admin"}
    u = people.accept_invite(db, tok, "newco", "Boss")
    assert u["role"] == "elevator_admin" and u["tenant_id"] == "newco"


@pytest.mark.parametrize("slug", ["a", "Bad Slug", "has_underscore", "admin", "-lead", "rcm"])
def test_bad_or_taken_slugs_rejected(db, slug):
    with pytest.raises(PortalError):
        platform.create_tenant(db, PA, slug, "X", "a@b.com", [("Main", None)])


def test_create_tenant_needs_location_and_valid_admin_email(db):
    with pytest.raises(PortalError):
        platform.create_tenant(db, PA, "noloc", "No Loc", "a@b.com", [])
    with pytest.raises(PortalError):
        platform.create_tenant(db, PA, "bademail", "Bad", "nope", [("Main", None)])


def test_update_tenant_validates_branding(db):
    good = dict(name="RCM Coop", logo_url="https://rcm.example/logo.png", primary_color="#112233",
                accent_color="#abcdef", support_email="help@rcm.example", from_name="RCM",
                open_pricing=False, hedging_enabled=True)
    platform.update_tenant(db, PA, "rcm", **good)
    t = data.get_tenant(db, "rcm")
    assert (t["primary_color"], t["open_pricing"], t["hedging_enabled"]) == ("#112233", 0, 1)
    for bad in (dict(primary_color="red"), dict(logo_url="http://insecure.example/x.png"),
                dict(logo_url="https://10.0.0.1/x.png"), dict(support_email="nope"), dict(name=" ")):
        with pytest.raises(PortalError):
            platform.update_tenant(db, PA, "rcm", **{**good, **bad})


def test_suspend_hides_tenant(db):
    platform.set_tenant_status(db, PA, "rcm", False)
    assert data.get_tenant(db, "rcm") is None
    platform.set_tenant_status(db, PA, "rcm", True)
    assert data.get_tenant(db, "rcm")


def test_locations_and_sources(db):
    platform.add_location(db, PA, "rcm", "Riverton", "il")
    with pytest.raises(PortalError):
        platform.add_location(db, PA, "rcm", "riverton", None)           # duplicate (case-insensitive)
    platform.add_source(db, PA, "rcm", "agricharts", "https://www.rcmcoop.com", None)
    platform.add_source(db, PA, "rcm", "bushelsites", "https://www.example.com/cash-bids", "Riverton")
    for args in (("agricharts", "http://insecure.com", None), ("agricharts", "https://127.0.0.1", None),
                 ("telepathy", "https://x.com", None), ("bushelsites", "https://x.com/y", "Nowhere"),
                 ("bushelsites", "https://x.com/y", None)):
        with pytest.raises(PortalError):
            platform.add_source(db, PA, "rcm", *args)
    srcs = platform.list_sources(db, PA, "rcm")
    assert [s["kind"] for s in srcs] == ["agricharts", "bushelsites"]
    platform.set_source_active(db, PA, "rcm", srcs[0]["source_id"], False)
    assert platform.list_sources(db, PA, "rcm")[0]["active"] == 0
    assert platform.list_sources(db, PA, "dorans") == []                 # tenant-scoped


def test_usage_table_rolls_up_by_tenant(db):
    data.publish_bid(db, "rcm", 2, grain="Corn", delivery_month="2026-12", basis_cents=-38)
    oid = data.create_offer(db, "rcm", db.query("SELECT * FROM users WHERE user_id = 1")[0], grain="Corn",
                            contract_type="HTA", price_type="BASIS", bushels=1000, basis_cents=-30,
                            futures_symbol="ZCZ26")
    data.act_on_offer(db, "rcm", db.query("SELECT * FROM users WHERE user_id = 2")[0], oid, "hit")
    month = db.query("SELECT SUBSTR(CAST(created_at AS VARCHAR), 1, 7) AS m FROM usage_events LIMIT 1")[0]["m"]
    rows = {r["Tenant"]: r for r in platform.usage_table(db, PA, month)}
    assert (rows["RCM"]["bid_published"], rows["RCM"]["offer_submitted"], rows["RCM"]["offer_hit"],
            rows["RCM"]["active_farmers"]) == (1, 1, 1, 1)
    assert rows["DORANS"]["offer_submitted"] == 0 and rows["DORANS"]["active_farmers"] == 1
    with pytest.raises(PortalError):
        platform.usage_table(db, PA, "2026-13")
    with pytest.raises(PortalError):
        platform.usage_table(db, 2, month)
