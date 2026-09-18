"""Apply cited fixes to a dev raw sgx_reit_* table from citation files.

Citation files live in internal/fixes/property/*.json. A file may name its table; the default
is sgx_reit_property keyed on (symbol, financial_year, property_name):
  {"fix": "<name>",
   "table": "sgx_reit_property_transaction",
   "key_columns": ["symbol", "financial_year", "transaction_type", "property_name"],
   "entries": [
     {"key": {"symbol": "A17U.SI", "financial_year": 2024, "property_name": "FoodAxis @ Senoko"},
      "set": {"purchase_price": 57800000},
      "expected_current": {"purchase_price": 1900000},
      "source": "05_A17U..._FY2025 p.52; FY2025 AR p.208", "note": "..."},
     {"key": {...}, "action": "delete", "source": "..."},
     {"key": {...}, "action": "insert", "row": {<column>: <value>, ...}, "source": "..."}
  ]}

Per value (Rule 2 of the 2026-09-17 plan):
  current == target            -> skip (already applied)
  current == expected_current  -> write target
  anything else                -> CONFLICT, nothing written for that entry
Numbers compare with a tolerance (raw numeric is Decimal, JSON is float).
A (key, column) owned by two entries across all citation files stops the run.

Usage:
  python scripts/db/apply_property_fixes.py internal/fixes/property/purchase_price_row_shift.json
  python scripts/db/apply_property_fixes.py internal/fixes/property/purchase_price_row_shift.json --write
"""
import glob
import json
import math
import os
import sys

import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(ROOT, ".env"))
FIX_DIR = os.path.join(ROOT, "internal", "fixes", "property")
DEFAULT_TABLE = "sgx_reit_property"
DEFAULT_KEY = ("symbol", "financial_year", "property_name")
TABLE, KEY = DEFAULT_TABLE, DEFAULT_KEY


def same(a, b):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=0.5)
    except (TypeError, ValueError):
        return str(a) == str(b)


def key_of(entry, key=None):
    return tuple(entry["key"][k] for k in (key or KEY))


def check_owners():
    owners = {}
    for path in sorted(glob.glob(os.path.join(FIX_DIR, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            fix = json.load(fh)
        key = tuple(fix.get("key_columns", DEFAULT_KEY))
        table = fix.get("table", DEFAULT_TABLE)
        for e in fix["entries"]:
            for col in list(e.get("set", {})) or [f"<{e.get('action')}>"]:
                cell = (table,) + key_of(e, key) + (col,)
                if cell in owners and owners[cell] != path:
                    sys.exit(f"STOP: {cell} is claimed by {os.path.basename(owners[cell])} "
                             f"and {os.path.basename(path)}")
                owners[cell] = path


def fetch(cur, key, cols):
    where = " and ".join(f"{k}=%s" for k in KEY)
    cur.execute(f"select {', '.join(cols)} from {TABLE} where {where}", key)
    rows = cur.fetchall()
    if len(rows) > 1:
        sys.exit(f"STOP: key {key} matches {len(rows)} rows")
    return dict(zip(cols, rows[0])) if rows else None


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path, write = sys.argv[1], "--write" in sys.argv
    check_owners()
    with open(path, encoding="utf-8") as fh:
        fix = json.load(fh)
    global TABLE, KEY
    TABLE = fix.get("table", DEFAULT_TABLE)
    KEY = tuple(fix.get("key_columns", DEFAULT_KEY))

    conn = psycopg2.connect(os.environ["SUPABASE_CONNECTION_STRING"])
    cur = conn.cursor()
    counts = {"write": 0, "skip": 0, "conflict": 0}
    plan = []
    for e in fix["entries"]:
        key = key_of(e)
        action = e.get("action", "set")
        if action == "delete":
            live = fetch(cur, key, [KEY[-1]])
            status = "skip" if live is None else "write"
            plan.append((status, key, "DELETE", None, None))
        elif action == "insert":
            live = fetch(cur, key, [KEY[-1]])
            status = "skip" if live is not None else "write"
            plan.append((status, key, "INSERT", None, e["row"]))
        else:
            live = fetch(cur, key, list(e["set"]))
            if live is None:
                plan.append(("conflict", key, "row missing", None, None))
                continue
            for col, target in e["set"].items():
                cur_val = live[col]
                expected = e["expected_current"][col]
                if same(cur_val, target):
                    status = "skip"
                elif same(cur_val, expected):
                    status = "write"
                else:
                    status = "conflict"
                plan.append((status, key, col, cur_val, target))
    for status, key, col, cur_val, target in plan:
        counts[status] += 1
        if status != "skip":
            print(f"  {status.upper():8} {key[0]:8} FY{key[1]} {str(key[-1])[:38]:38} {col:22} "
                  f"{cur_val} -> {target if col not in ('INSERT',) else 'row'}")
    print(f"{fix['fix']}: write {counts['write']}, skip {counts['skip']}, conflict {counts['conflict']}")

    if not write:
        print("dry run; add --write to apply")
        return
    if counts["conflict"]:
        sys.exit("STOP: conflicts; nothing written")
    for status, key, col, _cur, target in plan:
        if status != "write":
            continue
        where = " and ".join(f"{k}=%s" for k in KEY)
        if col == "DELETE":
            cur.execute(f"delete from {TABLE} where {where}", key)
        elif col == "INSERT":
            cols = list(target)
            cur.execute(f"insert into {TABLE} ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})",
                        [target[c] for c in cols])
        else:
            # jsonb columns (e.g. distribution_record) must go through Json(), or psycopg2
            # adapts a Python list to a Postgres array
            value = Json(target) if isinstance(target, (list, dict)) else target
            cur.execute(f"update {TABLE} set {col}=%s where {where}", (value, *key))
            if cur.rowcount != 1:
                conn.rollback()
                sys.exit(f"STOP: update of {key} {col} touched {cur.rowcount} rows; rolled back")
    conn.commit()
    print(f"applied {counts['write']} changes")


if __name__ == "__main__":
    main()
