#!/usr/bin/env python3
"""Northwell / Phelps Hospital Center for Digestive Health (JW RP 26-010-AUG).

Blind validation run: plans_clean.pdf is the 10/15/2025 architect pricing
set with all 1,729 annotation objects stripped (they were AutoCAD SHX
font-substitution markers, not estimator markups -- this set shipped
WITHOUT JW's answer key on sheet). MARKUP_TAKEOFF stays OFF regardless.

Flag construction is copied verbatim from nsai_k3_marathon_2026-08-25/
run_k3_child.py (the current validated JW-class posture, incl. K=3 job
draw median, FACES wall basis, mandatory review).

Usage: run_northwell.py
"""
import os, sys, json, re, time, shutil, traceback
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WT = os.path.join(os.path.dirname(ROOT), "nightshift-finishplan-wt")
BATCH = os.path.join(ROOT, "nsai_batch_2026-08-20")
sys.path.insert(0, WT)

BASELINE_FLAGS = {
    "NIGHTSHIFT_MARKUP_TAKEOFF": "0",
    "NIGHTSHIFT_VME_PRIMARY": "1",
    "NIGHTSHIFT_VME_AUTHORITATIVE_WALLS": "1",
    "NIGHTSHIFT_VECTOR_MEASURE": "1",
    "NIGHTSHIFT_PER_SHEET_EXTRACTION": "1",
    "NIGHTSHIFT_PROVENANCE_GATE": "1",
    "NIGHTSHIFT_STAIR_SHEET_EXTRACTION": "1",
    "NIGHTSHIFT_CLOSET_RECOVERY": "1",
}
NEW_FIX_FLAGS = {
    "NIGHTSHIFT_WILL_SCOPE_REMOVAL": "1",
    "NIGHTSHIFT_ELEV_REQUIRE_SHEETS": "1",
    "NIGHTSHIFT_STAIR_CROSS_SHEET_DEDUP": "1",
    "NIGHTSHIFT_WC_TYPICAL_MATCH": "1",
    "NIGHTSHIFT_DOOR_TYPICAL_TRANSFER": "1",
    "NIGHTSHIFT_FACTORY_FINISH_ALLOWANCE": "1",
    "NIGHTSHIFT_SALES_FLOOR_ACT_EVIDENCE": "1",
    "NIGHTSHIFT_ELEV_STRUCTURED_MEASURE": "1",
    "NIGHTSHIFT_PAINT_SCHEDULE_GATE": "1",
    "NIGHTSHIFT_SAME_FLOOR_ROOM_DEDUP": "1",
    "NIGHTSHIFT_WC_DEDUCT_FLOOR": "1",
    "NIGHTSHIFT_ELEV_PASS_CONSENSUS": "3",
    "NIGHTSHIFT_JOB_DRAW_MEDIAN": "3",
    "NIGHTSHIFT_CEILING_ASSUME_PAINTED_ACT": "1",
    "NIGHTSHIFT_WC_UNKNOWN_TOKEN_SAFE": "1",
}
JW_ONLY_FLAGS = {"NIGHTSHIFT_WALL_BASIS_FACES": "1"}

# 2026-09-01 fixes under test (commit 6d8be40)
FIX_FLAGS = {
    "NIGHTSHIFT_FINISH_PLAN_SCHEDULE": "1",      # detect A102 finish grid
    "NIGHTSHIFT_CEILING_SCHEDULE_EVIDENCE": "1", # ACT beats painted default
    "NIGHTSHIFT_SCHEDULE_ROOM_SCOPE": "1",       # drop unscheduled rooms
    "NIGHTSHIFT_OVER_EXTRACTION_GUARD": "1",     # high-side plausibility
    # now that the schedule is actually captured, the wall-side clip and
    # paint gate have something authoritative to work from
    "NIGHTSHIFT_SCHEDULE_SCOPE_AUTHORITATIVE": "1",
}


def build_flags():
    flags = dict(BASELINE_FLAGS)
    for line in open(os.path.join(BATCH, "rerun_batch.sh")):
        m = re.match(r"export\s+([A-Z0-9_]+)=(\S+)", line.strip())
        if m and m.group(1) != "NIGHTSHIFT_MARKUP_TAKEOFF":
            flags[m.group(1)] = m.group(2)
    flags.update(NEW_FIX_FLAGS)
    flags.update(JW_ONLY_FLAGS)          # JW class
    flags["NIGHTSHIFT_WC_UNKNOWN_TOKEN_SAFE"] = "paint"
    flags["NIGHTSHIFT_UNIT_MIX_PIN"] = "1"
    flags["NIGHTSHIFT_DRAW_MEDIAN_K_SMALL"] = "5"
    flags["NIGHTSHIFT_SCHEDULE_HEIGHT_SPLIT"] = "1"
    flags.update(FIX_FLAGS)
    flags["NIGHTSHIFT_MANDATORY_REVIEW"] = "1"
    return flags


def main():
    flags = build_flags()
    for k, v in flags.items():
        os.environ[k] = v
    pdf = os.path.join(HERE, "plans_clean.pdf")
    os.environ["NIGHTSHIFT_PROGRESS_FILE"] = os.path.join(HERE, "progress.txt")

    meta = {"job": "jw_northwell_phelps", "cls": "jw", "bid": 39004.63,
            "flags": flags, "pdf": pdf,
            "started_utc": datetime.now(timezone.utc).isoformat()}
    t0 = time.time()
    try:
        from Takeoff_DIRECT import run_analysis
        import regression_test as rt
        result = run_analysis(
            [pdf], contact_name="JW Northwell RERUN 2026-09-01",
            contact_email="sboyce80+northwell@gmail.com",
            scope_notes="", rate_overrides=None, multi_pass=True)
        for kind, src in (("result.json", result.get("output_json_path")),
                          ("result.pdf", result.get("output_pdf_path"))):
            if src and os.path.exists(src):
                shutil.copy(src, os.path.join(HERE, kind))
        analysis = result.get("analysis") or {}
        ce = result.get("cost_estimate", {}) or {}
        data = {"analysis": analysis, "cost_estimate": ce}
        meta.update({
            "ok": True,
            "subtotal": ce.get("subtotal", 0),
            "manual_review": bool(result.get("manual_review_required")),
            "metrics": rt.extract_metrics(data),
            "draw_median": analysis.get("_job_draw_median") or {},
            "elapsed_s": round(time.time() - t0, 1),
        })
    except Exception as e:
        meta.update({"ok": False, "error": repr(e),
                     "traceback": traceback.format_exc()[-4000:],
                     "elapsed_s": round(time.time() - t0, 1)})
    with open(os.path.join(HERE, "run_meta.json"), "w") as f:
        json.dump(meta, f, indent=2, default=str)
    print(json.dumps({k: v for k, v in meta.items() if k != "flags"},
                     indent=2, default=str)[:3000])
    sys.exit(0 if meta.get("ok") else 1)


if __name__ == "__main__":
    main()
