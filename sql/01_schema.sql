-- ============================================================================
-- Offer Portal — Snowflake schema (white-label, multi-tenant)
-- ----------------------------------------------------------------------------
-- Run once in the JSA Analytics account:
--     USE DATABASE <JSA_ANALYTICS_DB>;
-- then run this whole file. Creates schema OFFER_PORTAL.
--
-- Tenancy: a tenant = one elevator COMPANY. Every tenant-owned table carries
-- tenant_id and every app query filters on it (see portal/data.py). Locations
-- sit under the tenant. Column names are UNQUOTED (fold to UPPERCASE); the data
-- layer lowercases result keys, same as the basis tracker.
-- PRIMARY KEY / UNIQUE are informational in Snowflake (not enforced).
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS OFFER_PORTAL;
USE SCHEMA OFFER_PORTAL;

-- ── Tenants (one per elevator company) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id      STRING       NOT NULL,          -- slug, e.g. 'rcm'
    name           STRING       NOT NULL,
    status         STRING       NOT NULL DEFAULT 'active',   -- active | suspended
    logo_url       STRING,
    primary_color  STRING       DEFAULT '#32373c',
    accent_color   STRING       DEFAULT '#0693e3',
    support_email  STRING,
    from_name      STRING,                          -- email "from" display name
    hedging_enabled BOOLEAN     NOT NULL DEFAULT FALSE,   -- phase 2 flag
    open_pricing   BOOLEAN      NOT NULL DEFAULT TRUE,    -- farmers may propose any price
    sms_enabled    BOOLEAN      DEFAULT FALSE,            -- text messages on for this elevator (also needs SMS_ENABLED + the Twilio settings)
    sms_from       STRING,                          -- this elevator's own sending number; NULL = JSA's shared number
    show_futures   BOOLEAN      DEFAULT TRUE,             -- farmers see delayed futures quotes unless an elevator turns it off (staff always can)
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (tenant_id)
);

CREATE TABLE IF NOT EXISTS locations (
    location_id    NUMBER(38,0) IDENTITY(1,1),
    tenant_id      STRING       NOT NULL,
    name           STRING       NOT NULL,
    state          STRING,
    active         BOOLEAN      NOT NULL DEFAULT TRUE,
    PRIMARY KEY (location_id),
    UNIQUE (tenant_id, name)
);

-- ── Users (platform admin has tenant_id NULL) ───────────────────────────────
-- role: platform_admin | elevator_admin | elevator_staff | farmer
CREATE TABLE IF NOT EXISTS users (
    user_id        NUMBER(38,0) IDENTITY(1,1),
    tenant_id      STRING,
    email          STRING       NOT NULL,
    name           STRING,
    role           STRING       NOT NULL,
    active         BOOLEAN      NOT NULL DEFAULT TRUE,
    notify_email   BOOLEAN      DEFAULT TRUE,                -- offer / recap emails (NULL counts as on)
    invited_by     NUMBER(38,0),
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    phone          STRING,                                      -- E.164, for text messages
    sms_opt_in_at  TIMESTAMP_TZ,                                -- NULL = has not opted in to texts
    password_hash  STRING,                                      -- pbkdf2_sha256$rounds$salt$hash; NULL = no password yet (email code only)
    password_set_at TIMESTAMP_TZ,
    failed_logins  NUMBER(38,0) DEFAULT 0,                      -- wrong passwords in a row
    locked_until   TIMESTAMP_TZ,                                -- password sign-in paused until then
    PRIMARY KEY (user_id),
    UNIQUE (email)                                  -- one tenant per login
);

-- Emailed one-time sign-in codes and single-use farmer invites.
CREATE TABLE IF NOT EXISTS login_codes (
    email          STRING       NOT NULL,
    code_hash      STRING       NOT NULL,
    expires_at     TIMESTAMP_TZ NOT NULL,
    used           BOOLEAN      NOT NULL DEFAULT FALSE,
    attempts       NUMBER(38,0) NOT NULL DEFAULT 0,
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
);

-- Farmer customer records. A farmer need not have a login: staff create them to place offers
-- on a farmer's behalf (phone / in person). user_id is set once the farmer accepts an invite.
CREATE TABLE IF NOT EXISTS farmers (
    farmer_id      NUMBER(38,0) IDENTITY(1,1),
    tenant_id      STRING       NOT NULL,
    name           STRING       NOT NULL,           -- display: 'Entity (Individual)' or whichever exists
    client_id      STRING,                          -- the elevator's own account number for the entity
    entity_name    STRING,                          -- farm / partnership / LLC
    individual_name STRING,
    phone          STRING,
    email          STRING,
    user_id        NUMBER(38,0),                    -- linked portal login, if any
    active         BOOLEAN      NOT NULL DEFAULT TRUE,
    notify_email   BOOLEAN      DEFAULT TRUE,                -- email this farmer about their offers (uses `email`)
    created_by     NUMBER(38,0),
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (farmer_id)
);

