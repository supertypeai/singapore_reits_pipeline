---
name: reit-extract
description: Extract SGX REIT annual reports (Datalab-parsed markdown) into the sgx_reit_* 6-table schema as JSON, with page provenance, sub-sector-aware playbooks, and a two-stage QC gate. Use when extracting, re-extracting, or verifying REIT annual-report data from parsed_reports_datalab/, when adding a trust/fiscal-year to extracted/, or when the user mentions extraction against the schema.
---

# SGX REIT extraction (Datalab edition)

Turn a Datalab-parsed annual report into schema-shaped JSON under
`extracted/<SYMBOL>.SI_FY<YYYY>/`. Target: `schema/sgx_reit_schema.md` (the locked
6-table plan) and its Pydantic mirror `schema/models.py` (the field contract).

This skill is **data-driven**: every rule below comes from a structural sweep of four
archetype reports — CICT (SG diversified), CapitaLand Ascott Trust (hospitality, stapled),
Keppel DC REIT (data centre), Manulife US REIT (US office). The per-archetype section
maps, table shapes, and quirks are in `REFERENCE.md`.

## The two things that make this corpus hard

1. **Three-tier valuation.** Every report prints each property's value up to THREE times,
   on different bases:
   - **Tier A — marketing "Portfolio Valuation" summary** (S$/US$ *millions*; often the
     REIT's *proportionate* stake for JV assets).
   - **Tier B — per-property detail card / "At A Glance"** (100%-basis valuation, plus
     operating metrics).
   - **Tier C — AUDITED "Portfolio Statement" / "Statement of Portfolio"** ($'000;
     carrying value, may be 100%-consolidated even for <100%-owned assets).

   **`market_valuation` ALWAYS comes from Tier C (the audited Portfolio Statement).** Tier
   A/B values, when they differ, go in `alt_value`/`alt_basis` with a note. Mixing tiers
   is the single biggest error source — see the Gallileo/ION/SGP-3 traps in REFERENCE.md.

2. **Sub-sector changes which tables even exist.** `trade_mix` and `top_tenant` are
   retail/office facts. Data-centre and hospitality trusts disclose *different facts*
   (client-type / contract-type / geography). Do not force-fit them. Run the matching
   **playbook** below.

## Pipeline

```bash
# 1. Map the document: dialect, sub-sector guess, audited-FS page, anchor pages.
python .claude/skills/reit-extract/scripts/locate.py \
    parsed_reports_datalab/<dir>/full.md

# 2. Extract -> write the 8 JSON files (workflow below).

# 3. QC — run BOTH; fix every FAIL before finishing.
python .claude/skills/reit-extract/scripts/validate_schema.py extracted/<SYMBOL>.SI_FY<YYYY>
python .claude/skills/reit-extract/scripts/check_extraction.py extracted/<SYMBOL>.SI_FY<YYYY>
```

`validate_schema.py` enforces the type/enum contract against `schema/models.py`;
`check_extraction.py` enforces reconciliation, units, provenance, and fill rates. They are
complementary — neither alone is sufficient.

## Workflow

1. **Run `locate.py` first.** It prints the page-marker dialect (Datalab =
   `<!-- PAGE N -->`), a **sub_sector guess** with evidence, the audited-FS start page, and
   the page list for every anchor. Read these pages with chunked `Read` (offset/limit) —
   never read 200 pages linearly. Trust the sub_sector guess but sanity-check it (Retail +
   Office co-dominant ⇒ Diversified).

2. **Read §0 INVARIANTS in REFERENCE.md first** — those are the only things you may assume.
   The sub_sector playbooks below are ILLUSTRATIVE PRIORS (where data *usually* lives), NOT
   rules: never decide a table is empty because the playbook says so — discover from THIS
   report and reconcile. (Reports don't generalise by sub-sector; assuming they do is what
   caused under-captures like declaring hospitality's industry mix "absent".)

3. **Extract in source-precedence order** (audited wins every conflict):
   audited Portfolio Statement (Tier C) → audited Statement of Total Return + revenue/
   expense notes → financial review / per-property cards → highlights. When two pages
   disagree, the audited figure is `market_valuation`/`amount`; the other goes to
   `alt_value`/`alt_basis` (+ a `_notes` conflict entry).

4. **Apply the conventions** (non-negotiable — the gates enforce most):
   - **Field names match `schema/models.py` EXACTLY.** The year key is **`financial_year`**
     (int), never `fiscal_year`. `symbol` carries the `.SI` suffix (`C38U.SI`).
   - **Money in ABSOLUTE units.** `$'000` table → ×1000; "S$82.8 million" → 82800000.
     Check the column header every time (Tier C is `$'000`; Tier A is millions). Any
     trust-level money < 1,000,000 is almost certainly unscaled — the gate FAILs it.
   - **As-disclosed only.** Never compute, impute, or annualise. (Exception: nothing.)
   - **Every record carries `source_page` (int).**
   - **Every percentage carries `pct_basis`** in the report's wording, mapped to the enum
     (REFERENCE.md §3). DC/US trusts use `rental_income`/`cash_rental_income`, not `gri`.
   - **`null` only after checking the anchor pages** for that field; say which you checked
     in the record `note`. A field absent for the whole sub-sector → declare it in
     `_notes.columns_never_fillable` (the gate downgrades the warning to INFO).
   - **Enums hold only enum values.** `land_tenure` ∈ {Freehold, Leasehold}; verbatim
     wording ("Leasehold tenure of 99 years w.e.f…") goes to `tenure_raw`. The gate FAILs
     non-enum values. If `tenure_raw` names an expiry, `lease_expiry_date` MUST be filled.
   - **Dual-basis = capture the basis, not just the number.** On JV/<100% properties set
     `value_basis` (consolidated | joint_venture_100pct | effective_interest) and, when a
     second value is printed, `alt_value` + `alt_basis`.
   - **`performance.portfolio_value`** = the headlined portfolio valuation INCLUDING
     proportionate JV interests (not the balance-sheet investment-properties line).
   - **Multi-currency**: set `currency` per record; record any disclosed FX rate in a note.
     Stapled trusts (Ascott): use the **Stapled Group** column. US trusts: USD throughout.

5. **Write the 8 intermediate files** (shapes in REFERENCE.md §2):
   `profile, performance, properties, top_tenants, trade_mix, financial,
   property_transactions, _notes`. They map to the 6 schema tables as:
   `profile→sgx_reit_profile`, `properties→sgx_reit_property`,
   `performance→sgx_reit_performance`, `top_tenants→sgx_reit_top_tenant`,
   `trade_mix→sgx_reit_trade_mix`, `financial→sgx_reit_financial` (financial.json is a
   SINGLE object, 1:1 with prod income_stmt_metrics — not a list of note-lines).
   `property_transactions` is parked (out of scope, kept for audit trail); `_notes` is QC
   metadata. Required fields the schema added: `profile.management`
   (`[{role, company_name}]`) and `trade_mix.category_raw` (verbatim label).

6. **Run both gates; fix every FAIL — at the SOURCE, never by plugging numbers** (REFERENCE.md
   §0 invariant 8). A gate failure is a signal that a value or classification is wrong; the fix
   is whatever the report actually says, with a `source_page`. NEVER invent, reclassify, derive,
   or adjust a figure just to make a sum tie out — that's fabrication even when the arithmetic
   then works. A non-reconciling sum usually means a merged/duplicate table row, a missed
   property, a JV property that's equity-accounted (outside the consolidated total), a below-NPI
   line mislabelled `revenue`, or the total read off a marketing summary instead of the audited
   statement — go confirm which, on the page. If you can't resolve it from the report, leave it
   flagged in `_notes` rather than forcing a balance. Fill `_notes` honestly:
   `columns_never_fillable` (with the structural reason), `data_with_no_home` (≤12 material
   items — feeds schema iteration), `parsing_traps`, `reconciliation`.

## Sub-sector playbooks

> ⚠️ ILLUSTRATIVE PRIORS, NOT RULES (see REFERENCE.md §0). Use them to speed discovery —
> never to decide what's present/absent. The report overrides every one of these.

**Retail / Office / Diversified** (CICT, Manulife US, Keppel REIT, FCT…)
- Full table set. `trade_mix` and `top_tenant` both present and rich.
- `property`: Tier C for valuation/tenure/address; per-property cards for occupancy + GLA/
  NLA + major tenants. **Per-property NPI is often NOT disclosed** (only segment-level) —
  declare it structural rather than computing it. Gross revenue per property is usually in
  the financial-review table AND the cards.
- US trusts: USD; tenure "Freehold" literally; `pct_basis` is `gri`/`cash_rental_income`;
  per-property NPI/revenue may be in **bar charts** Datalab rendered as tables (verify);
  watch held-for-sale assets carrying a separate audited value (use it).

**Data Centre** (Keppel DC, Digital Core)
- `trade_mix`: capture the **client trade-sector** breakdown (e.g. Internet Enterprise,
  IT Services, Telecommunications) with `pct_basis = rental_income`; also capture the
  contract-type mix (Colocation/Single-Tenant/Shell-and-Core) as `data_with_no_home` or a
  second basis — it is NOT the retail 15-value taxonomy, so map loosely and keep
  `category_raw`.
- `top_tenant`: clients are usually **anonymised** ("Fortune Global 500 Company
  (Hyperscaler)") — set `client_name` to the verbatim descriptor, ranked by % rental
  income. Expect extreme concentration (top client 40%+).
- `property`: area metric is "Attributable Lettable Area (sq ft)" (no MW). Per-property NPI
  not disclosed (segment only). Watch NCI/<100% stakes and lease-extension carrying-value
  inflation (Tier C may exceed Tier A/B).

**Hospitality** (CapitaLand Ascott, Far East Hospitality, Centurion)
- `trade_mix`: **leave empty** — there is no trade-sector mix. The analogue is gross-profit
  by contract type (master lease / MCMGI / management contract) and by geography; record
  it in `_notes.data_with_no_home`. Declare `trade_mix` structural.
- `top_tenant`: usually **empty or trivial** (top "corporate clients" total a few %). If a
  weak table exists, capture it but note it isn't tenant-concentration risk.
- `property`: size metric is **units/keys**, not GLA/NLA → declare gla/nla structural.
  Per-property NPI/occupancy/revenue typically absent (only country-level, local currency).
  Stapled REIT+BT: use Stapled Group columns; BT properties sit in a separate Portfolio-
  Statement block. `income_model` is master_lease/mcmgi/management_contract/mixed.

**Healthcare / Industrial** — start from the Retail/Office playbook; healthcare trade mix
maps to "Healthcare, Pharmaceuticals & Life Sciences"; industrial has a long "Other
Industrial Trades" tail. See REFERENCE.md §3 for the category aliases.

## Known traps (wrong-but-plausible data)

Full catalogue in REFERENCE.md §4. The five that bite hardest:

| Trap | Rule |
|---|---|
| Three valuation tiers | `market_valuation` = audited Portfolio Statement ($'000) ONLY; tier A/B → `alt_value` |
| Same fact, two bases | 100% vs proportionate (valuation), consolidated vs attributable (NPI/revenue), incl/excl GTO (tenant %) — capture the basis |
| "Term of lease" in Portfolio Statement | the REIT's LAND lease, not a tenant lease; "Freehold" appears as a literal in numeric columns |
| Held-for-sale / divested-in-year | separate audited row + partial-year P&L contribution; reconciliation won't tie unless you account for it |
| Sub-sector mismatch | DC client-type ≠ trade mix; hospitality contract-type ≠ trade mix; don't emit them as trade_mix rows |

## Property basis and ownership (2026-09-17)

- `market_valuation`, `net_property_income` and `gross_revenue` are stored at **100% of the asset**. If the report prints only the REIT's share, store the 100% figure it prints elsewhere ("carrying value based on 100.0% interest", a footnote) or divide by the stated interest, and say which in a note. Never multiply a figure by `ownership`.
- `ownership` comes from the subsidiary / joint-venture table (effective interest), not from "held under X REIT" or "held through Y Trust". A table row with no property name is mapped by matching its revenue or non-current assets to one property; if no match is certain, leave `ownership` null with a note.
- Strata units owned outright (e.g. Ngee Ann City, Suntec City Office Towers): value as printed, `ownership` = 100.
- An equity investment whose income is dividend income below NPI is not a property valuation: keep the row if asked, but `market_valuation` = null.
- `purchase_price_currency` is the currency the price column is printed in. Many REITs print foreign prices in S$ (A17U, K71U); others print JPY, EUR, IDR. A purchase price is historical: the same property must carry the same price every year — a change means a row shift or a redevelopment cost; check which.
- Parsed tables can shift values one row against their names (A17U FY2024 p.48). Cross-check a row's valuation against the Portfolio Statement before trusting its other columns.

## Self-check before finishing

- Any money value < 1,000,000? You left it in $'000/millions — rescale.
- Every `null` names the anchors checked; every dual-printed figure carries its alternative.
- Re-read your WRITTEN JSON to compose the final message — not memory.
- Both gates green (`SCHEMA: PASS`, `GATE: PASS`) — and green because each value matches the
  report, NOT because you adjusted figures to make a check pass (REFERENCE.md §0 invariant 8).

## Model & scaling

- **Primary extraction: Sonnet** + this skill + both gates. Sonnet finds the numbers
  reliably; the skill transfers the conventions and the gates catch unit drift and
  reconciliation breaks. One agent per report; reports are 0.5–0.9 MB — chunked Read driven
  by the `locate.py` map, never linear.
- Parse with `scripts/parse_datalab.py` (balanced, token-efficient, checkpoint) — the
  `save_checkpoint` id in `meta.json` lets you re-extract without re-paying the parse.
