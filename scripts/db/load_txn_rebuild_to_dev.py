"""Load txn_rebuild/*.json into dev raw sgx_reit_property_transaction (v2 schema).

Three steps, in one transaction:
  1. MIGRATE  add the v2 columns, drop the 8 columns v2 retires
  2. LOAD     replace all rows with the rebuild's
  3. (manual) build_final_tables.py must be updated in the same change -- its
              transaction block still selects the dropped columns and will fail
              on the next build. See --check-pipeline.

v2 schema (docs/7-30-2026-schema-review/transaction-target-schema-AGREED.md):

  ACQUISITION   purchase_price + purchase_price_currency + purchase_price_scope
  DIVESTMENT    sale_price + sale_price_currency + sale_price_scope
                basis_value + basis_currency + basis   (was reference_value/_basis)
                interest_pct, deal_id

  gain  = sale_price - basis_value          (both must be the same currency)
  pct   = gain / basis_value

Money is stored NATIVE with a currency tag. No conversion happens here -- that is
build_final_tables.py's job, and doing it at load is what produced the DCRU 770,936
artifact and the N2IU double-conversion.

Usage:
  python scripts/db/load_txn_rebuild_to_dev.py                  # DRY RUN (default)
  python scripts/db/load_txn_rebuild_to_dev.py --check-pipeline # what breaks in build_final
  python scripts/db/load_txn_rebuild_to_dev.py --write          # migrate + load

DRY BY DEFAULT. Nothing is written without --write.
"""
import os, sys, json, glob, argparse, collections
import psycopg2
from psycopg2.extras import Json, execute_values
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(ROOT, ".env"))
TABLE = "sgx_reit_property_transaction"

ADD = [  # column, type
    ("basis_value",           "numeric"),
    ("basis_currency",        "text"),
    ("basis",                 "text"),
    ("sale_price_scope",      "text"),
    ("purchase_price_scope",  "text"),
    ("figures_source",        "text"),
    ("basis_mismatch",        "text"),
    ("sale_price_citation",   "text"),
    ("purchase_price_citation", "text"),
    ("citation",              "text"),
    ("notes",                 "text"),
]
DROP = ["gain_loss_pct",
        "carrying_value", "carrying_value_currency", "carrying_value_basis",
        "valuation", "valuation_currency", "valuation_date",
        "gain_on_divestment", "gain_currency", "gain_on_divestment_basis", "gain_basis",
        "net_sale_proceeds", "net_sale_proceeds_currency", "net_proceeds_basis",
        "announced_date", "transaction_date"]
# rebuild JSON field -> dev column
COLS = ["symbol", "financial_year", "deal_id", "transaction_type", "status", "property_name",
        "counterparty", "completed_date",
        "purchase_price", "purchase_price_currency", "purchase_price_scope", "purchase_price_citation",
        "sale_price", "sale_price_currency", "sale_price_scope", "sale_price_citation",
        "basis_value", "basis_currency", "basis",
        "interest_pct",
        "figures_source", "basis_mismatch", "citation", "notes"]


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check-pipeline", action="store_true",
                    help="report which build_final_tables.py references break, then exit")
    return ap.parse_args()


def read_rebuild():
    rows, issues = [], []
    for f in sorted(glob.glob(os.path.join(ROOT, "txn_rebuild", "*.json"))):
        j = json.load(open(f, encoding="utf-8"))
        sym, fy = j.get("symbol"), j.get("financial_year")
        # rebuild files hold bare tickers; every table uses '<TICKER>.SI'
        if sym and not sym.endswith(".SI"):
            sym += ".SI"
        for t in j.get("transactions", []):
            isdiv = "acquisition" not in str(t.get("transaction_type", ""))
            # v2 rename: reference_* -> basis_*
            bv, bc, bs = t.get("reference_value"), t.get("reference_currency"), t.get("reference_basis")
            # gain_loss_pct is NOT stored (dropped 2026-08-03): it derives from
            # sale_price and basis_value, and no row needs it to recover a price.
            r = {c: None for c in COLS}
            r.update(symbol=sym, financial_year=fy, deal_id=t.get("deal_id"),
                     transaction_type=t.get("transaction_type"), status=t.get("status"),
                     property_name=t.get("property_name"), counterparty=t.get("counterparty"),
                     completed_date=t.get("completed_date"),
                     purchase_price=t.get("purchase_price"),
                     purchase_price_currency=t.get("purchase_price_currency"),
                     purchase_price_scope=t.get("purchase_price_scope"),
                     purchase_price_citation=t.get("purchase_price_citation"),
                     sale_price=t.get("sale_price"), sale_price_currency=t.get("sale_price_currency"),
                     sale_price_scope=t.get("sale_price_scope"),
                     sale_price_citation=t.get("sale_price_citation"),
                     basis_value=bv, basis_currency=bc, basis=bs,
                     interest_pct=t.get("interest_pct"),
                     figures_source=t.get("figures_source"), basis_mismatch=t.get("basis_mismatch"),
                     citation=t.get("citation"), notes=t.get("notes"))
            # integrity checks -- reported, never silently fixed
            price = r["sale_price"] if isdiv else r["purchase_price"]
            pcur = r["sale_price_currency"] if isdiv else r["purchase_price_currency"]
            if price is not None and not pcur:
                issues.append((sym, fy, r["property_name"], "price has no currency tag"))
            if bv is not None and not bc:
                issues.append((sym, fy, r["property_name"], "basis_value has no currency tag"))
            if bs and bs not in ("valuation", "book_value", "purchase_price", "net_identifiable_assets"):
                issues.append((sym, fy, r["property_name"], f"basis outside enum: {bs}"))
            if isdiv and price is not None and bv is not None and pcur and bc and pcur != bc:
                issues.append((sym, fy, r["property_name"],
                               f"CURRENCY MISMATCH price {pcur} vs basis {bc} -- gain not computable"))
            rows.append(r)
    return rows, issues


