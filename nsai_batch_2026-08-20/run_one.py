#!/usr/bin/env python3
"""Run ONE batch job through run_analysis. Invoked by run_batch.py as a
subprocess so the parent can enforce wall-clock timeouts and survive crashes.

Usage: run_one.py <job_key> <multi_pass:0|1>
Writes <job_dir>/result.json (KnightShift output), result.pdf, and
<job_dir>/run_meta.json (subtotal / manual_review / timing / error).
"""
import os, sys, json, time, shutil, traceback
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# --- Prod-equivalent flag set (mirrors Render env group as of 2026-08-11) ---
FLAGS = {
    # NIGHTSHIFT_MARKUP_TAKEOFF deliberately OFF (user directive 2026-08-20):
    # fresh independent takeoff — never measure JW's markups. Plans are also
    # pre-stripped of all annotations (plans_clean.pdf) so no vision pass
    # can read his quantities off the sheets.
    "NIGHTSHIFT_MARKUP_TAKEOFF": "0",
    "NIGHTSHIFT_VME_PRIMARY": "1",             # ON since 7/8
    "NIGHTSHIFT_VME_AUTHORITATIVE_WALLS": "1", # ON since 7/8
    "NIGHTSHIFT_VECTOR_MEASURE": "1",
    "NIGHTSHIFT_PER_SHEET_EXTRACTION": "1",    # accuracy flags ON 6/15
    "NIGHTSHIFT_PROVENANCE_GATE": "1",
    "NIGHTSHIFT_STAIR_SHEET_EXTRACTION": "1",  # PR #23 shipped ON 7/20
    "NIGHTSHIFT_CLOSET_RECOVERY": "1",
    # ceiling scope gate + calibrated confidence default ON in code
}
for k, v in FLAGS.items():
    os.environ.setdefault(k, v)

JOBS = {
    "harlem_valley":    {"label": "JW 26-376 Harlem Valley Homestead"},
    "hudson_hotel":     {"label": "JW 26-390 Hudson Hotel (West Point)"},
    "caris_hyde_park":  {"label": "JW 26-385 Caris Of Hyde Park"},
    "under_canvas_ulum":{"label": "40-211 Under Canvas Hudson Valley ULUM"},
    "homewood_suites":  {"label": "RP 26-002 Homewood Suites"},
}

def main():
    job_key, mp = sys.argv[1], sys.argv[2] == "1"
    job_dir = os.path.join(HERE, job_key)
    pdf = os.path.join(job_dir, "plans_clean.pdf")  # annotations pre-stripped
    os.environ["NIGHTSHIFT_PROGRESS_FILE"] = os.path.join(job_dir, "progress.txt")
    meta = {"job": job_key, "label": JOBS[job_key]["label"], "multi_pass": mp,
            "started_utc": datetime.now(timezone.utc).isoformat()}
    t0 = time.time()
    try:
        from Takeoff_DIRECT import run_analysis
        result = run_analysis(
            [pdf],
            contact_name="JW Batch Validation",
            contact_email="sboyce80+batch@gmail.com",
            scope_notes="", rate_overrides=None,
            multi_pass=mp,
        )
        for kind, src in (("json", result.get("output_json_path")),
                          ("pdf", result.get("output_pdf_path"))):
            if src and os.path.exists(src):
                shutil.copy(src, os.path.join(job_dir, f"result.{kind}"))
        ce = result.get("cost_estimate", {}) or {}
        meta.update({
            "ok": True,
            "subtotal": ce.get("subtotal", 0),
            "manual_review": bool(result.get("manual_review_required")),
            "elapsed_s": round(time.time() - t0, 1),
        })
    except Exception as e:
        meta.update({"ok": False, "error": repr(e),
                     "traceback": traceback.format_exc()[-4000:],
                     "elapsed_s": round(time.time() - t0, 1)})
    with open(os.path.join(job_dir, "run_meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    sys.exit(0 if meta.get("ok") else 1)

if __name__ == "__main__":
    main()
