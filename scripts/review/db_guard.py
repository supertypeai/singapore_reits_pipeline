"""db_guard.py — whole-database guard for the dev SGX REIT tables.

Runs every invariant the per-report gates cannot see, because they check one extraction at
a time and the defects that actually shipped were cross-row, cross-year or whole-column:

  ME8U FY2023 category never mapped through the aliases  (a per-report enum check passes:
      the raw label IS a valid string, just not a canonical one)
  basis_segment tagged on FY2024 but not FY2025          (needs a cross-year comparison)
  T82U trade_mix summing to 200%                         (needs the sum grouped by segment)
  percentages living on two scales at once               (needs a range check per column)
  the same transaction present in two report years       (needs a cross-report view)
  deal_id left grouping a single row                     (needs the final row set)

Checks raw (`sgx_reit_*`) and derived (`sgx_reit_*_final`). Prints every finding grouped by
severity and exits 1 if any FAIL. WARNs never fail the run.

Usage:
  python scripts/review/db_guard.py                 # everything
  python scripts/review/db_guard.py --only scale,enums
  python scripts/review/db_guard.py --list          # show group names
  python scripts/review/db_guard.py --quiet         # findings only, no PASS lines
"""
import os, sys, argparse, json
import psycopg2
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(ROOT, ".env"))

FAIL, WARN = "FAIL", "WARN"
findings = []          # (severity, group, check, message)
passes = []            # (group, check, note)


WAIVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db_guard_waivers.json")
WAIVERS = {}
if os.path.exists(WAIVER_PATH):
    with open(WAIVER_PATH, encoding="utf-8") as fh:
        WAIVERS = {(w["group"], w["check"]): w for w in json.load(fh)["waivers"]}
waived = []


def add(sev, group, check, msg):
    """A waived finding is still printed, just downgraded so it cannot fail the run. Waivers
    are keyed on (group, check) and each carries a reason, so a NEW failure in the same check
    still shows up in the text even though the run stays green."""
    w = WAIVERS.get((group, check))
    if w and sev == FAIL:
        waived.append((group, check, msg, w.get("reason", "")))
        return
    findings.append((sev, group, check, msg))


def ok(group, check, note=""):
    passes.append((group, check, note))


# ---------------------------------------------------------------- controlled vocabularies
PROPERTY_CATEGORY = {"Industrial & Logistics", "Office", "Retail", "Data Centers",
                     "Specialized", "Diversified (Commercial)"}
PROPERTY_STATUS = {"active", "divested", "held_for_sale"}
LAND_TENURE = {"Freehold", "Leasehold"}
PCT_BASIS = {"headline_rent", "annualised_rent", "npi", "asset_value",
             "gross_rental_income", "gross_revenue"}
BASIS_SEGMENT = {"office", "retail", "commercial", "logistics_industrial"}
TXN_BASIS = {"valuation", "book_value", "purchase_price", "net_identifiable_assets"}
TXN_TYPE = {"acquisition", "divestment"}
TRADE_CATEGORY = {
    "Food & Beverages", "Financial & Professional Services", "Healthcare & Wellness",
    "Other Retail Trades", "Logistics & Supply Chain Management", "Hospitality & Leisure",
    "Departmental Store/Supermarket", "Infrastructure, Real Estate & Property Services",
    "IT & Telecommunications", "Government Related", "Fashion & Accessories",
    "Manufacturing", "Other Industrial Trades", "Energy, Mining & Resources",
    "Other Office Trades"}

# columns that must lie in 0-1 in *_final. Anything outside means a layer double-scaled or
# skipped pct01(); both have happened.
PCT01 = [
    ("sgx_reit_property_final", "occupancy_rate"),
    ("sgx_reit_property_final", "ownership"),
    ("sgx_reit_top_tenant_final", "pct"),
    ("sgx_reit_trade_mix_final", "pct"),
    ("sgx_reit_property_transaction_final", "interest_pct"),
    ("sgx_reit_performance_final", "aggregate_leverage"),
    ("sgx_reit_performance_final", "cost_of_debt"),
    ("sgx_reit_performance_final", "portfolio_occupancy"),
]
# deliberately NOT percentages. A value inside 0-1 here means someone scaled it by mistake;
# an absurd upper value means the opposite.
NOT_PCT = [
    ("sgx_reit_performance_final", "interest_coverage_ratio", 0.5, 60),
    ("sgx_reit_performance_final", "weighted_average_lease_expiry", 0.1, 60),
    ("sgx_reit_performance_final", "weighted_average_debt_maturity", 0.1, 30),
    ("sgx_reit_performance_final", "distribution_per_unit", 0, 2),        # SGD dollars
    ("sgx_reit_performance_final", "net_asset_value_per_unit", 0, 50),    # dollars
]


def q(cur, sql, args=None):
    cur.execute(sql, args or ())
    return cur.fetchall()


def table_exists(cur, t):
    return bool(q(cur, "select 1 from information_schema.tables where table_name=%s", (t,)))


