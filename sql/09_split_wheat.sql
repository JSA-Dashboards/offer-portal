-- The first build had one "Wheat" (CBOT soft red, ZW). Wheat is now three markets: SRW Wheat (CBOT ZW),
-- HRW Wheat (KC KE) and HRS Wheat (MIAX MWE). Existing "Wheat" rows were all CBOT ZW, so they become "SRW Wheat".
-- Run in a Snowsight SQL WORKSHEET as ACCOUNTADMIN (paste all, "Run all"). Safe to re-run. Nothing else changes.
USE ROLE ACCOUNTADMIN;
USE WAREHOUSE OFFER_PORTAL;
USE DATABASE OFFER_PORTAL;
USE SCHEMA OFFER_PORTAL;

UPDATE bids            SET grain = 'SRW Wheat' WHERE grain = 'Wheat';
UPDATE bid_suggestions SET grain = 'SRW Wheat' WHERE grain = 'Wheat';
UPDATE offers          SET grain = 'SRW Wheat' WHERE grain = 'Wheat';
UPDATE purchases       SET grain = 'SRW Wheat' WHERE grain = 'Wheat';
UPDATE auto_hit_rules  SET grain = 'SRW Wheat' WHERE grain = 'Wheat';

-- Check: expect 0 rows still called plain Wheat in each table.
SELECT 'bids' t, COUNT(*) n FROM bids WHERE grain = 'Wheat'
UNION ALL SELECT 'bid_suggestions', COUNT(*) FROM bid_suggestions WHERE grain = 'Wheat'
UNION ALL SELECT 'offers', COUNT(*) FROM offers WHERE grain = 'Wheat'
UNION ALL SELECT 'purchases', COUNT(*) FROM purchases WHERE grain = 'Wheat'
UNION ALL SELECT 'auto_hit_rules', COUNT(*) FROM auto_hit_rules WHERE grain = 'Wheat';
