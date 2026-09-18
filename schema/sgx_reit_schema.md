# `sgx_reit_*` schema

**6 tables + 1 materialized view.** Keyed on `symbol` (properties keep an integer `id`).
Market cap, price/volume, shares, and standard financial statements are never stored —
fetched from `sgx_companies` / `sgx_daily_data` / `sgx_company_report`.

All computed metrics (yield, P/NAV, standardized NPI, land lease remaining) live in the
API layer as versioned formulas — never as columns.

**Decision**

| # | Decision | Where it lands |
|---|---|---|
| 1 | Only 6 REIT tables shipped | `sgx_reit_property_transaction` dropped (corporate/transaction layer out of scope for now) |
| 2 | REIT manager is part of the profile, not a separate table | `sgx_reit_management` dropped; `management` **jsonb** column on `sgx_reit_profile` (manager entities + roles; distinct from prod's people-level `sgx_companies.management`; normalize later only if cross-REIT manager/fee screening becomes a feature) |
| 3 | `income_component` name unclear — it holds the financial breakdown, not just income | renamed **`sgx_reit_financial`** |
| 4 | Trade mix is REIT-level data from the annual report; do NOT assume it aggregates from property level (denominators differ: valuation / rental revenue / tenant count) | `is_derived` flag removed from `sgx_reit_trade_mix`; disclosed figures only |
| 5 | Property valuation = market valuation from the **audited financial statements**; ignore agreed purchase price / agreed JV valuation from property pages | note on `market_valuation` + `valuation_date` |
| 6 | Every table documents its data source per field | `src:` tags below |
| 7 | Indexes added for query speed | §Indexes |

**Source legend (per field):**

- `src: supabase` — pulled/joined from existing Supabase tables (`sgx_companies`, `sgx_daily_data`, `sgx_company_report`); never re-stored unless tagged otherwise
- `src: AR` — extracted from the annual report (pipeline)

Only these two for now. Whether any field needs manual collection (or extracted +
manually verified) is decided after the extraction cross-check vs existing data.

---

## 1. sgx_reit_profile

REIT-specific columns only. Everything generic (name, address, listing info, shares,
market cap) stays in `sgx_companies` and is **joined, not copied** — and only **selected
columns** of `sgx_companies` are exposed through the MV, not the whole table.

```sql
create table sgx_reit_profile (
  symbol           text primary key references sgx_companies(symbol),  -- src: supabase
  sub_sector       text,    -- src: AR — classified into the dedicated REIT sub-sector
                            -- list (~7 values):
                            -- Retail | Office | Industrial | Hospitality | Healthcare |
                            -- Data Centre | Diversified
                            -- sgx_companies.sub_sector confirmed DIFFERENT (e.g. 'Hotel
                            -- & Motel') — this REIT-specific value wins, sourced from AR.
                            -- cheap to verify later: one value per trust, eyeball-able
  management       jsonb    -- src: AR — replaces the dropped management table;
                            -- holds the manager ENTITIES (companies + roles):
                            -- [{"role": "reit_manager",
                            --   "company_name": "CapitaLand Integrated Commercial
                            --                    Trust Management Limited"}, ...]
                            -- roles: reit_manager | property_manager | trustee |
                            --        sponsor | operator | master_lessee
                            -- NOT the same as sgx_companies.management, which holds
                            -- PEOPLE ({age, name, position, start_date}) — the two are
                            -- complementary; people-level data stays in prod.
                            -- normalize into a table later only if cross-REIT
                            -- manager/fee screening becomes a feature
);
```

Parked (not in this iteration): `income_model`, transaction history.

## 2. sgx_reit_property

One row per (symbol, property, financial_year). A property sold to another trust appears
under the buyer's symbol the following year.

**Valuation rule (meeting decision):** `market_valuation` is the figure from the
**audited financial statements** (audited Portfolio Statement / investment-property
note), and `valuation_date` follows the financial-statement valuation date. The agreed
purchase price or agreed JV valuation printed on property marketing pages is ignored.

```sql
create table sgx_reit_property (
  id                  serial primary key,
  symbol              text references sgx_reit_profile(symbol),  -- src: AR
  financial_year         smallint,   -- src: AR
  country             text,       -- src: AR
  category            text,       -- src: AR — canonical 6 (Evelyn, Jun 17 2026):
                                  -- Industrial & Logistics | Office | Retail | Data Centers
                                  -- | Specialized | Diversified (Commercial). raw asset
                                  -- types reclassify via PROPERTY_CATEGORY_ALIASES, e.g.
                                  -- Flatted Factories/Stack-up → Industrial & Logistics;
                                  -- Life Sciences/Hi-Tech/Business Space → Specialized
  category_raw        text,       -- src: AR — verbatim disclosed asset type (audit trail)
  property_name       text,       -- src: AR
  address             text,       -- src: AR
  ownership           numeric,    -- src: AR — % stake (kept per meeting)
  market_valuation    numeric,    -- src: AR — audited FS valuation ONLY (see rule above);
                                  -- convention: recorded at 100% of the property as the
                                  -- audited FS prints it; effective-interest value =
                                  -- market_valuation × ownership, computed in API layer
  valuation_date      date,       -- src: AR — the FS valuation date, not page dates
  currency            text,       -- src: AR
  net_property_income numeric,    -- src: AR — as-disclosed only, never computed
  gross_revenue       numeric,    -- src: AR
  npi_pct             numeric,    -- src: AR — property's share of portfolio NPI (% plain
                                  -- number), when disclosed as a percentage not an absolute
  occupancy_rate      numeric,    -- src: AR
  trade_mix           jsonb,      -- src: AR — property-level set, sparse (few trusts
                                  -- disclose it); REIT-level mix lives in its own table;
                                  -- keys use the same canonical 15-value category list
  major_tenants       jsonb,      -- src: AR — [{name, industry?, pct?}]; a property often
                                  -- has several major/top tenants (was major_tenant text)
                                  -- gla DROPPED 2026-08-03 (schema review): set on 9/3420 rows,
                                  -- values moved into nla, column removed
  nla                 numeric,    -- src: AR — LETTABLE area (net, or gross where that is all
                                  -- the report discloses)
  gfa                 numeric,    -- src: AR — gross FLOOR area (built area, not lettable);
                                  -- many cards disclose GFA — keep separate from nla
  land_tenure         text,       -- src: AR — Freehold | Leasehold
  effective_date      date,       -- src: AR — land-lease start
  lease_term_years    numeric,    -- src: AR — parsed from '64/99' → 99
  lease_expiry_date   date,       -- src: AR — when disclosed
  tenure_raw          text,       -- src: AR — verbatim disclosure (audit trail)
  status              text default 'active',  -- src: AR — active | divested |
                                              -- held_for_sale (the divestment indicator)
  divestment_price    numeric,    -- src: AR — sale price/proceeds when status=divested
                                  -- and disclosed (absolute); null otherwise
  flags               jsonb,      -- src: AR — caveats to verify (Jun 17 meeting):
                                  -- [{type, scope, note}], e.g. same_property_diff_lease,
                                  -- divested_partial_data, full_consolidation_partial_ownership
                                  -- (100% financials on a <100% owned asset)
  source_page         int,        -- provenance
  unique (symbol, property_name, financial_year)
);
```

No stored remaining-lease column — computed from dates at query time.
No purchase price / transaction columns — dropped per meeting.

## 3. sgx_reit_performance

One row per (symbol, financial_year). Conventions aligned with `sgx_manual_input`
(verified against its actual structure): `financial_year smallint` as the year key,
`date` for the FY-end date, `source_url` for the source report link. REIT-specific
fields the manual-input structure lacks (`net_distributable_income`,
`distribution_record`, `dpu`, `portfolio_value`) are added here.

Generic financials (revenue/EBIT/balance-sheet/cash-flow) are NOT duplicated — they
already live in `sgx_manual_input.income_stmt_metrics` / `balance_sheet_metrics` /
`cash_flow_metrics` jsonb per (symbol, financial_year).

```sql
create table sgx_reit_performance (
  symbol                   text references sgx_reit_profile(symbol),  -- src: AR
  financial_year              smallint,  -- src: AR
  portfolio_value          numeric,   -- src: AR — headlined portfolio valuation incl. JV
                                      -- proportionate interests (pinned definition)
  properties_location      text[],    -- src: AR — canonical country names, each with an
                                      -- ISO 3166-1 alpha-2 code (see db_guard.COUNTRY_CODE)
  gross_revenue            numeric,   -- src: AR — verify vs sgx_company_report revenue;
                                      -- if identical, drop and fetch instead
  net_property_income      numeric,   -- src: AR — as reported
  net_distributable_income numeric,   -- src: AR — REIT-specific add (meeting)
  dpu                      numeric,   -- src: AR — full-year, SGD dollars (same unit as NAV)
  distribution_record      jsonb,     -- src: AR — [{period, dpu, ex_date, pay_date}];
                                      -- captures the half-year (H1/H2) DPU split case
  number_of_unitholders    int,       -- src: AR
  -- as-disclosed comparison KPIs (added 2026-06-18) — capture verbatim, null if not disclosed
  aggregate_leverage       numeric,   -- src: AR — gearing / aggregate leverage, % plain number
  interest_coverage_ratio  numeric,   -- src: AR — ICR, x (times)
  cost_of_debt             numeric,   -- src: AR — weighted avg all-in cost of debt, % plain number
  weighted_avg_debt_maturity numeric, -- src: AR — weighted avg debt maturity/tenor, years
  nav_per_unit             numeric,   -- src: AR — NAV per unit, trust currency
  wale                     numeric,   -- src: AR — weighted average lease expiry, years
  portfolio_occupancy      numeric,   -- src: AR — committed/portfolio occupancy, % plain number
  currency                 text,      -- src: AR
  date                     date,      -- src: AR — FY-end date (manual-input convention)
  flags                    jsonb,     -- src: AR — caveats to verify, [{type, scope, note}],
                                      -- e.g. dpu_half_year_split (Jun 17 meeting)
  source_url               text,      -- provenance (manual-input convention)
  source_page              int,       -- provenance
  primary key (symbol, financial_year)
);
-- market_cap / price / standard financials: src: supabase — fetched from sgx_companies /
-- sgx_manual_input (income_stmt/balance_sheet/cash_flow_metrics jsonb), never stored here
```

## 4. sgx_reit_top_tenant

```sql
create table sgx_reit_top_tenant (
  symbol         text references sgx_reit_profile(symbol),  -- src: AR
  financial_year    smallint,    -- src: AR
  rank           smallint,    -- src: AR
  client_name    text,        -- src: AR — tenant name; null when anonymised (was
                              -- tenant_name; renamed to match prod client_name)
  industry       text,        -- src: AR — same canonical 15-value list as
                              -- sgx_reit_trade_mix.category (was trade_sector; renamed
                              -- to match prod 'industry'); one taxonomy everywhere
  revenue_pct    numeric,     -- src: AR — plain number e.g. 5.0 (was gri_percentage;
                              -- prod stores 0.05 — convert at the manual_input transform)
  pct_basis      text,        -- src: AR — gri | gri_excl_gto | gross_revenue |
                              -- rental_income | headline_rent | cash_rental_income |
                              -- nla | outlet_sales
  source_page    int,         -- provenance
  primary key (symbol, financial_year, rank)
);
```

## 5. sgx_reit_trade_mix

**REIT-level data from the annual report, as disclosed.** Per the meeting: do NOT
derive this by aggregating property-level data — the disclosed percentages may be
based on valuation, rental revenue, tenant count, NLA, etc., so a property roll-up is
not comparable. (The old `is_derived` flag is removed.)

**Category enum (15 values — Evelyn, Jun 17 2026).** Replaces the earlier 19-value list;
five categories were consolidated. The disclosed label is kept verbatim in `category_raw`;
the extraction pipeline maps it to the canonical `category` via the alias dictionary
(`TRADE_ALIASES` in `schema/models.py`). Same taxonomy is used by `top_tenant.industry`.

```sql
create table sgx_reit_trade_mix (
  symbol         text references sgx_reit_profile(symbol),  -- src: AR
  financial_year smallint,  -- src: AR
  category       text check (category in (
                   'Food & Beverages',
                   'Financial & Professional Services',
                   'Healthcare & Wellness',
                   'Fashion & Accessories',
                   'Hospitality & Leisure',
                   'Infrastructure, Real Estate & Property Services',
                   'IT & Telecommunications',
                   'Other Office Trades',
                   'Other Retail Trades',
                   'Other Industrial Trades',
                   'Logistics & Supply Chain Management',
                   'Manufacturing',
                   'Government Related',
                   'Energy, Mining & Resources',
                   'Departmental Store/Supermarket'
                 )),       -- src: AR — mapped to canonical via alias dictionary
  category_raw   text,     -- src: AR — verbatim disclosed label (audit trail; allows
                           -- remapping without re-extraction)
  pct            numeric,  -- src: AR
  pct_basis      text,     -- src: AR — the denominator the trust used (mandatory for
                           -- cross-REIT comparison)
  source_page    int,      -- provenance
  primary key (symbol, financial_year, category)
);
```

Consolidation from the old 19-value list (now part of the alias dictionary):

| Old labels | → Canonical (15) |
|---|---|
| Banking, Insurance & Financial Services · Professional Services | Financial & Professional Services |
| Beauty & Health · Healthcare, Pharmaceuticals & Life Sciences | Healthcare & Wellness |
| Real Estate & Property Services · Construction & Engineering | Infrastructure, Real Estate & Property Services |
| Mining & Resources · Energy & Utilities | Energy, Mining & Resources |

> Note: "Beauty & Health" → Healthcare & Wellness by default (retail beauty tenants can be
> remapped to Other Retail Trades via `category_raw` if needed).

Further alias mappings (synonyms, NOT new categories):
'TMT' / 'TAMI' / 'Information & Communications Technology' → IT & Telecommunications;
'F&B' / 'Food & Beverage' → Food & Beverages; 'Supermarket & Grocers' / 'Grocery &
Wholesale' → Departmental Store/Supermarket; 'Public Administration' / 'Government
agency' → Government Related; '3PL' / 'Transportation - Storage' → Logistics & Supply
Chain Management; retail sub-trades (Jewellery & Watches, Sports Apparel, Homeware,
Education, Leisure & Entertainment...) → Other Retail Trades unless a baseline
category fits.

Structurally out of scope (leave the table EMPTY for these, don't force-fit):
hospitality/accommodation trusts (CLAS, FEHT, Centurion) disclose income by contract
type/geography, not trade sectors; data-centre trusts (KDC, Digital Core) disclose
customer types (hyperscaler/colocation). Those are different facts, not a trade mix —
the CLAS pilot extraction proved this by emitting contract types as 'categories'.

## 6. sgx_reit_financial  *(renamed from `income_component`; restructured Jun 17 2026)*

**One row per (symbol, financial_year). The sector-agnostic financial-statement core — 1:1 with
the three financial-statement jsonb blobs of prod `sgx_manual_input`** (`income_stmt_metrics`,
`balance_sheet_metrics`, `cash_flow_metrics`). We extract every figure ourselves from the AR's
audited statements. Per the Jun 17 meeting, extraction lands in the **SGX REITs DB** (source of
truth); Gerald later pushes REITs-DB → `sgx_manual_input`, so a blob-for-blob match makes that a
copy, not a remap — and positions this table to **generalise to non-REIT SGX companies** (a bank/
airline carries different keys inside the same blobs; the models allow extra keys).

**NOT here** — the REIT *enrichment* lives in the other tables: `industry_breakdown` is composed
from `top_tenant`/`trade_mix`/`property`; `net_property_income`(named NPI), `net_distributable_
income`, `dpu`, `distribution_record`, `portfolio_value` are `sgx_reit_performance`'s job.
`sankey_component` is **derivable** from the income-statement breakdowns, so it isn't stored.

A REIT's audited statement doesn't literally print `cost_of_revenue / gross_income /
operating_income / ebit / ebitda`; prod **standardizes** them (property expenses →
`cost_of_revenue`, NPI → `gross_income`, …) — we apply the same mapping. The verbatim audited
Statement-of-Total-Return lines are preserved in `line_items` (our extension, NOT in prod) as the
reconciliation anchor (Σrevenue − Σexpense + Σadjustment(signed) = `income_stmt_metrics.net_income`).

> Note: the `references sgx_reit_profile(symbol)` FK is narrow for a would-be all-SGX table — if
> this generalises beyond REITs, re-point it at the generic company symbol.

```sql
create table sgx_reit_financial (
  symbol         text references sgx_reit_profile(symbol),  -- src: AR
  financial_year smallint,  -- src: AR
  currency       text,      -- src: AR

  -- 1:1 with prod's three financial-statement blobs (prod's exact keys) — src: AR
  income_stmt_metrics   jsonb,  -- total_revenue, cost_of_revenue, gross_income(=NPI),
                                -- operating_income/expense, ebit, ebitda, pretax_income,
                                -- income_taxes, net_income, non_operating_income_or_loss,
                                -- interest_expense_non_operating, diluted_shares_outstanding,
                                -- net_property_sales, funds_from_operation, unitholders,
                                -- perpetual_security_holders, minorities,
                                -- revenue_breakdown/operating_expense_breakdown [{class,amount,category}]
  balance_sheet_metrics jsonb,  -- total_asset, total_equity, total_liabilities,
                                -- working_capital, total_(non_)current_asset/liabilities
  cash_flow_metrics     jsonb,  -- operating/investing/financing_cash_flow, net_cash_flow,
                                -- free_cash_flow, capital_expenditure
  employee_breakdown    jsonb,  -- permanent/contract/others/total_employee; usually NULL for
                                -- REITs (externally managed) — the one sgx_manual_input blob
                                -- with no other home, kept here for 1:1 coverage

  -- OUR extension (audit trail; not in prod, not pushed) — src: AR
  line_items            jsonb,  -- [{statement(revenue|expense|adjustment), component,
                                --   amount (adjustments SIGNED), label_raw, source_page}]

  source_page    int,       -- provenance
  primary key (symbol, financial_year)
);
```

The standardized-formula consumers (standardized NPI, GRI-only revenue, cost ratio) read the
scalars / breakdowns directly; the API layer can still reconstruct the fine breakdown from
`line_items` and synthesize a `sankey_component` for the cockpit.

## mv_sgx_reit

The single queryable surface: **selected** `sgx_companies` columns (not the whole
table) + REIT enrichment.

```sql
create materialized view mv_sgx_reit as
select c.symbol, c.name, c.sector,        -- selected sgx_companies columns only;
                                          -- extend deliberately, column by column
       p.sub_sector, p.management,
       perf.financial_year, perf.portfolio_value, perf.net_property_income,
       perf.dpu, perf.net_distributable_income
from sgx_companies c
join sgx_reit_profile p using (symbol)
left join sgx_reit_performance perf using (symbol);
-- refresh after each extraction run
```

---

## Indexes

Primary keys above already index the main lookup paths (`symbol`, `(symbol,
financial_year)`, `(symbol, financial_year, rank)`, …). These cover the remaining query
patterns:

```sql
-- property: per-REIT-per-year portfolio pulls, plus cross-REIT screens
create index idx_reit_property_symbol_fy on sgx_reit_property (symbol, financial_year);
create index idx_reit_property_country   on sgx_reit_property (country);
create index idx_reit_property_category  on sgx_reit_property (category);
create index idx_reit_property_fy        on sgx_reit_property (financial_year);

-- performance: cross-REIT comparisons within a year
create index idx_reit_performance_fy on sgx_reit_performance (financial_year);

-- top tenant: 'where is DBS/Amazon a tenant' style lookups
create index idx_reit_top_tenant_name on sgx_reit_top_tenant (client_name);

-- trade mix: category screens across REITs
create index idx_reit_trade_mix_category on sgx_reit_trade_mix (category, financial_year);

-- financial: there is NO `component` column — the audited line components live inside the
-- `line_items` jsonb. For component screens across REITs ('utilities cost everywhere'), GIN-
-- index the jsonb and query via a view; OR, if it becomes a real feature, materialize a
-- long-format child table sgx_reit_financial_line(symbol, financial_year, statement, component,
-- amount, label_raw, source_page) and index `component` there.
create index idx_reit_financial_line_items on sgx_reit_financial using gin (line_items);

-- profile: sub-sector filter (small table, but the most common WHERE clause)
create index idx_reit_profile_sub_sector on sgx_reit_profile (sub_sector);

-- MV: unique index enables REFRESH MATERIALIZED VIEW CONCURRENTLY
create unique index idx_mv_sgx_reit on mv_sgx_reit (symbol, financial_year);
```

---

## Dropped at the final meeting

- **`sgx_reit_property_transaction`** — transaction/corporate layer out of scope;
  agreed purchase prices and JV deal valuations from property pages are ignored for now.
- **`sgx_reit_management`** — folded into the profile as a `management` jsonb (all
  roles: REIT manager, property manager, trustee, sponsor, operator, master lessee);
  normalize later only if manager/fee screening becomes a feature.
- **`value_basis`** on property — existed to disambiguate the same property printed at
  100% vs effective stake (Gallileo: S$547.6m vs S$519.7m at 94.9%). Superseded by the
  audited-FS-only valuation rule + kept `ownership` (effective value is computable).
  If the extraction cross-check finds trusts whose audited statements print effective
  stake instead of 100%, revisit.
- **`income_model`** on the profile — parked per earlier review.
- **`is_derived` trade-mix roll-up** — trade mix is captured as disclosed, REIT-level only.

## Open checks before migration

1. ~~**Sub-sector taxonomy**~~ — RESOLVED: some of `sgx_companies` category uses a different
   taxonomy (e.g. 'Hotel & Motel'), so `sub_sector` is populated from AR extraction
   against the ~7-value REIT list. Verification is cheap (one value per trust) — spot-
   check after the extraction run.
2. ~~**Performance ↔ SGX manual input**~~ — RESOLVED: `sgx_manual_input` is one row per
   (symbol, financial_year) with jsonb metric blobs. Conventions adopted:
   `financial_year` (replacing our `fiscal_year` everywhere), `date` (FY-end),
   `source_url`. Generic financials stay in its jsonb blobs — not duplicated here.
3. **`gross_revenue` duplication** — evidence so far: CICT FY2024
   `income_stmt_metrics.total_revenue` = 1,586,329,000 = the AR's gross revenue, so the
   figure DOES already exist in `sgx_manual_input`. Kept for now because (a) manual
   input is manually maintained and our pipeline should not depend on it being filled,
   and (b) the AR figure is the reconciliation anchor for `sgx_reit_financial`
   (components must sum to it). Revisit after the cross-check.
4. **Extraction cross-check** — compare pipeline output against existing/manual data to
   decide fully-automated vs hybrid collection (target: ~10 extraction results to
   Evelyn by Mon Jun 15 2026). See mapping below.
5. ~~**`management` jsonb shape**~~ — RESOLVED: `sgx_companies.management` holds PEOPLE
   (directors/execs: {age, name, position, start_date}), a different concept from our
   manager ENTITIES — no shape to mirror; our [{role, company_name}] shape stands.
6. **Symbol format** — `sgx_companies` stores symbols WITHOUT the `.SI` suffix
   (`K71U`, `H13`) while `sgx_manual_input` uses `C38U.SI` WITH it. Our extraction
   output also uses `.SI`. The `sgx_reit_*` tables FK into `sgx_companies(symbol)`, so
   they must use the suffix-less format — normalize (`strip '.SI'`) at ingestion, and
   strip it when joining against `sgx_manual_input` for the cross-check.

## Cross-check mapping: `sgx_manual_input` ↔ `sgx_reit_*`

`sgx_manual_input.industry_breakdown` already holds manually-collected versions of our
REIT data — this is the ground truth to compare the ~10 extraction runs against:

| `sgx_manual_input` (jsonb path) | Cross-checks against | Notes |
|---|---|---|
| `industry_breakdown.top_10_gri%_customers` (industry, client_name, revenue_pct) | `sgx_reit_top_tenant` | now ALIGNED: our fields are `industry`/`client_name`/`revenue_pct`. Theirs is a fraction (0.05), ours plain (5.0) — convert in Gerald's transform |
| `industry_breakdown.gross_rental_income_by_sectors` ({category: fraction}) | `sgx_reit_trade_mix` | canonical 15-value taxonomy (Jun17) |
| `industry_breakdown.property_portfolio_top_20` (name, country, category, valuation, gross_income, occupancy_rate, ownership_pct) | `sgx_reit_property` | top-20 only; full portfolio is our table's superset |
| `industry_breakdown.property_counts_by_country` | derivable from `sgx_reit_property` | count/sum per (country, category) |
| `sankey_component.links` + `income_stmt_metrics.revenue_breakdown` / `operating_expense_breakdown` | `sgx_reit_financial` | their categories ('Gross rental', 'Car park', 'Management fees'...) map to our canonical `component` keys |
| `income_stmt_metrics.total_revenue` | `sgx_reit_performance.gross_revenue` | reconciliation anchor |