# ================================================================= groups
def check_scale(cur):
    g = "scale"
    for t, c in PCT01:
        if not table_exists(cur, t):
            continue
        bad = q(cur, f"select count(*), min({c}), max({c}) from {t} where {c} is not null and ({c} < 0 or {c} > 1)")[0]
        if bad[0]:
            add(FAIL, g, f"{t}.{c}", f"{bad[0]} rows outside 0-1 (min {bad[1]}, max {bad[2]}). "
                                     f"Percentages are stored 0-1; a 0-100 value means pct01() did not run")
        else:
            rng = q(cur, f"select min({c}), max({c}) from {t}")[0]
            ok(g, f"{t}.{c}", f"{rng[0]} .. {rng[1]}")
    for t, c, lo, hi in NOT_PCT:
        if not table_exists(cur, t):
            continue
        bad = q(cur, f"select count(*) from {t} where {c} is not null and ({c} < %s or {c} > %s)", (lo, hi))[0][0]
        if bad:
            add(WARN, g, f"{t}.{c}", f"{bad} rows outside the plausible band {lo}..{hi}. "
                                     f"This column is NOT a percentage and must not be scaled")
        else:
            ok(g, f"{t}.{c}", "in band")


def check_enums(cur):
    g = "enums"
    ENUMS = [
        ("sgx_reit_property", "category", PROPERTY_CATEGORY),
        ("sgx_reit_property", "status", PROPERTY_STATUS),
        ("sgx_reit_property", "land_tenure", LAND_TENURE),
        ("sgx_reit_top_tenant", "pct_basis", PCT_BASIS),
        ("sgx_reit_top_tenant", "basis_segment", BASIS_SEGMENT),
        ("sgx_reit_top_tenant", "industry", TRADE_CATEGORY),
        ("sgx_reit_trade_mix", "pct_basis", PCT_BASIS),
        ("sgx_reit_trade_mix", "basis_segment", BASIS_SEGMENT),
        ("sgx_reit_trade_mix", "category", TRADE_CATEGORY),
        ("sgx_reit_property_transaction", "basis", TXN_BASIS),
        ("sgx_reit_property_transaction", "transaction_type", TXN_TYPE),
    ]
    for t, c, allowed in ENUMS:
        rows = q(cur, f"select {c}, count(*) from {t} where {c} is not null group by 1")
        bad = [(v, n) for v, n in rows if v not in allowed]
        if bad:
            add(FAIL, g, f"{t}.{c}",
                "values outside the canonical list: " + ", ".join(f"{v!r} x{n}" for v, n in bad))
        else:
            ok(g, f"{t}.{c}", f"{len(rows)} distinct, all canonical")


def check_mapping_ran(cur):
    """category == category_raw means the alias mapping never ran for that batch. That is how
    ME8U FY2023 shipped 5 raw asset types as if they were canonical categories."""
    g = "mapping"
    for t, canon in (("sgx_reit_property", PROPERTY_CATEGORY),
                     ("sgx_reit_trade_mix", TRADE_CATEGORY)):
        # equality alone is not suspicious: a raw label of "Office" legitimately equals the
        # canonical "Office". It is suspicious when the shared value is NOT canonical, which
        # means the raw label passed straight through unmapped.
        rows = q(cur, f"""select symbol, financial_year, count(*) from {t}
                          where category_raw is not null and category = category_raw
                            and category <> all(%s)
                          group by 1,2 order by 3 desc""", (list(canon),))
        suspect = list(rows)
        if suspect:
            add(WARN, g, t, "REIT-years where category == category_raw on many rows, which is "
                            "what a skipped alias mapping looks like: " +
                            ", ".join(f"{s} FY{fy} x{n}" for s, fy, n in suspect[:8]))
        else:
            ok(g, t, "no batch looks unmapped")


