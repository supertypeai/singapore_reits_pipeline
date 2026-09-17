"""Apply cited fixes to dev raw sgx_reit_property from citation files.

Citation files live in internal/fixes/property/*.json:
  {"fix": "<name>", "entries": [
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
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(ROOT, ".env"))
FIX_DIR = os.path.join(ROOT, "internal", "fixes", "property")
TABLE = "sgx_reit_property"
KEY = ("symbol", "financial_year", "property_name")


def same(a, b):
    if a is None or b is None:
        return a is None and b is None
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=0.5)
    except (TypeError, ValueError):
        return str(a) == str(b)


def key_of(entry):
    return tuple(entry["key"][k] for k in KEY)


def check_owners():
    owners = {}
    for path in sorted(glob.glob(os.path.join(FIX_DIR, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            for e in json.load(fh)["entries"]:
                cols = list(e.get("set", {})) or [f"<{e.get('action')}>"]
                for col in cols:
                    cell = key_of(e) + (col,)
                    if cell in owners and owners[cell] != path:
                        sys.exit(f"STOP: {cell} is claimed by {os.path.basename(owners[cell])} "
                                 f"and {os.path.basename(path)}")
                    owners[cell] = path


def fetch(cur, key, cols):
    cur.execute(f"select {', '.join(cols)} from {TABLE} where symbol=%s and financial_year=%s and property_name=%s",
                key)
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

    conn = psycopg2.connect(os.environ["SUPABASE_CONNECTION_STRING"])
    cur = conn.cursor()
    counts = {"write": 0, "skip": 0, "conflict": 0}
    plan = []
    for e in fix["entries"]:
        key = key_of(e)
        action = e.get("action", "set")
        if action == "delete":
            live = fetch(cur, key, ["property_name"])
            status = "skip" if live is None else "write"
            plan.append((status, key, "DELETE", None, None))
        elif action == "insert":
            live = fetch(cur, key, ["property_name"])
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
            print(f"  {status.upper():8} {key[0]:8} FY{key[1]} {key[2][:38]:38} {col:22} "
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
        if col == "DELETE":
            cur.execute(f"delete from {TABLE} where symbol=%s and financial_year=%s and property_name=%s", key)
        elif col == "INSERT":
            cols = list(target)
            cur.execute(f"insert into {TABLE} ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})",
                        [target[c] for c in cols])
        else:
            cur.execute(f"update {TABLE} set {col}=%s where symbol=%s and financial_year=%s and property_name=%s",
                        (target, *key))
            if cur.rowcount != 1:
                conn.rollback()
                sys.exit(f"STOP: update of {key} {col} touched {cur.rowcount} rows; rolled back")
    conn.commit()
    print(f"applied {counts['write']} changes")


if __name__ == "__main__":
    main()
