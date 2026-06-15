#!/usr/bin/env python3
"""Golden regression: every locally-available tier-1 verified job through BOTH
the legacy (deployed) path and per-sheet (all P2 fixes), scored vs golden
quantity targets. Two questions:
  1. REGRESSION: do the all-jobs changes (enlarged dedup, generic-room
     normalization) move the LEGACY result vs its known baseline? (must not)
  2. IMPROVEMENT: does per-sheet now produce accurate results across building
     classes (esp. Dutchess small-commercial, was 190%)?
Per-sheet uses checkpoints (fast); legacy is full multi-pass.
"""
import os, sys, json
from datetime import datetime, timezone
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
import regression_test as rt
from Takeoff_DIRECT import run_analysis
EXCL={"cost_estimate_subtotal","footprint_sqft"}
# Prior LEGACY baselines (calibration batch, mean abs % err over quantity targets)
LEGACY_BASELINE={"fishkill_397":28.9,"364_main":36.9,"dutchess_livestock":268.2}
JOBS=[("fishkill_397", os.path.join(HERE,"spike_samples","397Fishkill.pdf")),
      ("364_main", os.path.join(HERE,"spike_samples","364Main.pdf")),
      ("dutchess_livestock", os.path.join(HERE,"golden","plans","Dutchess_Livestock_Bidding_Documents.pdf"))]

def log(m): print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {m}",flush=True)
def score(result,cid):
    data={"analysis":result.get("analysis",{}),"cost_estimate":result.get("cost_estimate",{})}
    m=rt.extract_metrics(data); rows=[]; 
    for k,sp in (rt.REFERENCE_CASES[cid].get("targets") or {}).items():
        if k in EXCL: continue
        t=sp[0] if isinstance(sp,(list,tuple)) else sp; a=m.get(k)
        if a is None or not t: continue
        rows.append((k,a,t,abs(float(a)-float(t))/float(t)*100))
    mean=sum(r[3] for r in rows)/len(rows) if rows else None
    return mean,rows

results={}
for cid,pdf in JOBS:
    if not os.path.exists(pdf):
        log(f"SKIP {cid} — plan missing"); continue
    results[cid]={}
    for mode in ("legacy","per_sheet"):
        os.environ["NIGHTSHIFT_PER_SHEET_EXTRACTION"]="1" if mode=="per_sheet" else "0"
        os.environ.pop("NIGHTSHIFT_MERGE_UNION",None)
        log(f"===== {cid} [{mode}] =====")
        try:
            r=run_analysis([pdf],contact_name="GR",contact_email="gr@k.local",
                           scope_notes="",rate_overrides=None,multi_pass=True)
            mean,rows=score(r,cid); results[cid][mode]=(mean,rows)
            log(f"  {cid} [{mode}] mean_err={mean:.1f}%")
            for k,a,t,e in rows: log(f"     {k}: {a:.0f} vs {t:.0f} ({e:.0f}%)")
        except Exception as ex:
            import traceback; log(f"  FAILED {cid} [{mode}]: {ex!r}"); traceback.print_exc()
            results[cid][mode]=(None,[])

log("\n\n========== GOLDEN REGRESSION VERDICT ==========")
log(f"{'job':20s} {'legacy':>10s} {'baseline':>10s} {'Δlegacy':>9s} {'per_sheet':>10s}")
for cid in results:
    leg=results[cid].get("legacy",(None,))[0]; ps=results[cid].get("per_sheet",(None,))[0]
    base=LEGACY_BASELINE.get(cid)
    dleg=(leg-base) if (leg is not None and base is not None) else None
    flag=""
    if dleg is not None and abs(dleg)>3: flag=" <-- LEGACY MOVED"
    log(f"{cid:20s} {('%.1f%%'%leg) if leg is not None else 'n/a':>10s} "
        f"{('%.1f%%'%base) if base else 'n/a':>10s} "
        f"{('%+.1f'%dleg) if dleg is not None else 'n/a':>9s} "
        f"{('%.1f%%'%ps) if ps is not None else 'n/a':>10s}{flag}")
log("REGRESSION = any legacy moved >3% from baseline. IMPROVEMENT = per_sheet << legacy.")
log("========== END ==========")
