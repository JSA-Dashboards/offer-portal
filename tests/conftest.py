import sqlite3
import pytest
from portal.db import Db

# Test-only sqlite mirror of the columns data.py touches (production is Snowflake).
DDL = """
CREATE TABLE login_codes (email TEXT, code_hash TEXT, expires_at TEXT, used INT DEFAULT 0, attempts INT DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE scrape_sources (source_id INTEGER PRIMARY KEY, tenant_id TEXT, kind TEXT, url TEXT,
  location_name TEXT, active INT DEFAULT 1, last_run_at TEXT, last_status TEXT);
CREATE TABLE farmers (farmer_id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT, client_id TEXT, entity_name TEXT,
  individual_name TEXT, phone TEXT, email TEXT,
  user_id INT, active INT DEFAULT 1, created_by INT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE invites (token_hash TEXT PRIMARY KEY, tenant_id TEXT, email TEXT, role TEXT DEFAULT 'farmer',
  invited_by INT, farmer_id INT, expires_at TEXT, used INT DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE tenants (tenant_id TEXT PRIMARY KEY, name TEXT, status TEXT DEFAULT 'active', logo_url TEXT,
  primary_color TEXT, accent_color TEXT, support_email TEXT, from_name TEXT,
  hedging_enabled INT DEFAULT 0, open_pricing INT DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE locations (location_id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT, state TEXT, active INT DEFAULT 1);
CREATE TABLE users (user_id INTEGER PRIMARY KEY, tenant_id TEXT, email TEXT, name TEXT, role TEXT, active INT DEFAULT 1, invited_by INT);
CREATE TABLE bid_suggestions (suggestion_id INTEGER PRIMARY KEY, tenant_id TEXT, location_id INT, grain TEXT,
  delivery_month TEXT, futures_symbol TEXT, basis_cents INT, cash_price REAL, source TEXT,
  scraped_at TEXT DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'suggested', reviewed_by INT, reviewed_at TEXT);
CREATE TABLE bids (bid_id INTEGER PRIMARY KEY, tenant_id TEXT, location_id INT, grain TEXT, delivery_month TEXT,
  futures_symbol TEXT, basis_cents INT, cash_price REAL, bushel_cap INT, effective_date TEXT, expires_at TEXT,
  source_suggestion_id INT, published_by INT, published_at TEXT DEFAULT CURRENT_TIMESTAMP, active INT DEFAULT 1);
CREATE TABLE offers (offer_id INTEGER PRIMARY KEY, tenant_id TEXT, farmer_id INT, farmer_user_id INT, placed_by INT, location_id INT, bid_id INT,
  grain TEXT, contract_type TEXT, price_type TEXT, bushels INT, price REAL, basis_cents INT, futures_symbol TEXT,
  delivery_start TEXT, delivery_end TEXT, note TEXT, contract_ref TEXT, basis_ref_cents INT, implied_futures REAL,
  completed_at TEXT, completed_by INT, hedged_at TEXT, hedged_by INT, bid_snapshot TEXT, status TEXT, expires_at TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE offer_events (event_id INTEGER PRIMARY KEY, tenant_id TEXT, offer_id INT, actor_user_id INT,
  event_type TEXT, detail TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE confirmations (confirmation_id INTEGER PRIMARY KEY, tenant_id TEXT, offer_id INT, terms TEXT,
  accepted_by INT, auto_accepted INT DEFAULT 0, hedge_ticket_id INT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
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
