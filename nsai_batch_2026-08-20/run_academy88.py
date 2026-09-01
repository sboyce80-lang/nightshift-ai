#!/usr/bin/env python3
"""Blind KnightShift run for RP 26-013 88 Academy Street (JW/RP submission).

Posture is byte-identical to run_k3_child.py's JW class (the validated
K=3 marathon configuration): baseline flags + rerun_batch.sh S4 set +
round-2/3 fix flags + draw-median, MARKUP_TAKEOFF hard OFF, plans
pre-stripped of every JW annotation (plans_clean.pdf).

Code under test: nightshift-round3-wt (k3-round3-fixes) — main does NOT
carry draw-median or the round-2/3 fixes.

Usage: run_academy88.py
"""
import os, sys, json, re, time, shutil, traceback
from datetime import datetime, timezone

MAIN = "/Users/stevenboyce/Desktop/_Code/NSAI/nightshift-repo"
WT = "/Users/stevenboyce/Desktop/_Code/NSAI/nightshift-round3-wt"
BATCH = os.path.join(MAIN, "nsai_batch_2026-08-20")
JOB_DIR = os.path.join(BATCH, "academy_88")
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
}


def build_flags():
    flags = dict(BASELINE_FLAGS)
    for line in open(os.path.join(BATCH, "rerun_batch.sh")):
        m = re.match(r"export\s+([A-Z0-9_]+)=(\S+)", line.strip())
        if m and m.group(1) != "NIGHTSHIFT_MARKUP_TAKEOFF":
            flags[m.group(1)] = m.group(2)
    flags.update(NEW_FIX_FLAGS)          # JW class: allowances stay ON
    flags["NIGHTSHIFT_WC_UNKNOWN_TOKEN_SAFE"] = "paint"
    flags["NIGHTSHIFT_UNIT_MIX_PIN"] = "1"
    flags["NIGHTSHIFT_DRAW_MEDIAN_K_SMALL"] = "5"
    flags["NIGHTSHIFT_SCHEDULE_HEIGHT_SPLIT"] = "1"
    flags["NIGHTSHIFT_DRAW_MEDIAN_SMALL_MAX_PAGES"] = "25"
    flags["NIGHTSHIFT_VME_REQUIRE_FLOOR_COVERAGE"] = "1"
    flags["NIGHTSHIFT_ELEV_TEXT_EVIDENCE"] = "1"
    flags["NIGHTSHIFT_MANDATORY_REVIEW"] = "1"
    return flags


def main():
    flags = build_flags()
    for k, v in flags.items():
        os.environ[k] = v
    os.environ["NIGHTSHIFT_PROGRESS_FILE"] = os.path.join(JOB_DIR, "progress.txt")
    pdf = os.path.join(JOB_DIR, "plans_clean.pdf")

    meta = {"job": "academy_88", "cls": "jw", "bid": 16163.70,
            "pdf": pdf, "flags": flags,
            "started_utc": datetime.now(timezone.utc).isoformat()}
    t0 = time.time()
    try:
        from Takeoff_DIRECT import run_analysis
        result = run_analysis(
            [pdf], contact_name="88 Academy Blind Validation",
            contact_email="sboyce80+academy88@gmail.com",
            scope_notes="", rate_overrides=None, multi_pass=True)
        for kind, src in (("json", result.get("output_json_path")),
                          ("pdf", result.get("output_pdf_path"))):
            if src and os.path.exists(src):
                shutil.copy(src, os.path.join(JOB_DIR, f"result.{kind}"))
        ce = result.get("cost_estimate", {}) or {}
        analysis = result.get("analysis") or {}
        meta.update({
            "ok": True,
            "subtotal": ce.get("subtotal", 0),
            "delta_pct": round((ce.get("subtotal", 0) - 16163.70) / 16163.70 * 100, 1),
            "manual_review": bool(result.get("manual_review_required")),
            "draw_median": analysis.get("_job_draw_median"),
            "elapsed_s": round(time.time() - t0, 1),
        })
    except Exception as e:
        meta.update({"ok": False, "error": repr(e),
                     "traceback": traceback.format_exc()[-4000:],
                     "elapsed_s": round(time.time() - t0, 1)})
    with open(os.path.join(JOB_DIR, "run_meta.json"), "w") as f:
        json.dump(meta, f, indent=2, default=str)
    print(json.dumps({k: v for k, v in meta.items() if k != "flags"},
                     indent=2, default=str)[:2000])
    sys.exit(0 if meta.get("ok") else 1)


if __name__ == "__main__":
    main()
