"""Promote dev sgx_reit_*_final  ->  prod sgx_reit_* (PostgREST).

Encodes the transforms established by scripts/db/_compare_final_vs_prod.py +
_compare_values.py (dev *_final vs prod, verified same-report on M44U FY2024):

  symbol            kept as-is, with the .SI suffix         (all tables; prod uses .SI)
  percentages       NOT converted here. Every percentage is normalized to 0-1 once, in
                    build_final_tables.py pct01(), so _final and prod agree and this is a
                    pass-through. FRACTION_FIELDS is empty -- see the note on it below.
  properties_location  "A, B, C" / "A; B; C" (text) -> "[A, B, C]" (bracketed text,
                       prod's stored format; separators normalized to ", ")   (performance)
  date columns      coerced to 'YYYY-MM-DD' or NULL per prod column type
  text-typed numerics (e.g. property_transaction.purchase_price) -> string
  bigint columns    ROUNDED, not truncated (prod holds money as bigint; FX leaves fractions)
  dev-only columns  (source_type, announcement_refs) -> DROPPED
                    (prod schema is the target; we only send prod's columns)

NOT transformed (kept as-is, verified): all money & areas (absolute), and the KPIs that
are NOT percentages -- interest_coverage_ratio (a multiple), WALE, weighted_average_debt_
maturity (years), NAV, DPU (money).

REQUIRES schema/migrations/2026-08-04_prod_schema_sync.sql to have been applied. This
script does DATA only -- transform_row() emits just the columns prod already has, so a
column added in _final (deal_id, basis_value, basis, basis_segment, units_in_issue,
income_for_year, distribution_declared, amount_retained, other_additions)
is dropped SILENTLY until that migration runs.

sgx_reit_financial_final has NO prod counterpart (financials live in
sgx_manual_input) -> it is not promoted here.

Write model (prod is REST-only, non-transactional):
  - profile:      scope = symbol           -> DELETE then POST (re-insert)
  - all others:   scope = (symbol, fy)     -> DELETE then POST (re-insert)
  This mirrors dev's delete-then-insert-per-scope semantics. NOTE: DELETE and POST
  are separate REST calls; a failure between them leaves that scope empty until re-run.

Other writers: another pipeline adds rows to prod directly (e.g. new transactions).
  - Orphan scopes (in prod, not in _final) are only reported. Deleting them needs
    --delete-orphans.
  - For tables in PROTECT_KEYS, a scope that holds a prod row with no matching dev row
    is PROTECTED: it is skipped, not deleted.

Usage:
  python scripts/db/promote_final_to_prod.py                          # DRY, all rows
  python scripts/db/promote_final_to_prod.py --symbols M44U,ME8U,N2IU --fy 2025
  python scripts/db/promote_final_to_prod.py --symbols M44U,ME8U,N2IU --fy 2025 --write
  python scripts/db/promote_final_to_prod.py --tables sgx_reit_property --fy 2025 --write

Read dev = direct Postgres; write prod = REST GET/DELETE/POST (service key).
DRY by default; nothing hits prod without --write.
"""
import os, sys, re, json, argparse, datetime, decimal, urllib.request, urllib.error
import psycopg2
from dotenv import load_dotenv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------- config ----------
PAIRS = [  # (dev_final, prod, scope_cols)
    ("sgx_reit_profile_final",              "sgx_reit_profile",              ["symbol"]),
    ("sgx_reit_performance_final",          "sgx_reit_performance",          ["symbol", "financial_year"]),
    ("sgx_reit_property_final",             "sgx_reit_property",             ["symbol", "financial_year"]),
    ("sgx_reit_top_tenant_final",           "sgx_reit_top_tenant",           ["symbol", "financial_year"]),
    ("sgx_reit_trade_mix_final",            "sgx_reit_trade_mix",            ["symbol", "financial_year"]),
    ("sgx_reit_property_transaction_final", "sgx_reit_property_transaction", ["symbol", "financial_year"]),
]
# EMPTY, deliberately (2026-08-04). Percentages are normalized to 0-1 ONCE, in
# build_final_tables.py's pct01(), so _final and prod now agree and promotion is a
# pass-through. Converting here as well would divide by 100 twice.
#
# This also fixes a latent 100x bug: interest_pct was in this set but _final already held
# it as a fraction (CY6U's "20.2% stake" = 0.202), so promoting would have written 0.00202.
# Prod still shows the damage on one row -- AJBU FY2025 KDC SGP 7 & 8 reads 0.0051 where the
# report says 51%.
#
# The next promote also RE-WRITES prod's three remaining 0-100 columns to 0-1:
# performance.aggregate_leverage, cost_of_debt and portfolio_occupancy. Any consumer
# formatting those as already-percent needs a x100 at the display layer.
FRACTION_FIELDS = set()
# EMPTY since 2026-09-18: properties_location is a text[] in _final and in prod (the
# migration converts prod's bracketed text), so a Python list is sent as a JSON array and
# no bracketed-string transform is left.
BRACKET_TEXT_FIELDS = set()
# prod column name -> dev *_final column name (where they differ).
# Coordinates: dev uses coordinate_* ; prod uses bare latitude/longitude.
# coordinate_source is RAW-only (geocoder provenance) and never reaches _final.
COLUMN_ALIAS = {"latitude": "coordinate_latitude", "longitude": "coordinate_longitude"}

