"""Back up prod REIT tables to JSON, and restore one empty scope from a backup.

Backup (default):
  python scripts/db/backup_prod_tables.py
  -> backup/<YYYY-MM-DD>_prod_<NN>/<table>.json, one folder per run

Restore one scope (only when a promote left that scope empty):
  python scripts/db/backup_prod_tables.py --restore backup/2026-09-17_prod_02/sgx_reit_property.json --symbol D5IU --fy 2025
  python scripts/db/backup_prod_tables.py --restore ... --symbol D5IU --fy 2025 --write

Restore is a dry run unless --write, and it refuses a scope that still has rows in prod.
"""
import argparse
import datetime as dt
import json
import os
import sys

import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(ROOT, ".env"))

# order columns make paging stable; they are the natural key of each table
TABLES = {
    "sgx_reit_profile": "symbol",
    "sgx_reit_performance": "symbol,financial_year",
    "sgx_reit_property": "symbol,financial_year,property_name,address,status",
    "sgx_reit_property_transaction": "symbol,financial_year,property_name,transaction_type,completed_date,counterparty",
    "sgx_reit_top_tenant": "symbol,financial_year,pct_basis,basis_segment,rank,client_name",
    "sgx_reit_trade_mix": "symbol,financial_year,pct_basis,basis_segment,category",
    "sgx_manual_input": "symbol,financial_year",
}
PAGE = 1000


def session():
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1"
    key = os.environ["SUPABASE_KEY"]
    s = requests.Session()
    s.headers.update({"apikey": key, "Authorization": f"Bearer {key}"})
    return url, s


def prod_count(url, s, table, params=None):
    r = s.get(f"{url}/{table}", params={"select": "symbol", **(params or {})},
              headers={"Prefer": "count=exact", "Range": "0-0"}, timeout=60)
    r.raise_for_status()
    return int(r.headers["Content-Range"].split("/")[1])


def fetch_all(url, s, table, order):
    rows = []
    while True:
        r = s.get(f"{url}/{table}", params={"select": "*", "order": order},
                  headers={"Range": f"{len(rows)}-{len(rows) + PAGE - 1}"}, timeout=120)
        r.raise_for_status()
        page = r.json()
        rows += page
        if len(page) < PAGE:
            return rows


def next_folder():
    base = os.path.join(ROOT, "backup")
    day = dt.date.today().isoformat()
    n = 1
    while os.path.exists(os.path.join(base, f"{day}_prod_{n:02d}")):
        n += 1
    path = os.path.join(base, f"{day}_prod_{n:02d}")
    os.makedirs(path)
    return path


def backup():
    url, s = session()
    folder = next_folder()
    print(f"backup folder: {folder}")
    ok = True
    for table, order in TABLES.items():
        expected = prod_count(url, s, table)
        rows = fetch_all(url, s, table, order)
        with open(os.path.join(folder, f"{table}.json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False)
        status = "ok" if len(rows) == expected else "COUNT MISMATCH"
        ok &= len(rows) == expected
        print(f"  {table:32} {len(rows):>6} rows   (prod count {expected})  {status}")
    if not ok:
        sys.exit("backup incomplete: a table changed while it was read; run again")


def restore(path, symbol, fy, write):
    url, s = session()
    table = os.path.basename(path).removesuffix(".json")
    if table not in TABLES:
        sys.exit(f"unknown table in file name: {table}")
    symbol = symbol if symbol.endswith(".SI") else f"{symbol}.SI"
    with open(path, encoding="utf-8") as fh:
        rows = [r for r in json.load(fh)
                if r["symbol"] == symbol and (fy is None or r.get("financial_year") == fy)]
    params = {"symbol": f"eq.{symbol}"}
    if fy is not None:
        params["financial_year"] = f"eq.{fy}"
    live = prod_count(url, s, table, params)
    print(f"{table} {symbol} FY{fy}: {len(rows)} rows in backup, {live} rows in prod now")
    if live:
        sys.exit("refused: prod still has rows for this scope; re-promote it instead")
    if not rows:
        sys.exit("nothing to restore")
    if not write:
        print("dry run; add --write to POST these rows")
        return
    r = s.post(f"{url}/{table}", json=rows, headers={"Prefer": "return=minimal"}, timeout=120)
    if r.status_code >= 300:
        sys.exit(f"ERROR HTTP {r.status_code}: {r.text[:500]}")
    print(f"RESTORED {len(rows)} rows; prod now has {prod_count(url, s, table, params)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--restore", help="backup file of one table")
    ap.add_argument("--symbol", help="scope symbol, with or without .SI")
    ap.add_argument("--fy", type=int, help="scope financial_year (omit for sgx_reit_profile)")
    ap.add_argument("--write", action="store_true", help="restore: actually POST")
    args = ap.parse_args()
    if args.restore:
        if not args.symbol:
            sys.exit("--restore needs --symbol")
        restore(args.restore, args.symbol, args.fy, args.write)
    else:
        backup()


if __name__ == "__main__":
    main()
