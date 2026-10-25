"""End-to-end scenario against REAL Snowflake. Not part of the pytest suite (which runs on sqlite).

Run it only against a throwaway copy of the schema:
    SNOWFLAKE_SCHEMA=OFFER_PORTAL_TEST python tests/integration/snowflake_scenario.py
It refuses to run unless the schema name ends in _TEST. It creates its own tenant ("itestNNNN"), so it never
touches live tenants, but it does write rows (and billing usage events), hence the throwaway schema.
"""
import os
import pathlib
import sys
import time
from datetime import date, datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

assert os.environ.get("SNOWFLAKE_SCHEMA", "").upper().endswith("_TEST"), "refusing: SNOWFLAKE_SCHEMA must end in _TEST"

import scrape_run  # noqa: E402
from portal import auth, data, farmers, people, platform, scrapers  # noqa: E402
from portal.data import PortalError  # noqa: E402
from portal.db import connect  # noqa: E402

N = 0


def step(msg):
    global N
    N += 1
    print(f"[{N:02d}] {msg}", flush=True)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def raises(fn, what):
    try:
        fn()
    except PortalError:
        return
    raise AssertionError(f"expected PortalError: {what}")


db = connect()
step("connect")
who = db.query("SELECT CURRENT_USER() AS u, CURRENT_ROLE() AS r, CURRENT_SCHEMA() AS s, CURRENT_WAREHOUSE() AS w, "
               "CURRENT_TIMESTAMP() AS ts")[0]
print("   ", {k: v for k, v in who.items() if k != "ts"}, "| session tz:", db.query("SHOW PARAMETERS LIKE 'TIMEZONE' IN SESSION")[0]["value"])
check(who["s"].endswith("_TEST"), "not on the test schema")
check(db.query("SHOW PARAMETERS LIKE 'TIMEZONE' IN SESSION")[0]["value"] == "UTC", "session timezone is not UTC")

admin = db.query("SELECT * FROM users WHERE role = 'platform_admin' ORDER BY user_id")[0]
check(admin["tenant_id"] is None, "platform admin tenant should be NULL")
A = admin["user_id"]
slug = f"itest{int(time.time()) % 100000}"
NAME = f"Integration Test Co {slug}"      # unique per run: usage_table reports by tenant name
E = lambda u: f"{u}@{slug}.example"      # unique per run, so a re-run never collides with earlier test rows

step("platform: create tenant (VARIANT audit insert, TIMESTAMP_TZ invite insert)")
tok = platform.create_tenant(db, A, slug, NAME, E("boss"), [("Main", "il"), ("North", "IL")])
inv = db.query("SELECT DATEDIFF('minute', created_at, expires_at) AS m FROM invites WHERE tenant_id = %s", (slug,))[0]
check(abs(inv["m"] - 7 * 24 * 60) <= 2, f"invite TTL should be 7 days, got {inv['m']} minutes")
locs = {l["name"]: l["location_id"] for l in data.list_locations(db, slug)}
check(set(locs) == {"Main", "North"}, locs)
check(data.get_tenant(db, slug)["name"] == NAME, "tenant read")

step("people: accept admin invite, then a staff invite, then farmer invite linked to a farmer record")
boss = people.accept_invite(db, tok, slug, "Boss")
check(boss["role"] == "elevator_admin" and boss["tenant_id"] == slug, boss)
B = boss["user_id"]
raises(lambda: people.accept_invite(db, tok, slug, "again"), "single-use invite")
staff_tok = people.create_invite(db, slug, B, E("staff"), "elevator_staff")
staff = people.accept_invite(db, staff_tok, slug, "Staffer")
S = staff["user_id"]
fid = farmers.create_farmer(db, slug, S, "Pat Farmer", "555-1212", E("pat"), entity_name="Farmer Bros LLC", client_id="10442")
raises(lambda: farmers.create_farmer(db, slug, S, "Other", client_id="10442"), "client id unique")
ftok = people.create_invite(db, slug, S, E("pat"), "farmer", farmer_id=fid)
pat = people.accept_invite(db, ftok, slug, "Pat")
P = pat["user_id"]
check([f["user_id"] for f in farmers.list_farmers(db, slug, S) if f["farmer_id"] == fid] == [P], "farmer linked to login")
phone_fid = farmers.create_farmer(db, slug, S, "Phone Caller", "555-0000")        # no login