def check_sums(cur):
    g = "sums"
    # Two shapes are both correct and the check must accept either:
    #   separate denominators  -> EACH segment sums to ~100 (T82U office and retail)
    #   one portfolio split    -> ALL segments sum to ~100 TOGETHER (BUOU commercial +
    #                             logistics_industrial = 35.6 + 64.4)
    # A scope fails only when neither holds.
    rows = q(cur, """select symbol, financial_year, coalesce(basis_segment,'-'), round(sum(pct)::numeric, 4)
                     from sgx_reit_trade_mix group by 1,2,3""")
    by_scope = {}
    for sym, fy, seg, v in rows:
        by_scope.setdefault((sym, fy), []).append((seg, float(v)))
    bad = []
    for (sym, fy), segs in by_scope.items():
        each = all(abs(v - 100) <= 2 for _s, v in segs)
        together = abs(sum(v for _s, v in segs) - 100) <= 2
        if not (each or together):
            bad.append((sym, fy, ", ".join(f"{s2}={v:.1f}" for s2, v in segs)))
    if bad:
        add(FAIL, g, "trade_mix.pct sums to 100",
            "; ".join(f"{sym} FY{fy} [{d}]" for sym, fy, d in bad[:8]) +
            (f" (+{len(bad)-8} more)" if len(bad) > 8 else "") +
            ". Each segment should sum to 100, or all segments together should")
    else:
        ok(g, "trade_mix.pct", f"{len(by_scope)} REIT-years sum correctly")

    rows = q(cur, """select symbol, financial_year, coalesce(basis_segment,'-'), round(sum(pct)::numeric,2)
                     from sgx_reit_top_tenant group by 1,2,3""")
    bad = [r for r in rows if float(r[3]) > 100.5]
    if bad:
        add(FAIL, g, "top_tenant.pct <= 100 per segment",
            "; ".join(f"{s} FY{fy} seg={seg} = {v}" for s, fy, seg, v in bad[:8]))
    else:
        ok(g, "top_tenant.pct", f"{len(rows)} scopes all <= 100")

    n = q(cur, "select count(*) from sgx_reit_performance where net_property_income > gross_revenue")[0][0]
    (add(FAIL, g, "performance NPI <= gross_revenue", f"{n} rows violate it") if n
     else ok(g, "performance NPI <= gross_revenue"))
    n = q(cur, """select count(*) from sgx_reit_property
                  where net_property_income > gross_revenue
                    and gross_revenue_currency is not distinct from net_property_income_currency""")[0][0]
    (add(FAIL, g, "property NPI <= gross_revenue", f"{n} same-currency rows violate it") if n
     else ok(g, "property NPI <= gross_revenue", "same-currency rows only"))
    # a row whose revenue and NPI are booked in different currencies cannot be compared at all
    x = q(cur, """select count(*) from sgx_reit_property
                  where gross_revenue is not null and net_property_income is not null
                    and gross_revenue_currency is distinct from net_property_income_currency""")[0][0]
    (add(WARN, g, "property revenue/NPI currency mismatch",
         f"{x} rows book gross_revenue and net_property_income in different currencies; "
         f"no ratio or margin over them is meaningful") if x
     else ok(g, "property revenue/NPI currency", "consistent"))


def check_keys(cur):
    g = "keys"
    # Raw and prod do not share a key for every table. trade_mix raw holds one row per
    # category_raw and the promote sums them into one canonical category; transaction raw can
    # hold two deals on the same property in one year (DCRU's staged Wilhelm-Fay-Strasse
    # purchase) and the promote suffixes the second. Checking the PROD key against RAW would
    # fail on both by design, so each table is checked on its own true key.
    PK = [("sgx_reit_property", "symbol,financial_year,property_name", 3),
          ("sgx_reit_top_tenant", "symbol,financial_year,rank", 3),
          ("sgx_reit_trade_mix", "symbol,financial_year,category_raw,pct_basis,coalesce(basis_segment,'')", 5),
          ("sgx_reit_performance", "symbol,financial_year", 2)]
    for t, cols, ncol in PK:
        # the count is explicit: coalesce(basis_segment,'') contains a comma, so splitting
        # the string would count one column too many
        grp = ",".join(str(i + 1) for i in range(ncol))
        dup = q(cur, f"select count(*) from (select {cols} from {t} group by {grp} having count(*)>1) x")[0][0]
        (add(FAIL, g, f"{t} PK", f"{dup} duplicate keys on ({cols})") if dup
         else ok(g, f"{t} PK", "unique"))

    # trade_mix: rows sharing a canonical category must differ by category_raw, otherwise the
    # promote would sum two copies of the same disclosure.
    dup = q(cur, """select count(*) from (
              select symbol, financial_year, category, category_raw, pct_basis,
                     coalesce(basis_segment,'')
              from sgx_reit_trade_mix group by 1,2,3,4,5,6 having count(*) > 1) x""")[0][0]
    (add(FAIL, g, "trade_mix collapsible", f"{dup} groups share BOTH category and category_raw; "
                                           f"the promote would double-count them") if dup
     else ok(g, "trade_mix collapsible", "every same-category group differs by category_raw"))

    # transaction: repeats on one property in one year are legitimate but the promote must be
    # able to tell them apart, which it does by suffixing property_name.
    rows = q(cur, """select symbol, financial_year, transaction_type, property_name, count(*)
                     from sgx_reit_property_transaction group by 1,2,3,4 having count(*) > 1""")
    if rows:
        add(WARN, g, "transaction repeats on one property",
            "; ".join(f"{s2} FY{fy} {tt} {pn} x{n}" for s2, fy, tt, pn, n in rows[:5]) +
            ". Legitimate (a staged purchase), and disambiguate_txn() suffixes the second on "
            "promotion. Flagged so a genuine duplicate is not mistaken for one")
    else:
        ok(g, "transaction repeats on one property", "none")

    # a deal_id spanning two report years is the same transaction disclosed twice
    rows = q(cur, """select deal_id, count(distinct financial_year), count(*)
                     from sgx_reit_property_transaction where deal_id is not null
                     group by 1 having count(distinct financial_year) > 1""")
    if rows:
        add(FAIL, g, "transaction cross-year duplicates",
            f"{len(rows)} deals appear in more than one financial year: " +
            ", ".join(r[0] for r in rows[:5]))
    else:
        ok(g, "transaction cross-year duplicates", "none")

    # in _final a deal_id that groups one row groups nothing
    if table_exists(cur, "sgx_reit_property_transaction_final"):
        rows = q(cur, """select deal_id from sgx_reit_property_transaction_final
                         where deal_id is not null group by 1 having count(*)=1""")
        if rows:
            add(FAIL, g, "singleton deal_id in _final",
                f"{len(rows)} deal_id values group a single row: " + ", ".join(r[0] for r in rows[:5]))
        else:
            ok(g, "singleton deal_id in _final", "none")


