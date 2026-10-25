-- Service user + role for the Droplet scraper job (key-pair auth only; the account enforces MFA on
-- passwords). Run as ACCOUNTADMIN AFTER 00_database.sql and 01_schema.sql.
--
-- 1. On the Droplet the key is generated for you (deploy/DROPLET.md step 3) and its PUBLIC half is
--    printed. Paste that single line (no BEGIN/END header, no line breaks) below.
-- 2. The Streamlit app (Cloud / Streamlit-in-Snowflake) needs its own user later; this one is the
--    scraper's and gets the same table access, which is all the app does today.

CREATE ROLE IF NOT EXISTS OFFER_PORTAL_ROLE;
GRANT USAGE ON DATABASE OFFER_PORTAL TO ROLE OFFER_PORTAL_ROLE;
GRANT USAGE ON SCHEMA OFFER_PORTAL.OFFER_PORTAL TO ROLE OFFER_PORTAL_ROLE;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA OFFER_PORTAL.OFFER_PORTAL TO ROLE OFFER_PORTAL_ROLE;
GRANT SELECT, INSERT, UPDATE ON FUTURE TABLES IN SCHEMA OFFER_PORTAL.OFFER_PORTAL TO ROLE OFFER_PORTAL_ROLE;
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE OFFER_PORTAL_ROLE;

CREATE USER IF NOT EXISTS OFFER_PORTAL_SVC
    TYPE = SERVICE
    DEFAULT_ROLE = OFFER_PORTAL_ROLE
    DEFAULT_WAREHOUSE = COMPUTE_WH
    RSA_PUBLIC_KEY = '<PASTE THE PUBLIC KEY LINE FROM THE DROPLET HERE>';
GRANT ROLE OFFER_PORTAL_ROLE TO USER OFFER_PORTAL_SVC;
