"""Validate promote_final_to_prod.py transforms.
(A) Invariants on transformed FY2025 output.
(B) Round-trip: transform dev *_final FY2024 and diff vs ACTUAL prod FY2024 rows
    (ground truth the original promotion produced) -> a correct transform reproduces
    prod's systematic representation. Value drift (older prod vintage) is reported
    separately from TRANSFORM mismatches (which would be bugs).
Read-only."""
import os, re, json, importlib.util, urllib.request, psycopg2, datetime, decimal
from dotenv import load_dotenv
load_dotenv(".env")

# import the promotion module
spec = importlib.util.spec_from_file_location("promo", "scripts/db/promote_final_to_prod.py")
promo = importlib.util.module_from_spec(spec); spec.loader.exec_module(promo)

DEV = psycopg2.connect(os.environ["SUPABASE_CONNECTION_STRING"]); C = DEV.cursor()
P = os.environ["SUPABASE_URL"].rstrip("/"); K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": "Bearer " + K}
prod = promo.Prod(); DEFS = prod.defs()
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FRACTION = promo.FRACTION_FIELDS

def dev_rows(final_table, syms_si, fy):
    return promo.dev_read(C, final_table, syms_si, fy)
def _cols(cur, table):
    cur.execute("select column_name from information_schema.columns where table_name=%s", (table,))
    return {r[0] for r in cur.fetchall()}


def prod_rows(t, syms_bare, fy):
    inl = "(" + ",".join(syms_bare) + ")"
    q = f"symbol=in.{inl}"
    if fy is not None: q += f"&financial_year=eq.{fy}"
    rq = urllib.request.Request(f"{P}/rest/v1/{t}?{q}&limit=5000", headers=H)
    return json.load(urllib.request.urlopen(rq))

SYMS_SI = ["M44U.SI", "ME8U.SI", "N2IU.SI"]
SYMS_B = ["M44U", "ME8U", "N2IU"]
problems = []

# ---------- (A) invariants on FY2025 ----------
print("="*80); print("(A) INVARIANTS on transformed FY2025 output"); print("="*80)
for final_t, prod_t, scope in promo.PAIRS:
    ptypes = prod.col_types(prod_t, DEFS)
    src = dev_rows(final_t, SYMS_SI, 2025 if "financial_year" in _cols(C, final_t) else None)
    tr = [promo.transform_row(r, ptypes) for r in src]
    for i, (o, t) in enumerate(zip(src, tr)):
        # prod keeps the .SI suffix
        if not t.get("symbol", "").endswith(".SI"):
            problems.append(f"{prod_t}[{i}] symbol lost its .SI suffix: {t['symbol']}")
        # fraction fields in [0,1] and == orig/100
        for f in FRACTION:
            if f in ptypes and t.get(f) is not None:
                if not (0 <= t[f] <= 1.0000001):
                    problems.append(f"{prod_t}[{i}] {f}={t[f]} not in [0,1]")
                if o.get(f) is not None and abs(float(o[f])/100 - t[f]) > 1e-6:
                    problems.append(f"{prod_t}[{i}] {f} not /100: {o[f]}->{t[f]}")
        # gain_loss_pct NOT divided
        if "gain_loss_pct" in ptypes and t.get("gain_loss_pct") is not None and o.get("gain_loss_pct") is not None:
            if abs(float(o["gain_loss_pct"]) - float(t["gain_loss_pct"])) > 1e-6:
                problems.append(f"{prod_t}[{i}] gain_loss_pct CHANGED {o['gain_loss_pct']}->{t['gain_loss_pct']}")
        # date columns well-formed
        for c, pt in ptypes.items():
            if pt == "date" and t.get(c) is not None and not DATE_RX.match(str(t[c])):
                problems.append(f"{prod_t}[{i}] {c} bad date '{t[c]}'")
        # text-typed values are strings (not dict/list unless jsonb)
        for c, pt in ptypes.items():
            if pt in ("text","character varying") and t.get(c) is not None and isinstance(t[c],(dict,list)):
                problems.append(f"{prod_t}[{i}] {c} text col holds {type(t[c]).__name__}")
        # properties_location is list
        if "properties_location" in ptypes and t.get("properties_location") is not None:
            pl = t["properties_location"]
            if not (isinstance(pl, str) and pl.startswith("[") and pl.endswith("]")):
                problems.append(f"{prod_t}[{i}] properties_location not bracketed text: {pl!r}")
        # dev-only cols absent
        for dc in ("deal_id","source_type","announcement_refs"):
            if dc in t: problems.append(f"{prod_t}[{i}] dev-only col leaked: {dc}")
        # every value JSON-serializable
        try: json.dumps(t)
        except Exception as e: problems.append(f"{prod_t}[{i}] not JSON-serializable: {e}")
    print(f"  {prod_t:32} rows checked={len(tr)}")

print(f"\n  INVARIANT PROBLEMS: {len(problems)}")
for pmsg in problems[:50]: print("   -", pmsg)

# ---------- (B) round-trip vs prod ground truth (FY2024) ----------
print("\n"+"="*80); print("(B) ROUND-TRIP transform(dev *_final FY2024) vs ACTUAL prod FY2024"); print("="*80)
# transform-sensitive systematic fields we expect to MATCH prod exactly (same vintage rows)
def keyfn(prod_t, r):
    if prod_t == "sgx_reit_performance": return (r["symbol"], r["financial_year"])
    if prod_t == "sgx_reit_property": return (r["symbol"], r["financial_year"], r.get("property_name"))
    if prod_t == "sgx_reit_top_tenant": return (r["symbol"], r["financial_year"], r.get("rank"))
    if prod_t == "sgx_reit_trade_mix": return (r["symbol"], r["financial_year"], r.get("category"))
    return None

for final_t, prod_t, scope in promo.PAIRS:
    if prod_t in ("sgx_reit_profile","sgx_reit_property_transaction"): continue  # profile no fy; txn = heavy drift
    ptypes = prod.col_types(prod_t, DEFS)
    src = dev_rows(final_t, SYMS_SI, 2024)
    tr = [promo.transform_row(r, ptypes) for r in src]
    pr = prod_rows(prod_t, SYMS_B, 2024)
    pmap = {keyfn(prod_t, r): r for r in pr if keyfn(prod_t, r)}
    matched=missing=transform_mismatch=value_drift=0
    examples=[]
    for t in tr:
        k = keyfn(prod_t, t)
        if k not in pmap: missing+=1; continue
        matched+=1
        b = pmap[k]
        # check transform-sensitive fields specifically
        for f in list(FRACTION)+["symbol","properties_location"]:
            if f not in ptypes: continue
            a=t.get(f); bv=b.get(f)
            if a is None and bv is None: continue
            if f=="properties_location":
                same = (a==bv) or (isinstance(a,list) and isinstance(bv,list) and a==bv)
            elif f in FRACTION:
                same = (a is not None and bv is not None and abs(float(a)-float(bv))<1e-6) or (a==bv)
            else:
                same = str(a)==str(bv)
            if not same and len(examples)<15:
                examples.append(f"{prod_t} {k} {f}: transformed={a} prod={bv}")
                transform_mismatch+=1
    print(f"  {prod_t:28} matched={matched} missing_in_prod={missing} transform-field mismatches={transform_mismatch}")
    for e in examples[:8]: print("      *", e)

DEV.close()
