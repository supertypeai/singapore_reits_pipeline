"""Audit sgx_reit_property.purchase_price_currency (dev raw) against report evidence.

build_final_tables.py converts purchase_price with this tag, so a wrong tag converts twice
(a price printed in S$ but tagged JPY) or not at all. Evidence: the report-checked rows in
internal/fixes/2026-09-17_evidence/d1_verify.csv and d2_verify.csv (page per row).

The evidence currency for a (symbol, country) applies to every row of that REIT in that
country: a REIT prints one currency per country in its property table.

Dry run by default: prints tag changes, conflicts and unverified groups, and writes
internal/fixes/purchase_price_currency_tags_preview.json. --write applies the changes.
Safe to re-run: a row already holding the expected tag is skipped.
"""
import collections
import csv
import json
import os
import re
import sys

import psycopg2
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(ROOT, ".env"))
EVIDENCE = os.path.join(ROOT, "internal", "fixes", "2026-09-17_evidence")
PREVIEW = os.path.join(ROOT, "internal", "fixes", "purchase_price_currency_tags_preview.json")
WRITE = "--write" in sys.argv

# report unit text -> ISO code, as stored in raw (RMB is stored as CNY)
UNIT = [(r"^(S\$|SGD)", "SGD"), (r"^(US\$|USD)", "USD"), (r"^(A\$|AUD)", "AUD"), (r"^EUR", "EUR"),
        (r"^GBP", "GBP"), (r"^JPY", "JPY"), (r"^(RM\b|MYR)", "MYR"), (r"^Rp", "IDR"), (r"^HKD", "HKD"),
        (r"^INR", "INR"), (r"^KRW", "KRW"), (r"^(RMB|CNY)", "CNY"), (r"^VND", "VND"), (r"^NZD", "NZD")]
USE = {"NATIVE", "SGD", "MV_SANITY_SGD", "OTHER"}


def unit_ccy(text):
    for rx, code in UNIT:
        if re.search(rx, text.strip()):
            return code
    return None


def norm(c):
    return "CNY" if c == "RMB" else c


def load_evidence():
    by_group = collections.defaultdict(dict)   # (symbol, country) -> {ccy: [page refs]}
    skipped = []
    for name in ("d1_verify.csv", "d2_verify.csv"):
        with open(os.path.join(EVIDENCE, name), encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                ccy = unit_ccy(r["report_ccy_unit"]) if r["verdict"] in USE else None
                if not ccy:
                    skipped.append((r["symbol"], r["financial_year"], r["property_name"], r["verdict"],
                                    r["report_ccy_unit"]))
                    continue
                ref = f"{r['report_folder']} p.{r['report_page']}"
                by_group[(r["symbol"], r["country"])].setdefault(ccy, []).append(ref)
    return by_group, skipped


def main():
    evidence, skipped = load_evidence()
    conflicts = {k: v for k, v in evidence.items() if len(v) > 1}

    conn = psycopg2.connect(os.environ["SUPABASE_CONNECTION_STRING"])
    cur = conn.cursor()
    cur.execute("""select symbol, financial_year, property_name, country, purchase_price_currency
                   from sgx_reit_property where purchase_price is not null""")
    changes, same, unverified = [], 0, collections.Counter()
    for sym, fy, pn, country, tag in cur.fetchall():
        ev = evidence.get((sym, country))
        if not ev:
            if norm(tag) not in (None, "SGD"):
                unverified[(sym, country, tag)] += 1
            continue
        if (sym, country) in conflicts:
            continue
        want = next(iter(ev))
        if norm(tag) == want:
            same += 1
        else:
            changes.append({"symbol": sym, "financial_year": fy, "property_name": pn, "country": country,
                            "current": tag, "target": want, "source": ev[want][0]})

    print(f"rows already correct: {same}")
    print(f"tag changes planned:  {len(changes)}")
    for (sym, cur_tag, want), n in sorted(collections.Counter(
            (c["symbol"], c["current"], c["target"]) for c in changes).items()):
        print(f"  {sym:8} {str(cur_tag):5} -> {want:4} {n:4} rows")
    print(f"conflicting evidence (skipped): {len(conflicts)}")
    for k, v in conflicts.items():
        print(f"  {k}: {dict((c, refs[:1]) for c, refs in v.items())}")
    print(f"UNVERIFIED non-SGD groups (no evidence): {len(unverified)}")
    for (sym, country, tag), n in sorted(unverified.items()):
        print(f"  {sym:8} {country:22} tag={tag:4} {n} rows")
    print(f"evidence rows not usable: {len(skipped)}")

    with open(PREVIEW, "w", encoding="utf-8") as fh:
        json.dump({"changes": changes, "conflicts": {f"{k[0]}|{k[1]}": v for k, v in conflicts.items()},
                   "unverified": [list(k) + [n] for k, n in unverified.items()], "skipped": skipped},
                  fh, ensure_ascii=False, indent=1)
    print(f"preview: {PREVIEW}")

    if not WRITE:
        print("dry run; add --write to apply")
        return
    if conflicts or unverified:
        sys.exit("STOP: resolve conflicts and unverified groups before --write")
    for c in changes:
        cur.execute("""update sgx_reit_property set purchase_price_currency = %s
                       where symbol = %s and financial_year = %s and property_name = %s""",
                    (c["target"], c["symbol"], c["financial_year"], c["property_name"]))
    conn.commit()
    print(f"updated {len(changes)} rows")


if __name__ == "__main__":
    main()
