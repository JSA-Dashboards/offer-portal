-- Offers that staff acknowledge become that day's purchases (and so appear in the recap). This links a purchase to its offer
-- (one per offer). Until it is run, acknowledging an offer simply doesn't create a purchase; nothing else changes.
-- Run in a Snowsight SQL WORKSHEET as ACCOUNTADMIN (paste all, "Run all"). Safe to re-run. Nothing is lost.
USE ROLE ACCOUNTADMIN;
USE WAREHOUSE OFFER_PORTAL;
USE DATABASE OFFER_PORTAL;
USE SCHEMA OFFER_PORTAL;

ALTER TABLE purchases ADD COLUMN IF NOT EXISTS offer_id NUMBER(38,0);

-- Check: expect 1 row.
SELECT column_name FROM OFFER_PORTAL.INFORMATION_SCHEMA.COLUMNS
WHERE table_schema = 'OFFER_PORTAL' AND table_name = 'PURCHASES' AND column_name = 'OFFER_ID';