# Rows _final holds but prod must not show. Prod is investor-facing: a transaction is a
# fact only once it has completed. announced deals can be repriced or abandoned, and they
# carry no completed_date, so their money cannot even be FX-converted (see the NULLED
# warnings in build_final_tables.py). _final keeps them as the full record.
#
# IMPORTANT: the filter is applied AFTER scopes are grouped, never before. Two scopes --
# N2IU FY2023 and O5RU FY2024 -- contain ONLY non-completed rows. Filtering first would
# drop those scope keys entirely, so the promote would never DELETE them and their stale
# rows would survive in prod indefinitely. Grouping first means the scope is still
# visited, emptied, and left empty.
ROW_FILTERS = {
    "sgx_reit_property_transaction": lambda r: r.get("status") == "completed",
}

# Row keys used to find prod rows that another pipeline wrote and dev does not have.
# A scope holding such a row is skipped, so the DELETE never removes it.
PROTECT_KEYS = {
    "sgx_reit_property_transaction": ["transaction_type", "property_name"],
}


def parse_args():
    ap = argparse.ArgumentParser(description="Promote dev sgx_reit_*_final -> prod sgx_reit_*.")
    ap.add_argument("--symbols", help="comma list of .SI or bare tickers (default: all)")
    ap.add_argument("--fy", type=int, help="single financial_year to promote (default: all)")
    ap.add_argument("--tables", help="comma list of prod table names to limit to (default: all)")
    ap.add_argument("--write", action="store_true", help="apply to prod (default: dry run)")
    ap.add_argument("--delete-orphans", action="store_true",
                    help="with --write on a full promote: delete prod scopes that _final does not have")
    ap.add_argument("--limit-preview", type=int, default=1, help="sample rows to print in dry run")
    return ap.parse_args()


# ---------- json coercion ----------
def jsonable(v):
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()[:10]
    return v


_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_FMTS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y")
_DATE_DROPS = []  # (col, raw) for dev values that are NOT valid dates -> nulled (can't cast to prod date)

