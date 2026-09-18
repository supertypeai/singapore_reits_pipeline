-- Prod migration for the 2026-09-18 data fixes.
-- The ONLY schema change in that release: sgx_reit_performance.properties_location becomes a
-- real text[]. Everything else in the release is data, promoted by scripts/db/promote_final_to_prod.py.
--
-- Run this AFTER the frontend can read properties_location as either an array or the old
-- bracketed string, and shortly BEFORE the promote.
--
-- 1. Check first: this must return 0 rows. A view on the column blocks the type change.
--
-- select distinct c.relname from pg_depend d
--   join pg_rewrite r on r.oid = d.objid
--   join pg_class c on c.oid = r.ev_class
--   join pg_attribute a on a.attrelid = d.refobjid and a.attnum = d.refobjsubid
--  where d.refobjid = 'sgx_reit_performance'::regclass and a.attname = 'properties_location';
--
-- 1b. Check the format too: this must also return 0 rows. Every non-null value has to be
-- '[A, B]' with ', ' separators, or the conversion silently makes a one-element array.
--
-- select symbol, financial_year, properties_location from sgx_reit_performance
--  where properties_location is not null
--    and (properties_location !~ '^\[.*\]$' or btrim(properties_location, '[]') = '');

begin;

alter table sgx_reit_performance
  alter column properties_location type text[]
  using case
          when properties_location is null then null
          else string_to_array(trim(both '[]' from properties_location), ', ')
       end;

commit;

notify pgrst, 'reload schema';

-- 2. Check after: every row is an array of plain country names.
--
-- select symbol, properties_location from sgx_reit_performance where symbol = 'AJBU.SI';
--   -> {Singapore,Australia,China,Japan,Malaysia,Germany,Ireland,Italy,Netherlands,"United Kingdom"}
--
-- 3. Rollback, if the release has to be undone before the promote:
--
-- begin;
-- alter table sgx_reit_performance
--   alter column properties_location type text
--   using case
--           when properties_location is null then null
--           else '[' || array_to_string(properties_location, ', ') || ']'
--        end;
-- commit;
-- notify pgrst, 'reload schema';