def main():
    args = parse_args()
    rows, issues = read_rebuild()

    cn = psycopg2.connect(os.environ["SUPABASE_CONNECTION_STRING"])
    cur = cn.cursor()
    cur.execute("""select column_name from information_schema.columns
                   where table_name=%s and table_schema='public'""", (TABLE,))
    have = {r[0] for r in cur.fetchall()}
    cur.execute(f"select count(*) from {TABLE}")
    n_dev = cur.fetchone()[0]

    if args.check_pipeline:
        src = open(os.path.join(ROOT, "scripts", "db", "build_final_tables.py"), encoding="utf-8").read()
        # only the sgx_reit_property_transaction SELECT matters -- these names also exist
        # on sgx_reit_property, and appear in comments
        i = src.find("from sgx_reit_property_transaction")
        blk = src[max(0, i - 1400):i] if i >= 0 else ""
        blk = chr(10).join(l for l in blk.splitlines() if not l.strip().startswith("#"))
        bad = [c for c in DROP if c in blk]
        print("build_final_tables.py — columns v2 DROPS still SELECTed from the transaction table:")
        if bad:
            for c in bad:
                print(f"   {c:28s} -> must be removed before the migration runs")
        else:
            print("   none — the transaction block is on the v2 schema")
        print("\nThe transaction block needs rewriting to:")
        print("   select ... sale_price, sale_price_currency, basis_value, basis_currency, basis ...")
        print("   TXN_MONEY = [('purchase_price','purchase_price_currency','completed_date'),")
        print("                ('sale_price','sale_price_currency','completed_date'),")
        print("                ('basis_value','basis_currency','completed_date')]")
        return 0

    print("=" * 76)
    print("LOAD txn_rebuild -> dev raw" + ("  [--write]" if args.write else "  [DRY RUN]"))
    print("=" * 76)
    print(f"  dev rows now      {n_dev}")
    print(f"  rebuild rows      {len(rows)}")
    print(f"  net change        {len(rows) - n_dev:+d}")

    add = [(c, t) for c, t in ADD if c not in have]
    drop = [c for c in DROP if c in have]
    print(f"\n  columns to ADD    {len(add)}: {', '.join(c for c, _ in add) or '-'}")
    print(f"  columns to DROP   {len(drop)}: {', '.join(drop) or '-'}")

    d = sum(1 for r in rows if "acquisition" not in str(r["transaction_type"]))
    a = len(rows) - d
    comp = [r for r in rows if r["status"] == "completed"]
    print(f"\n  acquisitions {a} | divestments {d} | completed {len(comp)}")
    for c in ("sale_price", "basis_value", "basis", "deal_id"):
        v = sum(1 for r in rows if r[c] is not None)
        print(f"    {c:22s}{v:>4d}/{len(rows)}")
    print("    gain_loss_pct NOT stored -- derived at read time as "
          "(sale_price - basis_value) / basis_value")

    if issues:
        print(f"\n  !! {len(issues)} INTEGRITY ISSUES -- fix these before loading:")
        for s, fy, p, m in issues[:25]:
            print(f"     {str(s):7s}FY{fy} {str(p)[:40]:40s} {m}")
        if len(issues) > 25:
            print(f"     ... and {len(issues) - 25} more")
    else:
        print("\n  integrity checks: clean (every price and basis carries a currency; "
              "no basis outside the enum; no cross-currency rows)")

    if not args.write:
        print("\n  DRY RUN -- nothing written.")
        print("  Run with --check-pipeline first; build_final_tables.py MUST be updated in the")
        print("  same change or the next _final build will fail.")
        return 0

    if issues:
        print("\n  REFUSING to write while integrity issues are outstanding.")
        return 1

    try:
        for c, t in add:
            cur.execute(f"alter table {TABLE} add column {c} {t}")
        for c in drop:
            cur.execute(f"alter table {TABLE} drop column {c}")
        cur.execute(f"delete from {TABLE}")
        execute_values(cur, f"insert into {TABLE} ({','.join(COLS)}) values %s",
                       [tuple(r[c] for c in COLS) for r in rows])
        cn.commit()
        print(f"\n  APPLIED: +{len(add)} columns, -{len(drop)} columns, {len(rows)} rows loaded.")
        print("  Next: update build_final_tables.py, rebuild _final, then re-promote.")
    except Exception as exc:
        cn.rollback()
        print(f"\n  ROLLED BACK -- {exc}")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
