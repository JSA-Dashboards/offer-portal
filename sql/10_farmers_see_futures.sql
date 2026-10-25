-- Farmers see the delayed futures quotes by default (the tenant switch stays, so an elevator can still turn it off).
-- Run in a Snowsight SQL WORKSHEET as ACCOUNTADMIN (paste all, "Run all"). Safe to re-run.
USE ROLE ACCOUNTADMIN;
USE WAREHOUSE OFFER_PORTAL;
USE DATABASE OFFER_PORTAL;
USE SCHEMA OFFER_PORTAL;

ALTER TABLE tenants ALTER COLUMN show_futures SET DEFAULT TRUE;
UPDATE tenants SET show_futures = TRUE WHERE show_futures IS NULL OR show_futures = FALSE;

SELECT tenant_id, name, show_futures FROM tenants ORDER BY tenant_id;