CREATE TABLE IF NOT EXISTS invites (
    token_hash     STRING       NOT NULL,
    tenant_id      STRING       NOT NULL,
    email          STRING       NOT NULL,
    role           STRING       NOT NULL DEFAULT 'farmer',
    invited_by     NUMBER(38,0),
    farmer_id      NUMBER(38,0),                    -- link an existing farmer record on accept
    expires_at     TIMESTAMP_TZ NOT NULL,
    used           BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (token_hash)
);

-- ── Scraped bid staging: scraper output waits here until the admin confirms ─
-- status: suggested | approved | discarded | superseded (a newer scrape replaced it)
CREATE TABLE IF NOT EXISTS bid_suggestions (
    suggestion_id  NUMBER(38,0) IDENTITY(1,1),
    tenant_id      STRING       NOT NULL,
    location_id    NUMBER(38,0),
    grain          STRING       NOT NULL,
    delivery_month STRING       NOT NULL,           -- e.g. '2026-12'
    delivery_start DATE,                            -- the delivery date range; NULL = the scrape did not give a clear one
    delivery_end   DATE,                            --   (an admin must enter one before the suggestion can be approved)
    futures_symbol STRING,
    basis_cents    NUMBER(38,0),
    cash_price     FLOAT,
    source         STRING       NOT NULL,           -- scraper / provider tag
    scraped_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    status         STRING       NOT NULL DEFAULT 'suggested',
    reviewed_by    NUMBER(38,0),
    reviewed_at    TIMESTAMP_TZ,
    PRIMARY KEY (suggestion_id)
);

-- Per-tenant scrape config. kind: agricharts (url = site host, e.g. https://www.rcmcoop.com;
-- feed location names are matched to locations.name) | bushelsites (url = bids page;
-- location_name = the one tenant location it feeds).
CREATE TABLE IF NOT EXISTS scrape_sources (
    source_id      NUMBER(38,0) IDENTITY(1,1),
    tenant_id      STRING       NOT NULL,
    kind           STRING       NOT NULL,
    url            STRING       NOT NULL,
    location_name  STRING,
    active         BOOLEAN      NOT NULL DEFAULT TRUE,
    last_run_at    TIMESTAMP_TZ,
    last_status    STRING,
    PRIMARY KEY (source_id)
);

-- ── Published bid board (what farmers see). Admin-owned, always confirmed ───
CREATE TABLE IF NOT EXISTS bids (
    bid_id         NUMBER(38,0) IDENTITY(1,1),
    tenant_id      STRING       NOT NULL,
    location_id    NUMBER(38,0),
    grain          STRING       NOT NULL,
    delivery_month STRING       NOT NULL,
    delivery_start DATE,                            -- first / last day of the delivery period. An offer made against
    delivery_end   DATE,                            -- this bid expires the day after delivery_end passes
    futures_symbol STRING,
    basis_cents    NUMBER(38,0),                    -- cents under/over the futures month
    cash_price     FLOAT,                           -- fixed cash price, when posted
    bushel_cap     NUMBER(38,0),
    effective_date DATE         NOT NULL,
    expires_at     TIMESTAMP_TZ,
    source_suggestion_id NUMBER(38,0),              -- set when approved from staging
    published_by   NUMBER(38,0),
    published_at   TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    active         BOOLEAN      NOT NULL DEFAULT TRUE,
    PRIMARY KEY (bid_id)
);

