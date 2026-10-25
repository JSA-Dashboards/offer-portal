-- Acknowledge/cancel workflow columns, email outbox, staff purchases log, notification opt-outs, price-history pruning.
-- Run in a Snowsight SQL WORKSHEET (paste all, "Run all"), as ACCOUNTADMIN. Safe to re-run.
-- Applies to the throwaway test copy first, then the live schema.
USE ROLE ACCOUNTADMIN;
USE WAREHOUSE OFFER_PORTAL;
USE DATABASE OFFER_PORTAL;

-- Test copy
USE SCHEMA OFFER_PORTAL.OFFER_PORTAL_TEST;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMP_TZ;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS acknowledged_by NUMBER(38,0);
ALTER TABLE offers ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP_TZ;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS cancelled_by NUMBER(38,0);
ALTER TABLE offers ADD COLUMN IF NOT EXISTS cancel_reason STRING;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS hedge_adjust_needed BOOLEAN DEFAULT FALSE;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS hedge_adjusted_at TIMESTAMP_TZ;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS hedge_adjusted_by NUMBER(38,0);
UPDATE offers SET hedge_adjust_needed = FALSE WHERE hedge_adjust_needed IS NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_email BOOLEAN DEFAULT TRUE;
UPDATE users SET notify_email = TRUE WHERE notify_email IS NULL;
ALTER TABLE farmers ADD COLUMN IF NOT EXISTS notify_email BOOLEAN DEFAULT TRUE;
UPDATE farmers SET notify_email = TRUE WHERE notify_email IS NULL;
CREATE TABLE IF NOT EXISTS notifications (
    notification_id NUMBER(38,0) NOT NULL, tenant_id STRING NOT NULL, kind STRING NOT NULL, to_email STRING NOT NULL,
    to_name STRING, user_id NUMBER(38,0), offer_id NUMBER(38,0), dedupe_key STRING, subject STRING NOT NULL,
    body_html STRING NOT NULL, status STRING NOT NULL DEFAULT 'queued', attempts NUMBER(38,0) NOT NULL DEFAULT 0,
    error STRING, created_at TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(), sent_at TIMESTAMP_TZ, PRIMARY KEY (notification_id));
CREATE TABLE IF NOT EXISTS purchases (
    purchase_id NUMBER(38,0) NOT NULL, tenant_id STRING NOT NULL, purchase_date DATE NOT NULL, staff_user_id NUMBER(38,0) NOT NULL,
    farmer_id NUMBER(38,0), counterparty STRING NOT NULL, grain STRING NOT NULL, bushels NUMBER(38,0) NOT NULL, price FLOAT,
    basis_cents NUMBER(38,0), futures_symbol STRING, location_id NUMBER(38,0), delivery_location STRING, delivery_period STRING,
    contract_ref STRING, note STRING, voided_at TIMESTAMP_TZ, created_at TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (purchase_id));
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA OFFER_PORTAL.OFFER_PORTAL_TEST TO ROLE OFFER_PORTAL_ROLE;
GRANT DELETE ON TABLE OFFER_PORTAL.OFFER_PORTAL_TEST.bids TO ROLE OFFER_PORTAL_ROLE;
GRANT DELETE ON TABLE OFFER_PORTAL.OFFER_PORTAL_TEST.bid_suggestions TO ROLE OFFER_PORTAL_ROLE;

-- Live schema
USE SCHEMA OFFER_PORTAL.OFFER_PORTAL;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMP_TZ;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS acknowledged_by NUMBER(38,0);
ALTER TABLE offers ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP_TZ;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS cancelled_by NUMBER(38,0);
ALTER TABLE offers ADD COLUMN IF NOT EXISTS cancel_reason STRING;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS hedge_adjust_needed BOOLEAN DEFAULT FALSE;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS hedge_adjusted_at TIMESTAMP_TZ;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS hedge_adjusted_by NUMBER(38,0);
UPDATE offers SET hedge_adjust_needed = FALSE WHERE hedge_adjust_needed IS NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_email BOOLEAN DEFAULT TRUE;
UPDATE users SET notify_email = TRUE WHERE notify_email IS NULL;
ALTER TABLE farmers ADD COLUMN IF NOT EXISTS notify_email BOOLEAN DEFAULT TRUE;
UPDATE farmers SET notify_email = TRUE WHERE notify_email IS NULL;
CREATE TABLE IF NOT EXISTS notifications (
    notification_id NUMBER(38,0) NOT NULL, tenant_id STRING NOT NULL, kind STRING NOT NULL, to_email STRING NOT NULL,
    to_name STRING, user_id NUMBER(38,0), offer_id NUMBER(38,0), dedupe_key STRING, subject STRING NOT NULL,
    body_html STRING NOT NULL, status STRING NOT NULL DEFAULT 'queued', attempts NUMBER(38,0) NOT NULL DEFAULT 0,
    error STRING, created_at TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(), sent_at TIMESTAMP_TZ, PRIMARY KEY (notification_id));
CREATE TABLE IF NOT EXISTS purchases (
    purchase_id NUMBER(38,0) NOT NULL, tenant_id STRING NOT NULL, purchase_date DATE NOT NULL, staff_user_id NUMBER(38,0) NOT NULL,
    farmer_id NUMBER(38,0), counterparty STRING NOT NULL, grain STRING NOT NULL, bushels NUMBER(38,0) NOT NULL, price FLOAT,
    basis_cents NUMBER(38,0), futures_symbol STRING, location_id NUMBER(38,0), delivery_location STRING, delivery_period STRING,
    contract_ref STRING, note STRING, voided_at TIMESTAMP_TZ, created_at TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (purchase_id));
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA OFFER_PORTAL.OFFER_PORTAL TO ROLE OFFER_PORTAL_ROLE;
GRANT DELETE ON TABLE OFFER_PORTAL.OFFER_PORTAL.bids TO ROLE OFFER_PORTAL_ROLE;
GRANT DELETE ON TABLE OFFER_PORTAL.OFFER_PORTAL.bid_suggestions TO ROLE OFFER_PORTAL_ROLE;

-- Checks: expect 2 rows for each (test + live)
SELECT table_schema, table_name FROM OFFER_PORTAL.INFORMATION_SCHEMA.TABLES WHERE table_name IN ('NOTIFICATIONS', 'PURCHASES') ORDER BY 1, 2;
SELECT table_schema, table_name, column_name FROM OFFER_PORTAL.INFORMATION_SCHEMA.COLUMNS
 WHERE (table_name = 'OFFERS' AND column_name IN ('ACKNOWLEDGED_AT', 'CANCELLED_AT', 'HEDGE_ADJUST_NEEDED'))
    OR (table_name IN ('USERS', 'FARMERS') AND column_name = 'NOTIFY_EMAIL') ORDER BY 1, 2, 3;