def check_tallies(cur):
    g = "tallies"
    rows = q(cur, """select symbol, financial_year,
                       distributable_income_opening, income_for_year, other_additions,
                       distribution_paid, amount_retained, distributable_income_closing
                     from sgx_reit_performance""")
    tally = miss = 0
    bad = []
    for s, fy, o, i, a, p, r, c in rows:
        if o is None or i is None or c is None:
            miss += 1; continue
        calc = float(o) + float(i) + float(a or 0) - float(p or 0) - float(r or 0)
        if abs(calc - float(c)) > 1:
            bad.append((s, fy, round(calc), float(c)))
        else:
            tally += 1
    if bad:
        add(WARN, g, "distribution rollforward",
            f"{len(bad)} rows do not close: " +
            "; ".join(f"{s} FY{fy} calc {c1:,} vs closing {c2:,.0f}" for s, fy, c1, c2 in bad[:5]))
    ok(g, "distribution rollforward", f"{tally} close, {miss} have a null input, {len(bad)} off")

    # Rows with NO opening balance skip the check above entirely, and that blind spot hid a
    # real defect: J91U's distribution_paid held Note B's total cash out (167,921) instead of
    # the Distribution Statement's own deduction line (90,145), so 176,076 - paid missed the
    # closing balance by 78m and nothing noticed. Where opening is absent the statement is a
    # for-the-year one, so income + other - paid - retained must still reach closing.
    rows2 = q(cur, """select symbol, financial_year, income_for_year, other_additions,
                        distribution_paid, amount_retained, distributable_income_closing
                      from sgx_reit_performance
                      where distributable_income_opening is null
                        and distributable_income_closing is not null
                        and income_for_year is not null""")
    off2 = []
    for s2, fy, i, a, p2, r, c in rows2:
        calc = float(i) + float(a or 0) - float(p2 or 0) - float(r or 0)
        if abs(calc - float(c)) > 1:
            off2.append((s2, fy, round(calc), float(c)))
    if off2:
        add(WARN, g, "for-the-year rollforward (no opening balance)",
            f"{len(off2)} of {len(rows2)} rows do not close: " +
            "; ".join(f"{s2} FY{fy} calc {c1:,} vs closing {c2:,.0f}" for s2, fy, c1, c2 in off2[:5]))
    else:
        ok(g, "for-the-year rollforward (no opening balance)", f"{len(rows2)} close")

    rows = q(cur, "select symbol, financial_year, dpu, distribution_record from sgx_reit_performance")
    off = []
    for s, fy, dpu, rec in rows:
        if not rec or dpu is None:
            continue
        if (s, fy) in TRANCHE_EXCEPTIONS:
            continue
        tot = sum(t["dpu"] for t in rec if t.get("dpu") is not None)
        if abs(tot - float(dpu)) > 0.02:
            off.append((s, fy, round(tot, 3), float(dpu)))
    if off:
        add(FAIL, g, "sum(distribution_record.dpu) = dpu (raw, cents)",
            f"{len(off)} rows short: " +
            "; ".join(f"{s} FY{fy} record {a} vs dpu {b}" for s, fy, a, b in off[:6]) +
            ". Usually a semi-annual payer with only one half captured")
    else:
        ok(g, "sum(distribution_record.dpu) = dpu (raw, cents)",
           f"all tally ({len(TRANCHE_EXCEPTIONS)} excepted)")


def check_nulls(cur):
    """Columns prod declares NOT NULL. A null here fails the promote mid-scope, and because
    prod is delete-then-insert that leaves the scope empty."""
    g = "nulls"
    REQ = [("sgx_reit_property", ["symbol", "financial_year", "property_name", "category", "country"]),
           ("sgx_reit_top_tenant", ["symbol", "financial_year", "rank", "client_name"]),
           ("sgx_reit_trade_mix", ["symbol", "financial_year", "category", "pct_basis"]),
           ("sgx_reit_performance", ["symbol", "financial_year"]),
           ("sgx_reit_property_transaction", ["symbol", "financial_year", "transaction_type", "property_name"])]
    clean = True
    for t, cols in REQ:
        for c in cols:
            n = q(cur, f"select count(*) from {t} where {c} is null")[0][0]
            if n:
                clean = False
                add(FAIL, g, f"{t}.{c}", f"null on {n} rows; prod requires it NOT NULL")
    if clean:
        ok(g, "required columns", "no nulls")