def to_date_str(v):
    """Return a strict 'YYYY-MM-DD' or None. Non-date text (bare years, prose) -> None,
    because prod's column is typed `date` and Postgres rejects a non-date string."""
    if v is None:
        return None
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()[:10]
    s = str(v).strip()
    if not s:
        return None
    head = s[:10]
    if _DATE_RX.match(head):
        try:
            datetime.date.fromisoformat(head); return head
        except ValueError:
            pass
    for fmt in _DATE_FMTS:
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def coerce(col, ptype, v):
    """Transform one dev value to its prod representation."""
    if v is None:
        return None
    if col == "symbol":
        return str(v)
    if col in FRACTION_FIELDS:
        return round(float(v) / 100.0, 8)
    if ptype == "date":
        ds = to_date_str(v)
        if ds is None and v is not None:
            _DATE_DROPS.append((col, str(v)))
        return ds
    if ptype in ("text", "character varying", "character"):
        # text-typed numerics (e.g. purchase_price) must serialize as string
        return jsonable(v) if isinstance(v, (dict, list)) else str(jsonable(v))
    if ptype in ("numeric", "real", "double precision", "number"):
        return float(v)
    if ptype in ("integer", "bigint", "smallint"):
        # ROUND, not int(): prod holds money as bigint, and FX conversion leaves fractions
        # on ~35 figures (income_for_year, amount_retained, basis_value, purchase_price...).
        # int() truncates toward zero, so 1_638_000.9999 would land as 1_638_000.
        return int(round(float(v)))
    if ptype == "boolean":
        return bool(v)
    # jsonb / json / array -> pass through (already json-serializable dict/list)
    return jsonable(v)


