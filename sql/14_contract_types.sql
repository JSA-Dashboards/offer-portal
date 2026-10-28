-- Contract types are now Cash, Futures Only (HTA) and Basis Only. The first build called a basis offer "HTA"; those rows are
-- Basis Only. (The app already reads an old 'HTA' row as Basis Only, so this just tidies the stored value.)
-- Run in a Snowsight SQL WORKSHEET as ACCOUNTADMIN (paste all, "Run all"). Safe to re-run. Nothing is lost.
USE ROLE ACCOUNTADMIN;
USE WAREHOUSE OFFER_PORTAL;
USE DATABASE OFFER_PORTAL;
USE SCHEMA OFFER_PORTAL;

UPDATE offers SET contract_type = 'BASIS' WHERE contract_type = 'HTA';

-- Check: expect 0 rows still called HTA.
SELECT COUNT(*) AS offers_still_hta FROM offers WHERE contract_type = 'HTA';