def check_currency(cur):
    """Raw stores money natively with a per-figure currency tag. An untagged value silently
    inherits the row currency at build time, which is how a JPY figure became SGD once."""
    g = "currency"
    PAIRS = [("sgx_reit_property", "market_valuation", "market_valuation_currency"),
             ("sgx_reit_property", "purchase_price", "purchase_price_currency"),
             ("sgx_reit_property", "gross_revenue", "gross_revenue_currency"),
             ("sgx_reit_property_transaction", "purchase_price", "purchase_price_currency"),
             ("sgx_reit_property_transaction", "sale_price", "sale_price_currency"),
             ("sgx_reit_property_transaction", "basis_value", "basis_currency")]
    clean = True
    for t, val, ccy in PAIRS:
        n = q(cur, f"select count(*) from {t} where {val} is not null and {ccy} is null")[0][0]
        if n:
            clean = False
            add(WARN, g, f"{t}.{val}", f"{n} values carry no {ccy}; they inherit the row currency")
    # a price and its basis in different currencies cannot be subtracted
    n = q(cur, """select count(*) from sgx_reit_property_transaction
                  where sale_price is not null and basis_value is not null
                    and sale_price_currency is not null and basis_currency is not null
                    and sale_price_currency <> basis_currency""")[0][0]
    if n:
        clean = False
        add(FAIL, g, "transaction price vs basis currency",
            f"{n} rows compare across currencies; the derived gain would be meaningless")
    if clean:
        ok(g, "currency tags", "every money figure tagged, no cross-currency comparisons")


def check_coverage(cur):
    """A REIT-year present in one table but absent from another. performance is the spine:
    build_final_tables.py reads FY-end dates from it for every FX conversion."""
    g = "coverage"
    spine = {(s, fy) for s, fy in q(cur, "select symbol, financial_year from sgx_reit_performance")}
    for t in ("sgx_reit_property", "sgx_reit_top_tenant", "sgx_reit_trade_mix",
              "sgx_reit_property_transaction"):
        col = "symbol"
        rows = {(s if s.endswith(".SI") else s + ".SI", fy)
                for s, fy in q(cur, f"select distinct {col}, financial_year from {t}")}
        orphan = sorted(rows - spine)
        if orphan:
            add(FAIL, g, t, f"{len(orphan)} REIT-years have no sgx_reit_performance row, so no "
                            f"FY-end date and no FX anchor: " +
                            ", ".join(f"{s} FY{fy}" for s, fy in orphan[:6]))
        else:
            ok(g, t, f"{len(rows)} REIT-years all present in performance")


def check_final_sync(cur):
    """*_final is rebuilt wholesale from raw. A row-count gap means the build did not run
    after the last raw change."""
    g = "final_sync"
    PAIRS = [("sgx_reit_profile", "sgx_reit_profile_final"),
             ("sgx_reit_performance", "sgx_reit_performance_final"),
             ("sgx_reit_property", "sgx_reit_property_final"),
             ("sgx_reit_top_tenant", "sgx_reit_top_tenant_final"),
             ("sgx_reit_trade_mix", "sgx_reit_trade_mix_final"),
             ("sgx_reit_property_transaction", "sgx_reit_property_transaction_final")]
    for raw, fin in PAIRS:
        if not table_exists(cur, fin):
            add(FAIL, g, fin, "table missing; run build_final_tables.py --write")
            continue
        a = q(cur, f"select count(*) from {raw}")[0][0]
        b = q(cur, f"select count(*) from {fin}")[0][0]
        if a != b:
            add(FAIL, g, fin, f"raw has {a} rows, _final has {b}. Rebuild with "
                              f"build_final_tables.py --write")
        else:
            ok(g, fin, f"{b} rows, in sync")


def check_segment_consistency(cur):
    """A REIT that discloses segmented tenant or trade tables in one year almost always does
    so in the next. basis_segment present in FY(N) and absent in FY(N+1) with a similar row
    count is what the 2026-08-03 remap missed."""
    g = "segments"
    for t in ("sgx_reit_top_tenant", "sgx_reit_trade_mix"):
        rows = q(cur, f"""select symbol, financial_year,
                            count(*) filter (where basis_segment is not null) seg, count(*) n
                          from {t} group by 1,2""")
        by_sym = {}
        for s, fy, seg, n in rows:
            by_sym.setdefault(s, {})[fy] = (seg, n)
        flagged = []
        for s, years in by_sym.items():
            tagged = {fy for fy, (seg, _n) in years.items() if seg}
            if not tagged:
                continue
            for fy, (seg, n) in years.items():
                if seg == 0 and n >= 0.8 * max(x[1] for x in years.values()):
                    flagged.append(f"{s} FY{fy} ({n} rows, none tagged; FY{sorted(tagged)[0]} is)")
        if flagged:
            add(WARN, g, t, "segments tagged in one year but not another: " + "; ".join(flagged[:6]))
        else:
            ok(g, t, "segment tagging consistent across years")