# ---------- prod REST ----------
class Prod:
    def __init__(self):
        self.url = os.environ["SUPABASE_URL"].rstrip("/")
        self.key = os.environ["SUPABASE_KEY"]
        self.h = {"apikey": self.key, "Authorization": "Bearer " + self.key}

    def defs(self):
        req = urllib.request.Request(self.url + "/rest/v1/", headers=self.h)
        return json.load(urllib.request.urlopen(req)).get("definitions", {})

    def col_types(self, table, defs):
        props = defs.get(table, {}).get("properties", {})
        return {c: (m.get("format") or m.get("type")) for c, m in props.items()}

    def _scope_query(self, scope):
        parts = []
        for k, val in scope.items():
            parts.append(f"{k}=eq.{val}")
        return "&".join(parts)

    def count(self, table, scope):
        q = self._scope_query(scope)
        req = urllib.request.Request(f"{self.url}/rest/v1/{table}?{q}&select=count", headers=self.h)
        try:
            return json.load(urllib.request.urlopen(req))[0]["count"]
        except Exception:
            return None

    def delete(self, table, scope):
        q = self._scope_query(scope)
        req = urllib.request.Request(f"{self.url}/rest/v1/{table}?{q}", method="DELETE",
                                     headers={**self.h, "Prefer": "return=minimal"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status

    def scopes_present(self, table, scope_cols):
        """Every distinct scope tuple prod currently holds, for the orphan sweep."""
        sel = ",".join(scope_cols)
        out, off = set(), 0
        while True:
            req = urllib.request.Request(
                f"{self.url}/rest/v1/{table}?select={sel}&offset={off}&limit=1000", headers=self.h)
            page = json.load(urllib.request.urlopen(req, timeout=120))
            for row in page:
                out.add(tuple((c, row[c]) for c in scope_cols))
            if len(page) < 1000:
                return out
            off += 1000

    def rows(self, table, scope, cols):
        req = urllib.request.Request(
            f"{self.url}/rest/v1/{table}?{self._scope_query(scope)}&select={','.join(cols)}",
            headers=self.h)
        return json.load(urllib.request.urlopen(req, timeout=120))

    def insert(self, table, rows):
        body = json.dumps(rows).encode("utf-8")
        req = urllib.request.Request(f"{self.url}/rest/v1/{table}", data=body, method="POST",
                                     headers={**self.h, "Content-Type": "application/json",
                                              "Prefer": "return=minimal"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status


# ---------- dev read + transform ----------
def dev_read(cur, final_table, symbols_si, fy):
    where, args = [], []
    if symbols_si:
        where.append("symbol = ANY(%s)"); args.append(symbols_si)
    if fy is not None:
        # profile has no financial_year column
        cur.execute("select 1 from information_schema.columns where table_name=%s and column_name='financial_year'",
                    (final_table,))
        if cur.fetchone():
            where.append("financial_year = %s"); args.append(fy)
    sql = f"select * from {final_table}"
    if where:
        sql += " where " + " and ".join(where)
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def transform_row(row, prod_types):
    out = {}
    for col, ptype in prod_types.items():
        src = COLUMN_ALIAS.get(col, col)      # prod name -> dev *_final name
        out[col] = coerce(col, ptype, row.get(src))
    return out


def scope_of(prod_row, scope_cols):
    return {k: prod_row[k] for k in scope_cols}


def aggregate_trade_mix(rows):
    """Prod sgx_reit_trade_mix PK = (symbol, financial_year, category, pct_basis,
    basis_segment). Dev *_final can hold several rows collapsing to the same canonical
    category (distinct category_raw). Sum their pct into one row to satisfy the PK
    (matches the original prod promotion, which stores aggregated categories).

    basis_segment is part of the key and MUST stay there. T82U discloses office and
    retail sector tables against SEPARATE denominators, each summing to ~100% within
    its segment. Both carry pct_basis='gross_rental_income' since the 2026-08-03 remap,
    so without basis_segment in this key the two segments land on one key and their
    percentages are ADDED -- a trade mix summing to ~200%. Same for BUOU's
    commercial / logistics_industrial split."""
    agg = {}
    for r in rows:
        k = (r.get("symbol"), r.get("financial_year"), r.get("category"),
             r.get("pct_basis"), r.get("basis_segment"))
        if k in agg:
            if r.get("pct") is not None:
                agg[k]["pct"] = round((agg[k].get("pct") or 0) + r["pct"], 8)
        else:
            agg[k] = dict(r)
    return list(agg.values())


def assert_segment_promotable(dev_rows, prod_types, prod_table):
    """transform_row() only emits columns prod already has, so if prod is missing
    basis_segment the value is dropped SILENTLY -- and for trade_mix the two segments
    then collide in aggregate_trade_mix() and get summed. Refuse to promote instead.

    Clears once §3 of schema/migrations/2026-08-03_basis_segment.sql is applied to prod.
    """
    if "basis_segment" in prod_types:
        return
    n = sum(1 for r in dev_rows if r.get("basis_segment"))
    if not n:
        return
    segs = sorted({r["basis_segment"] for r in dev_rows if r.get("basis_segment")})
    syms = sorted({str(r.get("symbol")) for r in dev_rows if r.get("basis_segment")})
    raise SystemExit(
        f"\nABORT: {prod_table} -- prod has no basis_segment column, but {n} dev rows "
        f"carry one ({', '.join(segs)}; {', '.join(syms)}).\n"
        f"       Promoting now would drop the segment and, for trade_mix, SUM the "
        f"separate-denominator segments into one row (~200% totals).\n"
        f"       Apply §3 of schema/migrations/2026-08-03_basis_segment.sql to prod first.")


def disambiguate_txn(rows):
    """Prod sgx_reit_property_transaction PK = (symbol, financial_year, transaction_type,
    property_name). A property can legitimately have >1 transaction of the same type in a year
    (e.g. DCRU's Frankfurt Facility staged acquisition: Apr +24.9% then Dec +15.1%). Append a
    " (2)", " (3)"... suffix to property_name on the 2nd+ collision so all rows survive the PK."""
    seen = {}
    for r in rows:
        k = (r.get("symbol"), r.get("financial_year"), r.get("transaction_type"), r.get("property_name"))
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > 1 and r.get("property_name"):
            r["property_name"] = f"{r['property_name']} ({seen[k]})"
    return rows


def main():
    args = parse_args()
    load_dotenv(".env")
    symbols_si = None
    if args.symbols:
        symbols_si = [s if s.endswith(".SI") else s + ".SI" for s in args.symbols.split(",")]
    only_tables = set(args.tables.split(",")) if args.tables else None

    dev = psycopg2.connect(os.environ["SUPABASE_CONNECTION_STRING"]); cur = dev.cursor()
    prod = Prod(); defs = prod.defs()

    print("=" * 88)
    print(f"PROMOTE dev *_final -> prod   mode={'WRITE' if args.write else 'DRY RUN'}"
          f"  symbols={args.symbols or 'ALL'}  fy={args.fy or 'ALL'}")
    print("=" * 88)

    for final_table, prod_table, scope_cols in PAIRS:
        if only_tables and prod_table not in only_tables:
            continue
        prod_types = prod.col_types(prod_table, defs)
        rows = dev_read(cur, final_table, symbols_si, args.fy)
        _dstart = len(_DATE_DROPS)
        if prod_table in ("sgx_reit_trade_mix", "sgx_reit_top_tenant"):
            assert_segment_promotable(rows, prod_types, prod_table)
        prod_rows = [transform_row(r, prod_types) for r in rows]
        if prod_table == "sgx_reit_trade_mix":
            prod_rows = aggregate_trade_mix(prod_rows)
        if prod_table == "sgx_reit_property_transaction":
            prod_rows = disambiguate_txn(prod_rows)

        # group by scope for delete-then-insert
        scopes = {}
        for pr in prod_rows:
            key = tuple(scope_of(pr, scope_cols).items())
            scopes.setdefault(key, []).append(pr)

        # filter AFTER grouping -- see ROW_FILTERS. A scope that empties out still gets
        # its DELETE, which is the whole point.
        keep = ROW_FILTERS.get(prod_table)
        if keep:
            before = sum(len(g) for g in scopes.values())
            scopes = {k: [r for r in g if keep(r)] for k, g in scopes.items()}
            after = sum(len(g) for g in scopes.values())
            emptied = [dict(k) for k, g in scopes.items() if not g]
            print(f"    FILTER {prod_table}: {before} -> {after} rows "
                  f"({before - after} not completed, held back from prod)")
            if emptied:
                print(f"    scopes emptied by the filter (deleted, then left empty): {emptied}")

            # deal_id means "this price is SHARED -- group before summing". _final assigns it
            # from group size across all 212 rows, but the completed-only filter above can
            # strip a sibling and leave the survivor holding a deal_id that groups nothing.
            # 7 such singletons reached prod (e.g. m44u:flexhub -- its FY2023 half is still
            # 'announced'). Recompute AFTER filtering, over the rows prod will actually hold.
            if "deal_id" in prod_types:
                n_by_deal = {}
                for g in scopes.values():
                    for r in g:
                        if r.get("deal_id"):
                            n_by_deal[r["deal_id"]] = n_by_deal.get(r["deal_id"], 0) + 1
                singles = {d for d, n in n_by_deal.items() if n == 1}
                if singles:
                    for g in scopes.values():
                        for r in g:
                            if r.get("deal_id") in singles:
                                r["deal_id"] = None
                    print(f"    deal_id cleared on {len(singles)} rows whose deal lost its "
                          f"sibling to the filter (a deal of one groups nothing)")

        print(f"\n### {final_table} -> {prod_table}")
        print(f"    dev rows selected: {len(rows)}   scopes: {len(scopes)}   "
              f"(prod cols sent: {len(prod_types)})")
        # rows actually sent, after the row filter and trade-mix aggregation
        print(f"    TOTAL to send: {sum(len(g) for g in scopes.values())} rows in {len(scopes)} scopes")
        _mapped = set(COLUMN_ALIAS.values())
        dropped = [c for c in (rows[0].keys() if rows else [])
                   if c not in prod_types and c not in _mapped]
        if dropped:
            print(f"    dev-only cols DROPPED: {sorted(dropped)}")
        nulled = _DATE_DROPS[_dstart:]
        if nulled:
            from collections import Counter as _C
            byc = _C(c for c, _ in nulled)
            ex = "; ".join(f"{c}: e.g. {raw!r}" for c, raw in nulled[:3])
            print(f"    WARN non-date text NULLED for prod `date` cols {dict(byc)} -> {ex}")

        for key, group in list(scopes.items())[: (10**9 if args.write else 50)]:
            scope = dict(key)
            existing = prod.count(prod_table, scope)
            print(f"    scope {scope}: dev->{len(group)} rows | prod currently={existing}")

        if args.limit_preview and prod_rows and not args.write:
            print("    sample transformed row:")
            print("      " + json.dumps(prod_rows[0], ensure_ascii=False)[:600])

        # ORPHAN SCOPES. The write loop only visits scopes that exist in _final, so a scope
        # prod still holds but dev no longer produces is never deleted and silently persists.
        # It happens whenever a row changes scope -- collapsing the cross-year transaction
        # duplicates moved UD1U's Il-lumina from FY2025 into FY2024, and prod kept the FY2025
        # copy, leaving that property in the table twice.
        # Only swept on a FULL promote: under --symbols/--fy we cannot tell an orphan from a
        # scope that simply was not selected.
        orphans = []
        if not symbols_si and args.fy is None:
            live = {tuple(scope_of(pr, scope_cols).items()) for pr in prod_rows}
            try:
                existing = prod.scopes_present(prod_table, scope_cols)
                orphans = [dict(k) for k in existing - live]
            except Exception as exc:
                print(f"    (orphan-scope check skipped: {exc})")
            if orphans:
                print(f"    ORPHAN scopes in prod with no rows in _final: {len(orphans)} -> "
                      f"{orphans[:6]}{' ...' if len(orphans) > 6 else ''}"
                      f"{'' if args.delete_orphans else '  (kept; --delete-orphans to remove)'}")

        protected = set()
        pkeys = PROTECT_KEYS.get(prod_table)
        if pkeys:
            for key, group in scopes.items():
                dev_keys = {tuple(r.get(c) for c in pkeys) for r in group}
                extra = [r for r in prod.rows(prod_table, dict(key), pkeys)
                         if tuple(r.get(c) for c in pkeys) not in dev_keys]
                if extra:
                    protected.add(key)
                    print(f"    PROTECTED scope {dict(key)}: prod has {len(extra)} row(s) dev does not, "
                          f"e.g. {extra[:2]}; scope skipped")

        if args.write:
            for scope in (orphans if args.delete_orphans else []):
                try:
                    print(f"    DELETED orphan scope {scope}: {prod.delete(prod_table, scope)}")
                except urllib.error.HTTPError as e:
                    print(f"    ERROR orphan {scope}: HTTP {e.code}")
                except Exception as e:
                    print(f"    ERROR orphan {scope}: {e!r}")
            for key, group in scopes.items():
                scope = dict(key)
                if key in protected:
                    continue
                try:
                    dstat = prod.delete(prod_table, scope)
                    # a scope emptied by ROW_FILTERS is deleted and left empty; POSTing []
                    # is a 400 from PostgREST
                    istat = prod.insert(prod_table, group) if group else "skipped (0 rows)"
                    print(f"    WROTE scope {scope}: delete={dstat} insert={istat} rows={len(group)}")
                except urllib.error.HTTPError as e:
                    print(f"    ERROR scope {scope}: HTTP {e.code} {e.read().decode('utf-8','replace')[:300]}")
                except Exception as e:
                    # e.g. a timeout between DELETE and POST, which can leave the scope empty
                    print(f"    ERROR scope {scope}: {e!r}")

    if not args.write:
        print("\n(DRY RUN — nothing written to prod. Re-run with --write to apply.)")
    dev.close()


if __name__ == "__main__":
    main()