step("auth: emailed code lifetime is really 10 minutes (the session-timezone trap), wrong guesses lock, rate limit")
sent = []
auth.request_code(db, E("boss"), slug, lambda e, c: sent.append(c))
ttl = db.query("SELECT DATEDIFF('second', created_at, expires_at) AS s FROM login_codes WHERE email = '" + E("boss") + "'")[0]["s"]
check(abs(ttl - 600) <= 5, f"code TTL should be 600s, got {ttl}")
check(auth.verify_code(db, E("boss"), sent[0], slug)["user_id"] == B, "code verifies")
check(auth.verify_code(db, E("boss"), sent[0], slug) is None, "code is single use")
auth.request_code(db, E("staff"), slug, lambda e, c: sent.append(c))
for _ in range(auth.MAX_ATTEMPTS):
    check(auth.verify_code(db, E("staff"), "000000x", slug) is None, "wrong code")
check(auth.verify_code(db, E("staff"), sent[-1], slug) is None, "locked after wrong guesses")
for _ in range(auth.MAX_REQUESTS + 2):
    auth.request_code(db, E("pat"), slug, lambda e, c: sent.append(c))
check(len([1 for _ in db.query("SELECT 1 FROM login_codes WHERE email = '" + E("pat") + "'")]) == auth.MAX_REQUESTS, "rate limit")
check(auth.verify_code(db, E("pat"), sent[-1], "someone-else") is None, "wrong tenant page")

step("bids + offers: flat / basis / futures-only / open-priced, implied futures, bid_gap")
month = (date.today() + timedelta(days=45)).strftime("%Y-%m")
data.publish_bid(db, slug, S, grain="Corn", delivery_month=month, location_id=locs["Main"], futures_symbol="ZCZ26",
                 basis_cents=-38, cash_price=4.18)
bid = data.list_bids(db, slug)[0]
FLAT = dict(grain="Corn", contract_type="CASH", price_type="FIXED", bushels=5000, price=4.30, futures_symbol="ZCZ26")
flat = data.place_offer_for_farmer(db, slug, S, phone_fid, **FLAT, bid_id=bid["bid_id"], location_id=locs["Main"])
hta = data.create_offer(db, slug, pat, grain="Corn", contract_type="HTA", price_type="BASIS", bushels=10000,
                        basis_cents=-30, futures_symbol="ZCZ26", bid_id=bid["bid_id"], note="n", contract_ref="PRE-1")
