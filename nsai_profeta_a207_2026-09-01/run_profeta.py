#!/usr/bin/env python3
"""Full takeoff of 168 Holley St (Profeta / BASC Offices) with finish-plan
discovery ON, to measure the dollar movement the probe could not.

Baseline is the 2026-09-01 production run: subtotal $34,139.02, base trim
388 LF, wallcovering 0 SF, 40/59 rooms "GYP (assumed)", epoxy 0 SF.

Usage: run_profeta.py [0|1]     (finish-plan discovery flag; default 1)
Writes result.json / result.pdf / run_meta.json into this directory.
"""
import json, os, shutil, sys, time, traceback
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

mode = sys.argv[1] if len(sys.argv) > 1 else "1"

# Prod-equivalent flag set (mirrors the Render env group), matching
# nsai_batch_2026-08-20/run_one.py so this is comparable to the batch runs.
FLAGS = {
    "NIGHTSHIFT_MARKUP_TAKEOFF": "0",
    "NIGHTSHIFT_VME_PRIMARY": "1",
    "NIGHTSHIFT_VME_AUTHORITATIVE_WALLS": "1",
    "NIGHTSHIFT_VECTOR_MEASURE": "1",
    "NIGHTSHIFT_PER_SHEET_EXTRACTION": "1",
    "NIGHTSHIFT_PROVENANCE_GATE": "1",
    "NIGHTSHIFT_STAIR_SHEET_EXTRACTION": "1",
    "NIGHTSHIFT_CLOSET_RECOVERY": "1",
    "NIGHTSHIFT_WC_SCHEDULE_GATE": "1",
    "NIGHTSHIFT_FINISH_PLAN_DISCOVERY": mode,
}
for k, v in FLAGS.items():
    os.environ[k] = v
os.environ["NIGHTSHIFT_PROGRESS_FILE"] = os.path.join(HERE, f"progress_{mode}.txt")

pdf = os.path.join(HERE, "plans_clean.pdf")
meta = {"job": "profeta_168_holley", "flag_finish_plan_discovery": mode,
        "baseline_subtotal": 34139.02,
        "started_utc": datetime.now(timezone.utc).isoformat()}
t0 = time.time()
try:
    from Takeoff_DIRECT import run_analysis
    result = run_analysis([pdf], contact_name="A207 Validation",
                          contact_email="sboyce80+a207@gmail.com",
                          scope_notes="", rate_overrides=None, multi_pass=False)
    for kind, src in (("json", result.get("output_json_path")),
                      ("pdf", result.get("output_pdf_path"))):
        if src and os.path.exists(src):
            shutil.copy(src, os.path.join(HERE, f"result_{mode}.{kind}"))
    a = result.get("analysis", {}) or {}
    agg = a.get("aggregated_totals", {}) or {}
    meta.update({
        "ok": True,
        "subtotal": (result.get("cost_estimate", {}) or {}).get("subtotal", 0),
        "base_trim_lf": agg.get("total_base_trim_lf"),
        "wallcovering_sqft": agg.get("total_wallcovering_sqft"),
        "epoxy_wall_sqft": agg.get("total_epoxy_wall_sqft"),
        "wall_sqft": agg.get("total_paintable_wall_sqft"),
        "ceiling_sqft": agg.get("total_paintable_ceiling_sqft"),
        "wc_gate": a.get("_wc_schedule_gate"),
        "rfs_rows": len(a.get("schedule_data", {}).get("room_finish_schedule") or []),
        "manual_review": bool(result.get("manual_review_required")),
        "elapsed_s": round(time.time() - t0, 1),
    })
except Exception as e:
    meta.update({"ok": False, "error": repr(e),
                 "traceback": traceback.format_exc()[-4000:],
                 "elapsed_s": round(time.time() - t0, 1)})
json.dump(meta, open(os.path.join(HERE, f"run_meta_{mode}.json"), "w"), indent=2)
print(json.dumps({k: v for k, v in meta.items() if k != "traceback"}, indent=2))
sys.exit(0 if meta.get("ok") else 1)