-- ── Offers ──────────────────────────────────────────────────────────────────
-- contract_type: CASH (futures price + basis) | FUTURES_ONLY (a futures price: hedge-to-arrive) | BASIS (a basis only)
-- price_type:    FIXED | BASIS          (a basis offer needs futures_symbol)
-- status: submitted (outstanding) -> hit -> acknowledged -> completed (archived)
--         or declined | withdrawn | expired (before a hit), or cancelled (after a hit).   No counter-offers.
-- hit          = the market reached the farmer's offer; the elevator now owns the grain and may need to hedge
-- acknowledged = a staff member took it on (who / when are recorded)
-- completed    = contract number entered on an acknowledged offer; the offer moves to the archive
-- cancelled    = amended to cancelled after a hit: admins are told to adjust the hedge until they confirm it
CREATE TABLE IF NOT EXISTS offers (
    offer_id       NUMBER(38,0) IDENTITY(1,1),
    tenant_id      STRING       NOT NULL,
    farmer_id      NUMBER(38,0) NOT NULL,
    farmer_user_id NUMBER(38,0),                    -- NULL until the farmer has a login
    placed_by      NUMBER(38,0),                    -- staff user who entered it for the farmer; NULL = self-placed
    location_id    NUMBER(38,0),
    bid_id         NUMBER(38,0),                    -- NULL = open-priced offer
    grain          STRING       NOT NULL,
    contract_type  STRING       NOT NULL,
    price_type     STRING       NOT NULL,
    bushels        NUMBER(38,0) NOT NULL,
    price          FLOAT,                           -- FIXED: $/bu
    basis_cents    NUMBER(38,0),                    -- BASIS: cents
    futures_symbol STRING,
    delivery_start DATE,
    delivery_end   DATE,
    note           STRING,                          -- free-text note; staff can edit it later
    contract_ref   STRING,                          -- contract reference number (required to complete an offer)
    basis_ref_cents NUMBER(38,0),                   -- flat-price offers: the posted basis the implied futures uses
    implied_futures FLOAT,                          -- flat price minus that basis; re-computed when the basis moves
    hit_at         TIMESTAMP_TZ,
    hit_by         NUMBER(38,0),                    -- NULL when hit automatically
    auto_hit       BOOLEAN      NOT NULL DEFAULT FALSE,
    auto_hit_rule_id NUMBER(38,0),                  -- the rule that fired
    acknowledged_at TIMESTAMP_TZ,
    acknowledged_by NUMBER(38,0),
    completed_at   TIMESTAMP_TZ,
    completed_by   NUMBER(38,0),
    cancelled_at   TIMESTAMP_TZ,
    cancelled_by   NUMBER(38,0),
    cancel_reason  STRING,
    hedge_adjust_needed BOOLEAN DEFAULT FALSE,      -- a cancelled hit offer: admin must adjust the hedge
    hedge_adjusted_at TIMESTAMP_TZ,
    hedge_adjusted_by NUMBER(38,0),
    hedged_at      TIMESTAMP_TZ,                    -- set when staff mark a hit offer as hedged
    hedged_by      NUMBER(38,0),
    bid_snapshot   VARIANT,                         -- posted bid at submission time
    status         STRING       NOT NULL DEFAULT 'submitted',
    expires_at     TIMESTAMP_TZ,
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    updated_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (offer_id)
);

-- Append-only history (submissions, hits, completions, hedges, basis updates, declines, withdrawals, expiries).
CREATE TABLE IF NOT EXISTS offer_events (
    event_id       NUMBER(38,0) IDENTITY(1,1),
    tenant_id      STRING       NOT NULL,
    offer_id       NUMBER(38,0) NOT NULL,
    actor_user_id  NUMBER(38,0),                    -- NULL = system (expiry job, auto-hit)
    event_type     STRING       NOT NULL,           -- submitted | hit | acknowledged | completed | cancelled | hedged | hedge_adjusted | basis_updated | declined | withdrawn | expired | details_updated
    detail         VARIANT,                         -- terms at that moment
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (event_id)
);

-- Elevator-admin rules: mark an outstanding offer HIT automatically when the posted bid reaches it.
-- No active rules = nothing is ever auto-hit. NULL scope columns mean "any". Offers bigger than
-- max_bushels are left for staff to review. Flat-price offers need the bid to carry a cash price.
CREATE TABLE IF NOT EXISTS auto_hit_rules (
    rule_id        NUMBER(38,0) IDENTITY(1,1),
    tenant_id      STRING       NOT NULL,
    location_id    NUMBER(38,0),
    grain          STRING,
    contract_type  STRING,                          -- CASH | FUTURES_ONLY | BASIS
    max_bushels    NUMBER(38,0),
    active         BOOLEAN      NOT NULL DEFAULT TRUE,
    created_by     NUMBER(38,0),
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (rule_id)
);

-- Terms agreed when an offer was hit. hedge_ticket_id is reserved for phase 2 (QST bridge).
CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id NUMBER(38,0) IDENTITY(1,1),
    tenant_id      STRING       NOT NULL,
    offer_id       NUMBER(38,0) NOT NULL,
    terms          VARIANT      NOT NULL,
    hit_by         NUMBER(38,0),                    -- NULL when hit automatically
    auto_hit       BOOLEAN      NOT NULL DEFAULT FALSE,
    hedge_ticket_id NUMBER(38,0),
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (confirmation_id)
);

