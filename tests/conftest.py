import sqlite3
import pytest
from portal.db import Db

# Test-only sqlite mirror of the columns data.py touches (production is Snowflake).
DDL = """
CREATE TABLE login_codes (email TEXT, code_hash TEXT, expires_at TEXT, used INT DEFAULT 0, attempts INT DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE auto_hit_rules (rule_id INTEGER PRIMARY KEY, tenant_id TEXT, location_id INT,
  grain TEXT, contract_type TEXT, max_bushels INT, active INT DEFAULT 1, created_by INT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE futures_quotes (symbol TEXT PRIMARY KEY, price REAL, as_of TEXT,
  fetched_at TEXT DEFAULT CURRENT_TIMESTAMP, source TEXT DEFAULT 'massive', delayed INT DEFAULT 1);
CREATE TABLE notifications (notification_id INTEGER PRIMARY KEY, tenant_id TEXT, kind TEXT, to_email TEXT, to_name TEXT,
  user_id INT, offer_id INT, dedupe_key TEXT, subject TEXT, body_html TEXT, status TEXT DEFAULT 'queued', attempts INT DEFAULT 0,
  error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, sent_at TEXT);
CREATE TABLE purchases (purchase_id INTEGER PRIMARY KEY, tenant_id TEXT, purchase_date TEXT, staff_user_id INT, farmer_id INT,
  counterparty TEXT, grain TEXT, bushels INT, price REAL, basis_cents INT, futures_symbol TEXT, location_id INT,
  delivery_location TEXT, delivery_period TEXT, contract_ref TEXT, note TEXT, voided_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE scrape_sources (source_id INTEGER PRIMARY KEY, tenant_id TEXT, kind TEXT, url TEXT,
  location_name TEXT, active INT DEFAULT 1, last_run_at TEXT, last_status TEXT);
CREATE TABLE farmers (farmer_id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT, client_id TEXT, entity_name TEXT,
  individual_name TEXT, phone TEXT, email TEXT,
  user_id INT, active INT DEFAULT 1, notify_email INT DEFAULT 1, created_by INT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE invites (token_hash TEXT PRIMARY KEY, tenant_id TEXT, email TEXT, role TEXT DEFAULT 'farmer',
  invited_by INT, farmer_id INT, expires_at TEXT, used INT DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY, name TEXT, status TEXT DEFAULT 'active', logo_url TEXT,
  primary_color TEXT, accent_color TEXT, support_email TEXT, from_name TEXT,
  hedging_enabled INT DEFAULT 0, open_pricing INT DEFAULT 1, show_futures INT DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE locations (location_id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT, state TEXT, active INT DEFAULT 1);
CREATE TABLE users (user_id INTEGER PRIMARY KEY, tenant_id TEXT, email TEXT, name TEXT, role TEXT, active INT DEFAULT 1, invited_by INT, notify_email INT DEFAULT 1,
  password_hash TEXT, password_set_at TEXT, failed_logins INT DEFAULT 0, locked_until TEXT);
CREATE TABLE bid_suggestions (suggestion_id INTEGER PRIMARY KEY, tenant_id TEXT, location_id INT, grain TEXT,
  delivery_month TEXT, delivery_start TEXT, delivery_end TEXT, futures_symbol TEXT, basis_cents INT, cash_price REAL, source TEXT,
  scraped_at TEXT DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'suggested', reviewed_by INT, reviewed_at TEXT);
CREATE TABLE bids (bid_id INTEGER PRIMARY KEY, tenant_id TEXT, location_id INT, grain TEXT, delivery_month TEXT,
  delivery_start TEXT, delivery_end TEXT,
  futures_symbol TEXT, basis_cents INT, cash_price REAL, bushel_cap INT, effective_date TEXT, expires_at TEXT,
  source_suggestion_id INT, published_by INT, published_at TEXT DEFAULT CURRENT_TIMESTAMP, active INT DEFAULT 1);
CREATE TABLE offers (offer_id INTEGER PRIMARY KEY, tenant_id TEXT, farmer_id INT, farmer_user_id INT, placed_by INT, location_id INT, bid_id INT,
  grain TEXT, contract_type TEXT, price_type TEXT, bushels INT, price REAL, basis_cents INT, futures_symbol TEXT,
  delivery_start TEXT, delivery_end TEXT, note TEXT, contract_ref TEXT, basis_ref_cents INT, implied_futures REAL,
  hit_at TEXT, hit_by INT, auto_hit INT DEFAULT 0, auto_hit_rule_id INT,
  acknowledged_at TEXT, acknowledged_by INT, completed_at TEXT, completed_by INT, cancelled_at TEXT, cancelled_by INT,
  cancel_reason TEXT, hedge_adjust_needed INT DEFAULT 0, hedge_adjusted_at TEXT, hedge_adjusted_by INT,
  hedged_at TEXT, hedged_by INT, bid_snapshot TEXT, status TEXT, expires_at TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE offer_events (event_id INTEGER PRIMARY KEY, tenant_id TEXT, offer_id INT, actor_user_id INT,
  event_type TEXT, detail TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE confirmations (confirmation_id INTEGER PRIMARY KEY, tenant_id TEXT, offer_id INT, terms TEXT,
  hit_by INT, auto_hit INT DEFAULT 0, hedge_ticket_id INT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE audit_log (audit_id INTEGER PRIMARY KEY, tenant_id TEXT, actor_user_id INT, action TEXT,
  detail TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE usage_events (usage_id INTEGER PRIMARY KEY, tenant_id TEXT, event_type TEXT, quantity INT DEFAULT 1,
  ref_id INT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    d = Db(conn, "sqlite")
    for t in ("rcm", "dorans"):
        d.execute("INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)", (t, t.upper()))
    d.execute("INSERT INTO tenants (tenant_id, name, open_pricing) VALUES ('closed', 'Closed', 0)")
    rows = [(1, "rcm", "farmer@a.com", "farmer"), (2, "rcm", "admin@rcm.com", "elevator_admin"),
            (3, "dorans", "farmer@b.com", "farmer"), (4, "dorans", "admin@dorans.com", "elevator_admin"),
            (5, None, "kpostin@jpsi.com", "platform_admin")]
    for uid, t, e, r in rows:
        d.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (%s, %s, %s, %s)", (uid, t, e, r))
    return d


def user(db, uid):
    return db.query("SELECT * FROM users WHERE user_id = %s", (uid,))[0]


# ── AppTest helpers: the portal's pages are tab pages (only the open tab runs) ──────────────
_SUBTABS = ("Tenants", "Usage Report", "JSA Admins")


def pages(at):
    """The main page tabs the signed-in user sees (not the offer-stage or platform sub-tabs)."""
    return [t.label for t in at.tabs if " (" not in t.label and t.label not in _SUBTABS]


def goto(at, page):
    """Open a page: select its tab (the tab group's key holds the open tab's label) and rerun."""
    key = next(k for k in at.session_state.filtered_state if k.startswith("main_tabs"))
    at.session_state["_tab_" + key] = page
    del at.session_state[key]
    return at.run()


def stage(at, name):
    """Open an offers stage tab ('Hit', 'Closed', ...): tab labels carry live counts, so match on the name."""
    assert any(t.label.startswith(name + " (") for t in at.tabs), name
    at.session_state["offers_stage"] = name                 # the page remembers the open stage by name (the counts change)
    del at.session_state["offers_tabs"]
    return at.run()


def stages(at):
    return [t.label for t in at.tabs if " (" in t.label]