def check_property_money(cur):
    """Property money in _final must be SGD. H1 compares a property's purchase price with its
    own valuation: both are S$ figures for the same asset, so they sit within a few times of
    each other. A price left in a large-unit currency (JPY, IDR, KRW, VND, INR, CNY) is 5-12,000x
    off. Revenue is not used: new acquisitions and vacant buildings have near-zero revenue.
    An unconverted EUR/GBP/USD/AUD figure is only 1.3-1.8x off and cannot be seen here; the
    purchase_price_currency tag audit covers those. H2 catches a year-on-year jump that only a
    unit or FX error explains."""
    g = "property_money"
    rows = q(cur, """select symbol, financial_year, property_name, market_valuation, purchase_price
                     from sgx_reit_property_final
                     where market_valuation > 0 and purchase_price > 0""")
    bad = [(s, fy, pn, round(float(pp) / float(mv), 1)) for s, fy, pn, mv, pp in rows
           if not 1 / 30 <= float(pp) / float(mv) <= 30]
    if bad:
        add(FAIL, g, "H1 purchase_price within 30x of market_valuation", f"{len(bad)} rows, e.g. {bad[:4]}")
    else:
        ok(g, "H1 purchase_price within 30x of market_valuation", f"{len(rows)} properties")
    rows = q(cur, """select a.symbol, a.property_name, a.financial_year, a.market_valuation, b.market_valuation
                     from sgx_reit_property_final a join sgx_reit_property_final b
                       on a.symbol = b.symbol and a.property_name = b.property_name
                      and b.financial_year = a.financial_year + 1
                     where a.market_valuation > 0 and b.market_valuation > 0""")
    jumps = [(s, pn, fy) for s, pn, fy, v1, v2 in rows
             if max(float(v1), float(v2)) / min(float(v1), float(v2)) > 5]
    if jumps:
        add(FAIL, g, "H2 valuation YoY change <= 5x", f"{len(jumps)} pairs, e.g. {jumps[:4]}")
    else:
        ok(g, "H2 valuation YoY change <= 5x", f"{len(rows)} year pairs")


STRATA_ROWS = [("P40U.SI", "Ngee Ann City Property"), ("P40U.SI", "Wisma Atria Property"),
               ("T82U.SI", "Suntec City Office Towers")]


def check_property_basis(cur):
    """market_valuation and property NPI are stored at 100% of the asset, so the REIT's share is
    value x ownership. A share-basis value cannot be detected from the data alone (valuations move
    year to year), so these checks cover the known cases:
      - strata rows: the REIT owns its units outright, so ownership must be 1
      - JYEU Parkway Parade: a 10% equity stake; the report gives no property value, so null"""
    g = "property_basis"
    bad = []
    for sym, pn in STRATA_ROWS:
        for fy, own in q(cur, """select financial_year, ownership from sgx_reit_property_final
                                 where symbol=%s and property_name=%s""", (sym, pn)):
            if own is None or abs(float(own) - 1) > 1e-6:
                bad.append((sym, fy, pn, own))
    if bad:
        add(FAIL, g, "strata rows have ownership 1", f"{len(bad)} rows, e.g. {bad[:4]}")
    else:
        ok(g, "strata rows have ownership 1")
    n = q(cur, """select count(*) from sgx_reit_property_final where symbol='JYEU.SI'
                  and property_name ilike 'Parkway Parade%%' and market_valuation is not null""")[0][0]
    if n:
        add(FAIL, g, "JYEU Parkway Parade has no property valuation",
            f"{n} rows hold the equity-stake value as market_valuation")
    else:
        ok(g, "JYEU Parkway Parade has no property valuation")


# canonical country name -> ISO 3166-1 alpha-2, lowercase (the code flagcdn expects:
# https://flagcdn.com/w40/<code>.png). One name, one code, so the frontend can show a flag.
COUNTRY_CODE = {
    "Australia": "au", "Belgium": "be", "Canada": "ca", "China": "cn", "Czech Republic": "cz",
    "Denmark": "dk", "Finland": "fi", "France": "fr", "Germany": "de", "Hong Kong": "hk",
    "India": "in", "Indonesia": "id", "Ireland": "ie", "Italy": "it", "Japan": "jp",
    "Malaysia": "my", "Maldives": "mv", "Netherlands": "nl", "New Zealand": "nz",
    "Philippines": "ph", "Poland": "pl", "Singapore": "sg", "Slovakia": "sk",
    "South Korea": "kr", "Spain": "es", "Switzerland": "ch", "United Kingdom": "gb",
    "United States": "us", "Vietnam": "vn",
}


# REIT-years whose tranches cannot add up to DPU yet, with the reason. Waivers key on
# (group, check) and would hide every other failure of the same check, so the exceptions
# live here instead.
TRANCHE_EXCEPTIONS = {
    ("A17U.SI", 2025): "H2 2025 tranche is paid in 2026, so only the FY2026 AR prints it",
}


def check_tranches_final(cur):
    """Same check as in tallies, on _final where DPU is SGD dollars (D-6), so the tolerance
    is 100x smaller."""
    g = "tranches"
    off = []
    for s, fy, dpu, rec in q(cur, """select symbol, financial_year, distribution_per_unit, distribution_record
                                     from sgx_reit_performance_final"""):
        if not rec or dpu is None or (s, fy) in TRANCHE_EXCEPTIONS:
            continue
        tot = sum(t["dpu"] for t in rec if t.get("dpu") is not None)
        if abs(tot - float(dpu)) > 0.0002:
            off.append((s, fy, round(tot, 6), float(dpu)))
    if off:
        add(FAIL, g, "sum(distribution_record.dpu) = dpu (final, SGD)",
            f"{len(off)} rows: " + "; ".join(f"{s} FY{fy} record {a} vs dpu {b}" for s, fy, a, b in off[:6]))
    else:
        ok(g, "sum(distribution_record.dpu) = dpu (final, SGD)", f"{len(TRANCHE_EXCEPTIONS)} excepted")


