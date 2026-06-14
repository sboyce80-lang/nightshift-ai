#!/usr/bin/env python3
"""Per-sheet-only re-validation to lock in P2-G (base trim) + small-commercial
floor dedup. Runs the 3 local tier-1 jobs in PER-SHEET mode (checkpoints from
the prior regression make extraction near-instant; only downstream passes +
schedule scan re-run), scores vs golden, and checks the expected outcomes:
  - Fishkill (multifamily): UNCHANGED (~walls 6%) — no regression
  - 364 (residential MF): base trim IMPROVED (was 66% under)
  - Dutchess (small commercial): walls IMPROVED (was 230% over)
Interrupt-tolerant: per-sheet checkpoints resume.
"""
import os, sys, json
from datetime import datetime, timezone
os.environ["NIGHTSHIFT_PER_SHEET_EXTRACTION"]="1"
os.environ.pop("NIGHTSHIFT_MERGE_UNION",None)
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
import regression_test as rt
from Takeoff_DIRECT import run_analysis
EXCL={"cost_estimate_subtotal","footprint_sqft"}
JOBS=[("fishkill_397", os.path.join(HERE,"spike_samples","397Fishkill.pdf")),
      ("364_main", os.path.join(HERE,"spike_samples","364Main.pdf")),
      ("dutchess_livestock", os.path.join(HERE,"golden","plans","Dutchess_Livestock_Bidding_Documents.pdf"))]
def log(m): print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {m}",flush=True)
def score(r,cid):
    data={"analysis":r.get("analysis",{}),"cost_estimate":r.get("cost_estimate",{})}
    m=rt.extract_metrics(data); rows=[]
    for k,sp in (rt.REFERENCE_CASES[cid].get("targets") or {}).items():
        if k in EXCL: continue
        t=sp[0] if isinstance(sp,(list,tuple)) else sp; a=m.get(k)
        if a is None or not t: continue
        rows.append((k,a,t,abs(float(a)-float(t))/float(t)*100))
    return rows
results={}
for cid,pdf in JOBS:
    if not os.path.exists(pdf): log(f"SKIP {cid}"); continue
    log(f"===== {cid} [per_sheet] =====")
    try:
        r=run_analysis([pdf],contact_name="PV",contact_email="pv@k.local",scope_notes="",rate_overrides=None,multi_pass=True)
        rows=score(r,cid); results[cid]={k:(a,t,e) for k,a,t,e in rows}
        mean=sum(e for _,_,_,e in rows)/len(rows) if rows else 0
        log(f"RESULT {cid}: mean={mean:.0f}%")
        for k,a,t,e in rows: log(f"   {k}: {a:.0f} vs {t:.0f} ({e:.0f}%)")
    except Exception as ex:
        import traceback; log(f"FAILED {cid}: {ex!r}"); traceback.print_exc()
log("\n========== LOCK-IN VERDICT ==========")
def g(cid,k,which): 
    v=results.get(cid,{}).get(k); return v[which] if v else None
def emit(label,cid,k):
    v=results.get(cid,{}).get(k)
    log(f"  {label}: {('%.0f%% (%.0f vs %.0f)'%(v[2],v[0],v[1])) if v else 'n/a'}")
emit("Fishkill walls (expect ~6%, no regression)","fishkill_397","total_paintable_wall_sqft")
emit("364 walls (expect ~24%, #3 not yet)","364_main","total_paintable_wall_sqft")
emit("364 base trim (expect IMPROVED <40%)","364_main","total_base_trim_lf")
emit("Dutchess walls (expect IMPROVED <140%)","dutchess_livestock","total_paintable_wall_sqft")
log("========== END ==========")
