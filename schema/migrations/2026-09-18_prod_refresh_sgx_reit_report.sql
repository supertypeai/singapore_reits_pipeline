-- Refresh the sgx_reit_report materialized view after the 2026-09-18 data release.
--
-- The view definition does not change. to_jsonb(p.*) follows the table, so the new
-- properties_location text[] comes out as a JSON array by itself. But the view holds a
-- stored copy, so until it is refreshed it still serves the old values: DPU in cents and
-- properties_location as the old bracketed string.
--
-- Run this AFTER promote_final_to_prod.py --write and AFTER
-- 2026-09-18_prod_properties_location_array.sql.

refresh materialized view public.sgx_reit_report;

-- Use the concurrent form instead if the view must stay readable while it rebuilds.
-- It needs a unique index on the view, and it cannot run inside a transaction.
--
-- create unique index if not exists sgx_reit_report_symbol_idx
--   on public.sgx_reit_report (symbol);
-- refresh materialized view concurrently public.sgx_reit_report;

-- Check after: DPU in dollars, properties_location a JSON array, gain at the stake.
--
-- select historical_performance -> '2025-12-31' ->> 'distribution_per_unit' as dpu,
--        historical_performance -> '2025-12-31' -> 'properties_location'     as loc
--   from sgx_reit_report where symbol = 'A17U.SI';
--   -> 0.15005 | ["Singapore", "Australia", "United States", "United Kingdom"]
--
-- select jsonb_path_query(historical_property_transaction, '$.*[*] ? (@.property_name like_regex "ION")')
--   from sgx_reit_report where symbol = 'C38U.SI';
--   -> gain_loss_pct about -0.0015
