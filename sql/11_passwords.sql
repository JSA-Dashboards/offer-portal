-- Passwords for individual sign-in (email code stays for the first time, a forgotten password, and anyone without one).
-- Run in a Snowsight SQL WORKSHEET as ACCOUNTADMIN (paste all, "Run all"). Safe to re-run. Nothing is lost.
-- The app works without it (it falls back to email codes), so run it whenever convenient; passwords switch on within a minute.
USE ROLE ACCOUNTADMIN;
USE WAREHOUSE OFFER_PORTAL;
USE DATABASE OFFER_PORTAL;
USE SCHEMA OFFER_PORTAL;

ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash STRING;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_set_at TIMESTAMP_TZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_logins NUMBER(38,0) DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP_TZ;

-- Check: the four columns exist (expect 4 rows).
SELECT column_name FROM OFFER_PORTAL.INFORMATION_SCHEMA.COLUMNS
WHERE table_schema = 'OFFER_PORTAL' AND table_name = 'USERS'
  AND column_name IN ('PASSWORD_HASH', 'PASSWORD_SET_AT', 'FAILED_LOGINS', 'LOCKED_UNTIL')
ORDER BY column_name;
