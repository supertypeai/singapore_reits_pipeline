"""Add the .SI suffix to sgx_reit_property_transaction.symbol in dev raw.

Every other raw table and prod use '<TICKER>.SI'. The transaction rebuild loaded bare tickers,
so promote could not match prod scopes and build_final_tables.py fy_end() lookups missed.

Dry run by default; --write applies. Safe to re-run: rows already ending in .SI are skipped.
"""
import os
import sys

import psycopg2
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(ROOT, ".env"))
WRITE = "--write" in sys.argv


def main():
    conn = psycopg2.connect(os.environ["SUPABASE_CONNECTION_STRING"])
    cur = conn.cursor()
    cur.execute("""select symbol, count(*) from sgx_reit_property_transaction
                   where symbol not like '%.SI' group by symbol order by symbol""")
    bare = cur.fetchall()
    print(f"rows without .SI: {sum(n for _, n in bare)} across {len(bare)} symbols")
    for sym, n in bare:
        print(f"  {sym:6} -> {sym}.SI   {n} rows")

    # each new symbol must exist in the performance table, or the ticker itself is wrong
    cur.execute("select distinct symbol from sgx_reit_performance")
    known = {r[0] for r in cur.fetchall()}
    unknown = [s for s, _ in bare if f"{s}.SI" not in known]
    if unknown:
        sys.exit(f"STOP: no performance rows for {unknown}; check these tickers first")

    # the new key must not collide with a row that already has .SI
    cur.execute("""select a.symbol, a.financial_year, a.transaction_type, a.property_name
                   from sgx_reit_property_transaction a
                   join sgx_reit_property_transaction b
                     on b.symbol = a.symbol || '.SI' and b.financial_year = a.financial_year
                    and b.transaction_type = a.transaction_type and b.property_name = a.property_name
                   where a.symbol not like '%.SI'""")
    clash = cur.fetchall()
    if clash:
        sys.exit(f"STOP: {len(clash)} rows would collide, e.g. {clash[:3]}")

    if not WRITE:
        print("dry run; add --write to apply")
        return
    cur.execute("update sgx_reit_property_transaction set symbol = symbol || '.SI' where symbol not like '%.SI'")
    print(f"updated {cur.rowcount} rows")
    conn.commit()


if __name__ == "__main__":
    main()
