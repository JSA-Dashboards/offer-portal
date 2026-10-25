-- Delivery date ranges on bids and scraped suggestions (offers expire the day after the last delivery day).
-- Run in a Snowsight SQL WORKSHEET as ACCOUNTADMIN (paste all, "Run all"). Safe to re-run.
-- Applies to the test copy first, then the live schema.
USE ROLE ACCOUNTADMIN;
USE WAREHOUSE OFFER_PORTAL;
USE DATABASE OFFER_PORTAL;

-- Test copy
USE SCHEMA OFFER_PORTAL.OFFER_PORTAL_TEST;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS delivery_start DATE;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS delivery_end DATE;
ALTER TABLE bid_suggestions ADD COLUMN IF NOT EXISTS delivery_start DATE;
ALTER TABLE bid_suggestions ADD COLUMN IF NOT EXISTS delivery_end DATE;
-- Existing bids: the month label is explicit, so the whole month is the range. (Existing suggestions are left NULL on purpose:
-- an admin enters a range when approving them.)
UPDATE bids SET delivery_start = TO_DATE(delivery_month || '-01'), delivery_end = LAST_DAY(TO_DATE(delivery_month || '-01'))
 WHERE delivery_start IS NULL AND REGEXP_LIKE(delivery_month, '^[0-9]{4}-[0-9]{2}$');

-- Live schema
USE SCHEMA OFFER_PORTAL.OFFER_PORTAL;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS delivery_start DATE;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS delivery_end DATE;
ALTER TABLE bid_suggestions ADD COLUMN IF NOT EXISTS delivery_start DATE;
ALTER TABLE bid_suggestions ADD COLUMN IF NOT EXISTS delivery_end DATE;
-- Existing bids: the month label is explicit, so the whole month is the range. (Existing suggestions are left NULL on purpose:
-- an admin enters a range when approving them.)
UPDATE bids SET delivery_start = TO_DATE(delivery_month || '-01'), delivery_end = LAST_DAY(TO_DATE(delivery_month || '-01'))
 WHERE delivery_start IS NULL AND REGEXP_LIKE(delivery_month, '^[0-9]{4}-[0-9]{2}$');

-- Checks: expect delivery_start and delivery_end on BIDS and BID_SUGGESTIONS in both schemas (8 rows)
SELECT table_schema, table_name, column_name FROM OFFER_PORTAL.INFORMATION_SCHEMA.COLUMNS
 WHERE table_name IN ('BIDS', 'BID_SUGGESTIONS') AND column_name IN ('DELIVERY_START', 'DELIVERY_END') ORDER BY 1, 2, 3;