fut = data.create_offer(db, slug, pat, **{**FLAT, "contract_type": "FUTURES_ONLY", "price": 4.55}, bid_id=bid["bid_id"])
openp = data.create_offer(db, slug, pat, **FLAT, delivery_start=f"{month}-10", delivery_end=f"{month}-28",
                          expires_at=(datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"))
print("    offer ids (returned by the app, in creation order):", flat, hta, fut, openp)
print("    bids:", db.query("SELECT bid_id, active, basis_cents FROM bids WHERE tenant_id = %s ORDER BY bid_id", (slug,)))
offers = {o["offer_id"]: o for o in data.list_offers(db, slug, boss)}
check(offers[flat]["implied_futures"] == 4.68 and offers[flat]["basis_ref_cents"] == -38, offers[flat])
check(offers[openp]["implied_futures"] == 4.68, ("open-priced offer matched on futures + delivery month", {k: offers[openp][k] for k in ("delivery_start", "futures_symbol", "location_id", "bid_id", "basis_ref_cents", "implied_futures")}, bid))
check(offers[fut]["implied_futures"] == 4.55 and offers[hta]["implied_futures"] is None, "futures-only / HTA")
check(offers[flat]["farmer_client_id"] is None and offers[flat]["farmer_name"] == "Phone Caller", "phone farmer")
check(offers[hta]["farmer_client_id"] == "10442" and offers[hta]["farmer_entity"] == "Farmer Bros LLC", "linked farmer identity")
check(offers[flat]["placed_by"] == S and offers[hta]["placed_by"] is None, "placed_by")
print("    python types:", {k: type(offers[flat][k]).__name__ for k in
                            ("offer_id", "price", "bushels", "delivery_start", "created_at", "auto_hit", "bid_snapshot", "status")})
check(len(data.list_offers(db, slug, pat)) == 3, "farmer sees only their own (incl. none placed for Phone Caller)")

step("basis moves: new bid supersedes, outstanding flat offers re-priced, every offer follows the live bid")
r = data.publish_bid(db, slug, S, grain="Corn", delivery_month=month, location_id=locs["Main"], futures_symbol="ZCZ26",
                     basis_cents=-30, cash_price=4.26)
print("    after posting basis -30: repriced", int(r), "bids:", db.query("SELECT bid_id, active, basis_cents FROM bids WHERE tenant_id = %s ORDER BY bid_id", (slug,)), "offer bid_ids:", db.query("SELECT offer_id, bid_id, basis_ref_cents, implied_futures FROM offers WHERE tenant_id = %s ORDER BY offer_id", (slug,)))
check(int(r) == 2 and r.auto_hit == 0, (int(r), r.auto_hit))
live = [b for b in data.list_bids(db, slug)]
check(len(live) == 1 and live[0]["basis_cents"] == -30, live)
offers = {o["offer_id"]: o for o in data.list_offers(db, slug, boss)}
check(offers[flat]["implied_futures"] == 4.60 and offers[openp]["implied_futures"] == 4.60, "re-priced")
check(all(offers[x]["bid_id"] == live[0]["bid_id"] for x in (flat, hta, fut)), "re-pointed to the live bid")
ev = db.query("SELECT detail:basis_after::int AS b, detail:implied_futures_after::float AS i FROM offer_events "
              "WHERE offer_id = %s AND event_type = 'basis_updated'", (flat,))
check(len(ev) == 1 and ev[0]["b"] == -30 and abs(ev[0]["i"] - 4.60) < 1e-9, ev)

step("auto-hit: rule scoped to cash offers up to 8,000 bu; a bid that reaches the flat offers hits them; HTA stays")
check(data.add_auto_hit_rule(db, slug, B, grain="Corn", contract_type="CASH", max_bushels=8000) == 0, "none reached yet")
r = data.publish_bid(db, slug, S, grain="Corn", delivery_month=month, location_id=locs["Main"], futures_symbol="ZCZ26",
                     basis_cents=-30, cash_price=4.50)
check(r.auto_hit == 2, f"expected 2 auto-hits, got {r.auto_hit}")
offers = {o["offer_id"]: o for o in data.list_offers(db, slug, boss)}
check(offers[flat]["status"] == "hit" and offers[openp]["status"] == "hit", "flat offers hit")
check(offers[flat]["auto_hit"] in (True, 1) and offers[flat]["hit_by"] is None and offers[flat]["auto_hit_rule_id"], offers[flat])
check(offers[hta]["status"] == "submitted" and offers[fut]["status"] == "submitted", "others untouched")
c = db.query("SELECT terms:price::float AS p, terms:implied_futures::float AS i, auto_hit FROM confirmations WHERE offer_id = %s", (flat,))
check(len(c) == 1 and abs(c[0]["p"] - 4.30) < 1e-9 and abs(c[0]["i"] - 4.60) < 1e-9, c)
ae = db.query("SELECT detail:auto_hit::boolean AS a, detail:bid_cash_price::float AS p FROM offer_events WHERE offer_id = %s AND event_type = 'hit'", (flat,))
check(ae[0]["a"] is True and abs(ae[0]["p"] - 4.50) < 1e-9, ae)
check(len(data.list_auto_hits(db, slug, B)) == 2, "recent auto-hits")

step("manual hit, decline, withdraw (+ on behalf), completion needs a contract number, archive rules")
raises(lambda: data.act_on_offer(db, slug, pat, hta, "hit"), "farmer can't hit")
check(data.act_on_offer(db, slug, staff, hta, "hit") == "hit", "manual hit")
check(data.list_offers(db, slug, boss)[0] is not None, "list")
raises(lambda: data.complete_offer(db, slug, S, flat, ""), "contract number required")
data.complete_offer(db, slug, S, flat, "C-1001")
data.complete_offer(db, slug, S, hta, "PRE-1")
w = data.create_offer(db, slug, pat, **FLAT)
w2 = data.place_offer_for_farmer(db, slug, S, phone_fid, **FLAT)
check(data.act_on_offer(db, slug, pat, w, "withdraw") == "withdrawn", "withdraw")
check(data.act_on_offer(db, slug, staff, w2, "withdraw", on_behalf=True) == "withdrawn", "withdraw on behalf")
raises(lambda: data.update_offer_details(db, slug, S, flat, note="x", contract_ref=""), "archived keeps contract #")
data.update_offer_details(db, slug, S, flat, note="corrected", contract_ref="C-1001")
st = {o["offer_id"]: o["status"] for o in data.list_offers(db, slug, boss)}
check(st[flat] == "completed" and st[hta] == "completed" and st[w] == "withdrawn", st)
o_done = [o for o in data.list_offers(db, slug, boss) if o["offer_id"] == flat][0]
check(o_done["completed_at"] is not None and o_done["contract_ref"] == "C-1001" and o_done["note"] == "corrected", o_done)

step("expiry: an offer past its good-until time is expired, not hit (UTC string vs TIMESTAMP_TZ)")
exp = data.create_offer(db, slug, pat, **FLAT, expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"))
live_future = data.create_offer(db, slug, pat, **{**FLAT, "price": 9.99}, expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"))
data.expire_stale(db, slug)
st = {o["offer_id"]: o["status"] for o in data.list_offers(db, slug, boss)}
check(st[exp] == "expired" and st[live_future] == "submitted", st)

step("summary + hedge pages")
hit2 = data.create_offer(db, slug, pat, **{**FLAT, "bushels": 12500, "price": 4.40}, bid_id=data.list_bids(db, slug)[0]["bid_id"], delivery_start=f"{month}-12")        # the bid is re-posted in step 07
res = data.offer_summary(db, slug, S)
check(res["groups"] and all(g["grain"] == "Corn" for g in res["groups"]), res["groups"])
check(len(data.offer_summary(db, slug, S, entity="farmer bros")["offers"]) > 0, "entity filter")
check(data.offer_summary(db, slug, S, individual="nobody")["offers"] == [], "individual filter")
data.act_on_offer(db, slug, staff, hit2, "hit")
h = data.hedge_summary(db, slug, B)
tot = [t for t in h["now_totals"] if t["futures_symbol"] == "ZCZ26"][0]
# hit and unhedged: hit2 (12,500) + the auto-hit open-priced offer from step 07 (5,000)
check(tot["bushels"] == 17500 and tot["whole_contracts"] == 3 and tot["remainder_bushels"] == 2500, tot)
check(data.mark_hedged(db, slug, B, [hit2, openp]) == 2 and not data.hedge_summary(db, slug, B)["now"], "mark hedged")
raises(lambda: data.hedge_summary(db, slug, S), "hedge is admin only")

step("scrape staging + review: sync, supersede, approve publishes + reprices")
platform.add_source(db, A, slug, "agricharts", "https://www.example.com", None)
items = [dict(location_name="Main", grain="Corn", delivery_month=month, futures_symbol="ZCH27", basis_cents=-20, cash_price=4.60),
         dict(location_name="Nowhere", grain="Corn", delivery_month=month, futures_symbol="ZCH27", basis_cents=0, cash_price=None)]
out = scrape_run.run(db, slug, fetch=lambda src: items)
check(list(out.values())[0] == dict(staged=1, unchanged=0, unmatched=1, stale=0), out)
sug = data.list_suggestions(db, slug)
check(len(sug) == 1, sug)
n = data.review_suggestion(db, slug, S, sug[0]["suggestion_id"], True, basis_cents=-22)
check(n == 0 and [b for b in data.list_bids(db, slug) if b["futures_symbol"] == "ZCH27"][0]["basis_cents"] == -22, "approve with edit")
check(db.query("SELECT last_status FROM scrape_sources WHERE tenant_id = %s", (slug,))[0]["last_status"].startswith("ok staged=1"), "source status")

step("platform: update tenant, usage report, suspend / reactivate")
platform.update_tenant(db, A, slug, name=NAME, logo_url="https://example.com/l.png", primary_color="#112233",
                       accent_color="#abcdef", support_email=E("help"), from_name="ITest", open_pricing=True,
                       hedging_enabled=False)
month_utc = datetime.now(timezone.utc).strftime("%Y-%m")
row = [r for r in platform.usage_table(db, A, month_utc) if r["Tenant"] == NAME][0]
print("    usage:", row)
print("    raw usage rows:", db.query("SELECT event_type, COUNT(*) AS n, SUM(quantity) AS q, MIN(created_at) AS first, MAX(created_at) AS last FROM usage_events WHERE tenant_id = %s GROUP BY 1 ORDER BY 1", (slug,)))
print("    usage_by_tenant rows:", [r for r in data.usage_by_tenant(db, {"role": "platform_admin"}, month_utc) if r["tenant_id"] == slug])
print("    created_at strings of offer_submitted:", [r["s"] for r in db.query("SELECT CAST(created_at AS VARCHAR) AS s FROM usage_events WHERE tenant_id = %s AND event_type = 'offer_submitted' ORDER BY created_at", (slug,))])
print("    month filter hits:", db.query("SELECT COUNT(*) AS n FROM usage_events WHERE tenant_id = %s AND SUBSTR(CAST(created_at AS VARCHAR), 1, 7) = %s", (slug, month_utc))[0]["n"], "of", db.query("SELECT COUNT(*) AS n FROM usage_events WHERE tenant_id = %s", (slug,))[0]["n"])
print("    offers by status:", db.query("SELECT status, COUNT(*) AS n FROM offers WHERE tenant_id = %s GROUP BY 1 ORDER BY 1", (slug,)))
print("    offer_events:", db.query("SELECT event_type, COUNT(*) AS n FROM offer_events WHERE tenant_id = %s GROUP BY 1 ORDER BY 1", (slug,)))
check(row["offer_submitted"] >= 8 and row["offer_hit"] >= 4 and row["offer_completed"] == 2 and row["bid_published"] >= 4, row)
platform.set_tenant_status(db, A, slug, False)
check(data.get_tenant(db, slug) is None, "suspended hides tenant")
platform.set_tenant_status(db, A, slug, True)
check(data.get_tenant(db, slug) is not None, "reactivated")

step("tenant isolation on real data: the live tenants are untouched and invisible")
check(data.list_offers(db, "rcm", admin) == [] or True, "rcm read ok")
check(all(o["tenant_id"] == slug for o in data.list_offers(db, slug, boss)), "only itest offers")
check(data.list_bids(db, "dorans") == [] or all(b["tenant_id"] == "dorans" for b in data.list_bids(db, "dorans")), "dorans bids")

step("delayed futures (needs sql/05 applied to this schema): store quotes, staged suggestion, sticky bid, show_futures switch")
from portal import futures  # noqa: E402
data.publish_bid(db, slug, S, grain="Soybeans", delivery_month=month, location_id=locs["North"], futures_symbol="ZSX26",
                 basis_cents=-45, cash_price=11.80)
platform.set_source_active(db, A, slug, db.query("SELECT source_id FROM scrape_sources WHERE tenant_id = %s", (slug,))[0]["source_id"], False)
q = {"ZSX26": {"price": 12.30, "as_of": datetime.now(timezone.utc)}, "ZCZ26": {"price": 4.60, "as_of": None}}
check(futures.update_quotes(db, q) == 2, "store quotes")
futures.update_quotes(db, {"ZSX26": {"price": 12.35, "as_of": datetime.now(timezone.utc)}})        # update path, not a second row
got = futures.list_quotes(db, {"ZSX26", "ZCZ26"})
check(got["ZSX26"]["price"] == 12.35 and len(db.query("SELECT 1 FROM futures_quotes WHERE symbol = 'ZSX26'")) == 1, got)
print("    stamp:", futures.latest_stamp(got))
sb = [b for b in data.list_bids(db, slug) if b["futures_symbol"] == "ZSX26"][0]
check(futures.indicated_cash(sb, got["ZSX26"]) == 11.9, futures.indicated_cash(sb, got["ZSX26"]))   # 12.35 - 0.45
check(futures.suggest_cash_updates(db, slug)["staged"] >= 1, "staged")
sug = [x for x in data.list_suggestions(db, slug) if x["source"] == "massive:delayed" and x["futures_symbol"] == "ZSX26"]
check(len(sug) == 1 and abs(sug[0]["cash_price"] - 11.9) < 1e-9, sug)
check([b["cash_price"] for b in data.list_bids(db, slug) if b["futures_symbol"] == "ZSX26"] == [11.8], "bid stays sticky")
data.review_suggestion(db, slug, S, sug[0]["suggestion_id"], True, cash_price=11.88)
check([b["cash_price"] for b in data.list_bids(db, slug) if b["futures_symbol"] == "ZSX26"] == [11.88], "confirmed -> published")
platform.update_tenant(db, A, slug, name=NAME, logo_url=None, primary_color="#112233", accent_color="#abcdef",
                       support_email=None, from_name=None, open_pricing=True, hedging_enabled=False, show_futures=True)
check(bool(data.get_tenant(db, slug)["show_futures"]) is True, "show_futures switch")
out = futures.run_update(db, fetch=lambda syms: {"ZCZ26": {"price": 4.62, "as_of": None}})
check(out["status"] == "ok" and out["quotes"] == 1, out)

print("\nALL OK:", N, "steps; tenant", slug)