def check_rollforward_final(cur):
    """closing = opening + income_for_year + other_additions - distribution_paid - amount_retained,
    on _final, to S$1,000. other_additions is signed (D-10), so C2PU's rounding adjustment is
    inside it. Rows missing a term are skipped; check_tallies covers the raw table."""
    g = "rollforward_final"
    rows = q(cur, """select symbol, financial_year, distributable_income_opening, income_for_year,
                            other_additions, distribution_paid, amount_retained, distributable_income_closing
                     from sgx_reit_performance_final""")
    off, checked = [], 0
    for s, fy, o, i, a, p, r, c in rows:
        if None in (o, i, p, c):
            continue
        checked += 1
        calc = float(o) + float(i) + float(a or 0) - float(p) - float(r or 0)
        if abs(calc - float(c)) > 1000:
            off.append((s, fy, round(calc), float(c)))
    if off:
        add(FAIL, g, "distribution rollforward closes",
            f"{len(off)} of {checked} rows: " +
            "; ".join(f"{s} FY{fy} calc {a:,} vs closing {b:,.0f}" for s, fy, a, b in off[:5]))
    else:
        ok(g, "distribution rollforward closes", f"{checked} rows close")


def check_countries(cur):
    """Every country must be one canonical name that maps to one flag code."""
    g = "countries"
    bad = q(cur, """select country, count(*) from sgx_reit_property_final
                    where country is not null and country <> ALL(%s) group by 1 order by 2 desc""",
            (list(COUNTRY_CODE),))
    if bad:
        add(FAIL, g, "country in the canonical list", f"{len(bad)} values: {[(c, n) for c, n in bad]}")
    else:
        ok(g, "country in the canonical list", f"{len(COUNTRY_CODE)} names, each with a flag code")


def check_transactions(cur):
    """D-9: transaction_price and basis_value are gross (before debt) and for the stake
    transacted. A price for the stake against a 100% basis, or a net-of-debt price against a
    gross basis, shows up as a ratio far from 1 (C38U ION Orchard sat at 0.29)."""
    g = "transactions"
    rows = q(cur, """select symbol, financial_year, property_name, interest_pct,
                            transaction_price, basis_value
                     from sgx_reit_property_transaction_final
                     where transaction_price > 0 and basis_value > 0""")
    bad = [(s, fy, pn, round(float(p) / float(b), 2)) for s, fy, pn, ip, p, b in rows
           if not 0.5 <= float(p) / float(b) <= 2]
    if bad:
        add(FAIL, g, "price within 0.5-2x of basis_value", f"{len(bad)} rows, e.g. {bad[:4]}")
    else:
        ok(g, "price within 0.5-2x of basis_value", f"{len(rows)} rows with both figures")


