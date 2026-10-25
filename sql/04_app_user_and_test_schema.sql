-- Run in a Snowsight SQL worksheet as ACCOUNTADMIN (paste all, "Run all"). Safe to re-run.
-- Part 1: the Streamlit app's own service user (key-pair only; its public key is below, the private half
--         is on the Droplet and goes into the Streamlit Cloud secrets). Same role as the scraper.
-- Part 2: a throwaway COPY of the schema so the app's data layer can be tested end to end on real
--         Snowflake without touching live data. Drop it afterwards (last statement).
USE ROLE ACCOUNTADMIN;
USE WAREHOUSE COMPUTE_WH;
USE DATABASE OFFER_PORTAL;

CREATE USER IF NOT EXISTS OFFER_PORTAL_APP_SVC
    TYPE = SERVICE
    DEFAULT_ROLE = OFFER_PORTAL_ROLE
    DEFAULT_WAREHOUSE = COMPUTE_WH
    RSA_PUBLIC_KEY = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAxvZMSTUTiAplqM2YATAYhimezIDr67Y9hqM9ujdtfFlbBIhYdkIfzUV1lhQYQrH13vOtZZ4OGYsvZzxP8HlKg+T1lvh7YMOiUveyUQ21FrUoIYLoPeJE7ZB4+0BmsHVcrWAS6h/HKQOCsqTliFMefeR9OyT4DPy/thjzHqmB40ynqHkQl+Y29uukcYT+R1pdX6kGUgWyrKjVQ6xwv+VCljZ1x1Dl/x8p41FrwA1HMuJ1aQdeuJvC1aXDA75MuqYdKgwtjlstVj2S8hQfKkjLgRHFL23GghIzbvzZ2qpyaeQM0KJFG8DSi5qOAYqIc2uCX36oaJlXwM4nbDLYog5dJQIDAQAB';
GRANT ROLE OFFER_PORTAL_ROLE TO USER OFFER_PORTAL_APP_SVC;

-- Throwaway copy of the schema (zero-copy clone) for the integration test.
CREATE OR REPLACE SCHEMA OFFER_PORTAL.OFFER_PORTAL_TEST CLONE OFFER_PORTAL.OFFER_PORTAL;
GRANT USAGE ON SCHEMA OFFER_PORTAL.OFFER_PORTAL_TEST TO ROLE OFFER_PORTAL_ROLE;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA OFFER_PORTAL.OFFER_PORTAL_TEST TO ROLE OFFER_PORTAL_ROLE;

-- Checks (expect: the app user, and the test schema with 15 tables)
SHOW USERS LIKE 'OFFER_PORTAL%';
SELECT COUNT(*) AS test_schema_tables FROM OFFER_PORTAL.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'OFFER_PORTAL_TEST';

-- LATER, once the integration test has passed, run this to remove the copy:
-- DROP SCHEMA IF EXISTS OFFER_PORTAL.OFFER_PORTAL_TEST;