-- ── Delayed futures quotes (market data, not tenant-owned): the latest per futures month ───────────────
-- Filled by futures_run.py from the Massive API. A quote never changes a posted bid; it can only stage a
-- suggestion that an elevator admin confirms (see portal/futures.py).
CREATE TABLE IF NOT EXISTS futures_quotes (
    symbol         STRING       NOT NULL,           -- e.g. ZCZ26
    price          FLOAT        NOT NULL,           -- $/bu
    as_of          TIMESTAMP_TZ,                    -- time of the last trade the quote is based on
    fetched_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    source         STRING       NOT NULL DEFAULT 'massive',
    delayed        BOOLEAN      NOT NULL DEFAULT TRUE,
    PRIMARY KEY (symbol)
);

-- ── Email outbox: events queue a row, a sender (the app after an action, or notify_run.py on the droplet) sends it ──
CREATE TABLE IF NOT EXISTS notifications (
    notification_id NUMBER(38,0) NOT NULL,
    tenant_id      STRING       NOT NULL,
    kind           STRING       NOT NULL,           -- offer_submitted | offer_hit | offer_completed | offer_cancelled | ... | daily_recap
    to_email       STRING       NOT NULL,
    to_name        STRING,
    user_id        NUMBER(38,0),
    offer_id       NUMBER(38,0),
    dedupe_key     STRING,                          -- one email per (tenant, key, address)
    subject        STRING       NOT NULL,
    body_html      STRING       NOT NULL,
    status         STRING       NOT NULL DEFAULT 'queued',   -- queued | sent | failed
    attempts       NUMBER(38,0) NOT NULL DEFAULT 0,
    error          STRING,
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    sent_at        TIMESTAMP_TZ,
    PRIMARY KEY (notification_id)
);

-- ── What staff bought today, outside or inside the offer system (feeds the end-of-day recap) ────────────────────────
CREATE TABLE IF NOT EXISTS purchases (
    offer_id       NUMBER(38,0),                   -- set when an acknowledged offer created this purchase (one per offer)
    purchase_id    NUMBER(38,0) NOT NULL,
    tenant_id      STRING       NOT NULL,
    purchase_date  DATE         NOT NULL,           -- Central-time day the buy was logged for
    staff_user_id  NUMBER(38,0) NOT NULL,
    farmer_id      NUMBER(38,0),
    counterparty   STRING       NOT NULL,           -- entity or person name as it should read on the recap
    grain          STRING       NOT NULL,
    bushels        NUMBER(38,0) NOT NULL,
    price          FLOAT,                           -- flat $/bu
    basis_cents    NUMBER(38,0),
    futures_symbol STRING,
    location_id    NUMBER(38,0),
    delivery_location STRING,
    delivery_period STRING,                         -- 'YYYY-MM'
    contract_ref   STRING,
    note           STRING,
    voided_at      TIMESTAMP_TZ,
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (purchase_id)
);

-- ── Admin audit + per-tenant billing usage ──────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id       NUMBER(38,0) IDENTITY(1,1),
    tenant_id      STRING,
    actor_user_id  NUMBER(38,0),
    action         STRING       NOT NULL,
    detail         VARIANT,
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (audit_id)
);

-- One row per billable event: offer_submitted | offer_hit | offer_completed | active_farmer
-- | bid_published | hedge_ticket (phase 2). Roll up by tenant and month.
CREATE TABLE IF NOT EXISTS usage_events (
    usage_id       NUMBER(38,0) IDENTITY(1,1),
    tenant_id      STRING       NOT NULL,
    event_type     STRING       NOT NULL,
    quantity       NUMBER(38,0) NOT NULL DEFAULT 1,
    ref_id         NUMBER(38,0),
    created_at     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (usage_id)
);

-- Text-message outbox (see portal/sms.py, portal/notify.py). Mirrors `notifications`, for phones.
CREATE TABLE IF NOT EXISTS sms_queue (
    sms_id        NUMBER(38,0) NOT NULL, tenant_id STRING NOT NULL, kind STRING NOT NULL, to_phone STRING NOT NULL,
    to_name       STRING, user_id NUMBER(38,0), offer_id NUMBER(38,0), dedupe_key STRING, body STRING NOT NULL,
    from_number   STRING, status STRING NOT NULL DEFAULT 'queued', attempts NUMBER(38,0) NOT NULL DEFAULT 0,
    error         STRING, provider_id STRING, created_at TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(), sent_at TIMESTAMP_TZ,
    PRIMARY KEY (sms_id));