GROUPS = {
    "scale": check_scale,
    "enums": check_enums,
    "mapping": check_mapping_ran,
    "sums": check_sums,
    "keys": check_keys,
    "tallies": check_tallies,
    "nulls": check_nulls,
    "currency": check_currency,
    "coverage": check_coverage,
    "final_sync": check_final_sync,
    "segments": check_segment_consistency,
    "property_money": check_property_money,
    "property_basis": check_property_basis,
    "transactions": check_transactions,
    "countries": check_countries,
    "tranches": check_tranches_final,
    "rollforward_final": check_rollforward_final,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma list of groups")
    ap.add_argument("--list", action="store_true", help="list group names and exit")
    ap.add_argument("--quiet", action="store_true", help="suppress PASS lines")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    ap.add_argument("--github", action="store_true",
                    help="also emit ::error::/::warning:: annotations for GitHub Actions")
    ap.add_argument("--markdown", metavar="PATH",
                    help="write a markdown summary table to PATH (for $GITHUB_STEP_SUMMARY)")
    args = ap.parse_args()

    if args.list:
        for k in GROUPS:
            print(k)
        return 0

    selected = [k.strip() for k in args.only.split(",")] if args.only else list(GROUPS)
    unknown = [k for k in selected if k not in GROUPS]
    if unknown:
        print(f"unknown group(s): {unknown}. Known: {list(GROUPS)}")
        return 2

    cn = psycopg2.connect(os.environ["SUPABASE_CONNECTION_STRING"])
    cn.autocommit = True   # one bad statement must not abort every later check
    cur = cn.cursor()

    for name in selected:
        try:
            GROUPS[name](cur)
        except Exception as exc:
            add(FAIL, name, "check crashed", f"{type(exc).__name__}: {exc}")

    fails = [f for f in findings if f[0] == FAIL]
    warns = [f for f in findings if f[0] == WARN]

    if args.json:
        print(json.dumps({"fail": [dict(zip(("severity", "group", "check", "message"), f)) for f in fails],
                          "warn": [dict(zip(("severity", "group", "check", "message"), f)) for f in warns],
                          "waived": [dict(zip(("group", "check", "message", "reason"), w)) for w in waived],
                          "passed": len(passes)}, indent=2))
        return 1 if fails else 0

    width = 78
    print("=" * width)
    print(f"DB GUARD  groups: {', '.join(selected)}")
    print("=" * width)

    if not args.quiet:
        cur_group = None
        for grp, check, note in passes:
            if grp != cur_group:
                print(f"\n[{grp}]")
                cur_group = grp
            print(f"  ok    {check}" + (f"  ({note})" if note else ""))

    if waived:
        print(f"\n{'-' * width}\nWAIVED  ({len(waived)})\n{'-' * width}")
        for grp, check, msg, reason in waived:
            print(f"  [{grp}] {check}")
            for line in _wrap(msg, width - 8):
                print(f"        {line}")
            for line in _wrap("reason: " + reason, width - 8):
                print(f"        {line}")

    for sev, items in ((WARN, warns), (FAIL, fails)):
        if not items:
            continue
        print(f"\n{'-' * width}\n{sev}  ({len(items)})\n{'-' * width}")
        for _s, grp, check, msg in items:
            print(f"  [{grp}] {check}")
            for line in _wrap(msg, width - 8):
                print(f"        {line}")

    # ---- per-group tally, so a long log still ends with something readable ----
    tally = {g: {"ok": 0, "waived": 0, WARN: 0, FAIL: 0} for g in selected}
    for grp, _c, _n in passes:
        tally.setdefault(grp, {"ok": 0, "waived": 0, WARN: 0, FAIL: 0})["ok"] += 1
    for grp, _c, _m, _r in waived:
        tally.setdefault(grp, {"ok": 0, "waived": 0, WARN: 0, FAIL: 0})["waived"] += 1
    for sev, grp, _c, _m in findings:
        tally.setdefault(grp, {"ok": 0, "waived": 0, WARN: 0, FAIL: 0})[sev] += 1

    print(f"\n{'=' * width}")
    print("SUMMARY BY GROUP")
    print(f"{'-' * width}")
    print(f"  {'group':<14}{'ok':>6}{'waived':>8}{'warn':>7}{'fail':>7}   status")
    for g in selected:
        t = tally.get(g, {"ok": 0, "waived": 0, WARN: 0, FAIL: 0})
        status = "FAIL" if t[FAIL] else ("warn" if t[WARN] else "pass")
        print(f"  {g:<14}{t['ok']:>6}{t['waived']:>8}{t[WARN]:>7}{t[FAIL]:>7}   {status}")

    print(f"{'-' * width}")
    print(f"  {'TOTAL':<14}{len(passes):>6}{len(waived):>8}{len(warns):>7}{len(fails):>7}")
    print(f"{'=' * width}")
    print("GUARD: FAIL" if fails else "GUARD: PASS")

    # ---- GitHub Actions annotations: findings show up on the run, not only in the log ----
    if args.github:
        for _s2, grp, check, msg in fails:
            print(f"::error title=db-guard {grp}/{check}::{_flat(msg)}")
        for _s2, grp, check, msg in warns:
            print(f"::warning title=db-guard {grp}/{check}::{_flat(msg)}")

    if args.markdown:
        with open(args.markdown, "a", encoding="utf-8") as fh:
            fh.write(f"## DB guard: {'FAIL' if fails else 'PASS'}\n\n")
            fh.write(f"`{len(passes)} passed · {len(waived)} waived · "
                     f"{len(warns)} warnings · {len(fails)} failures`\n\n")
            fh.write("| group | ok | waived | warn | fail |\n|---|---:|---:|---:|---:|\n")
            for g in selected:
                t = tally.get(g, {"ok": 0, "waived": 0, WARN: 0, FAIL: 0})
                mark = " **FAIL**" if t[FAIL] else ""
                fh.write(f"| {g}{mark} | {t['ok']} | {t['waived']} | {t[WARN]} | {t[FAIL]} |\n")
            for label, items in (("Failures", fails), ("Warnings", warns)):
                if not items:
                    continue
                fh.write(f"\n### {label}\n\n")
                for _s2, grp, check, msg in items:
                    fh.write(f"- **{grp} / {check}** — {_flat(msg)}\n")
            if waived:
                fh.write("\n### Waived\n\n")
                for grp, check, msg, reason in waived:
                    fh.write(f"- **{grp} / {check}** — {_flat(msg)}  \n  _{_flat(reason)}_\n")

    return 1 if fails else 0


def _flat(text):
    """GitHub annotations are one line; collapse whitespace and cap the length."""
    t = " ".join(str(text).split())
    return t if len(t) <= 900 else t[:897] + "..."


def _wrap(text, w):
    out, line = [], ""
    for word in str(text).split():
        if len(line) + len(word) + 1 > w:
            out.append(line); line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


if __name__ == "__main__":
    sys.exit(main())
