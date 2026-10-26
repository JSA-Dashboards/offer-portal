-- Text messages: who has opted in, which elevators text, and the outbox. Texting stays OFF everywhere until
-- SMS_ENABLED=1 and the Twilio settings exist AND an elevator is switched on in Platform, so this is safe to run any time.
-- Run in a Snowsight SQL WORKSHEET as ACCOUNTADMIN (paste all, "Run all"). Safe to re-run. Nothing is lost.
USE ROLE ACCOUNTADMIN;
USE WAREHOUSE OFFER_PORTAL;
USE DATABASE OFFER_PORTAL;
USE SCHEMA OFFER_PORTAL;

ALTER TABLE users   ADD COLUMN IF NOT EXISTS phone STRING;                    -- E.164, e.g. +15551234567
ALTER TABLE users   ADD COLUMN IF NOT EXISTS sms_opt_in_at TIMESTAMP_TZ;      -- NULL = not opted in (consent is the person's own)
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sms_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sms_from STRING;                 -- the elevator's own number; NULL = JSA's shared one

CREATE TABLE IF NOT EXISTS sms_queue (
    sms_id        NUMBER(38,0) NOT NULL, tenant_id STRING NOT NULL, kind STRING NOT NULL, to_phone STRING NOT NULL,
    to_name       STRING, user_id NUMBER(38,0), offer_id NUMBER(38,0), dedupe_key STRING, body STRING NOT NULL,
    from_number   STRING, status STRING NOT NULL DEFAULT 'queued', attempts NUMBER(38,0) NOT NULL DEFAULT 0,
    error         STRING, provider_id STRING, created_at TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(), sent_at TIMESTAMP_TZ,
    PRIMARY KEY (sms_id));

-- Check: expect 4 rows for the columns, then 1 row for the table.
SELECT table_name, column_name FROM OFFER_PORTAL.INFORMATION_SCHEMA.COLUMNS
WHERE table_schema = 'OFFER_PORTAL' AND ((table_name = 'USERS' AND column_name IN ('PHONE', 'SMS_OPT_IN_AT'))
   OR (table_name = 'TENANTS' AND column_name IN ('SMS_ENABLED', 'SMS_FROM'))) ORDER BY 1, 2;
SELECT table_name FROM OFFER_PORTAL.INFORMATION_SCHEMA.TABLES WHERE table_schema = 'OFFER_PORTAL' AND table_name = 'SMS_QUEUE';
