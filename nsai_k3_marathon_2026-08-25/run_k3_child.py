#!/usr/bin/env python3
"""K=3 marathon child: ONE golden job, marathon posture +
NIGHTSHIFT_JOB_DRAW_MEDIAN=3, run from the job-draw-median worktree.

Flag construction is identical to nsai_marathon_2026-08-23/
run_marathon_child.py (baseline + rerun_batch.sh set, class-gated
allowances JW-only, merged fix flags, mandatory review) plus the draw
median. Plans + rerun_batch.sh come from the canonical repo; the
worktree supplies the code under test and its own fresh checkpoints.

Usage: run_k3_child.py <job_key>
Writes results/<job>.json (score meta) + results/<job>.result.json.
"""
import os, sys, json, re, time, shutil, traceback
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WT = os.path.dirname(HERE)
MAIN = os.path.join(os.path.dirname(WT), "nightshift-repo")
BATCH = os.path.join(MAIN, "nsai_batch_2026-08-20")
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
CLASS_GATED = {
    "NIGHTSHIFT_CEILING_ASSUME_PAINTED",
    "NIGHTSHIFT_SEALED_CONCRETE_ALLOWANCE",
    "NIGHTSHIFT_WINDOW_SASH_OPS",
    "NIGHTSHIFT_LEVEL5_ALLOWANCE",
    "NIGHTSHIFT_POWER_WASH_ALLOWANCE",
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
    # THE VARIANCE PROGRAM
    "NIGHTSHIFT_JOB_DRAW_MEDIAN": "3",
    # Harlem ceilings (2026-08-25): ACT-by-room-function-heuristic rooms
    # join the enclosed-room painted default. Parent CEILING_ASSUME_
    # PAINTED is class-gated to JW, so this is inert on rider-class.
    "NIGHTSHIFT_CEILING_ASSUME_PAINTED_ACT": "1",
}

JOBS = {
    "dutchess_livestock": {"cls": "rider", "pdf": os.path.join(
        MAIN, "golden", "plans", "Dutchess_Livestock_Bidding_Documents.pdf"),
        "bid": 21072.45},
    "fishkill_397": {"cls": "rider", "pdf": os.path.join(
        MAIN, "spike_samples", "397Fishkill.pdf"), "bid": 129448.0},
    "364_main": {"cls": "rider", "pdf": os.path.join(
        MAIN, "spike_samples", "364Main.pdf"), "bid": 162456.0},
    "tsc_fusion_highland": {"cls": "rider", "pdf": os.path.join(
        MAIN, "golden", "plans", "TSC_Fusion_Highland_Rev2.pdf"),
        "bid": None},
    "honey_farms_malta": {"cls": "rider", "pdf": os.path.join(
        MAIN, "golden", "plans", "Honey_Farms_Malta_100pct_Pricing_Set.pdf"),
        "bid": 28564.0},
    "jw_harlem_valley": {"cls": "jw", "pdf": os.path.join(
        BATCH, "harlem_valley", "plans_clean.pdf"), "bid": 43490.84,
        "ref": "harlem_valley"},
    "jw_hudson_hotel": {"cls": "jw", "pdf": os.path.join(
        BATCH, "hudson_hotel", "plans_clean.pdf"), "bid": 146023.71,
        "ref": "hudson_hotel"},
    "jw_caris_hyde_park": {"cls": "jw", "pdf": os.path.join(
        BATCH, "caris_hyde_park", "plans_clean.pdf"), "bid": 87608.82,
        "ref": "caris_hyde_park"},
    "jw_under_canvas_ulum": {"cls": "jw", "pdf": os.path.join(
        BATCH, "under_canvas_ulum", "plans_clean.pdf"), "bid": 41575.45,
        "ref": "under_canvas_ulum"},
    "jw_homewood_suites": {"cls": "jw", "pdf": os.path.join(
        BATCH, "homewood_suites", "plans_clean.pdf"), "bid": 1322982.57,
        "ref": "homewood_suites"},
}


def build_flags(cls):
    flags = dict(BASELINE_FLAGS)
    for line in open(os.path.join(BATCH, "rerun_batch.sh")):
        m = re.match(r"export\s+([A-Z0-9_]+)=(\S+)", line.strip())
        if m and m.group(1) != "NIGHTSHIFT_MARKUP_TAKEOFF":
            flags[m.group(1)] = m.group(2)
    if cls != "jw":
        for k in CLASS_GATED:
            flags[k] = "0"
    flags.update(NEW_FIX_FLAGS)
    flags["NIGHTSHIFT_MANDATORY_REVIEW"] = "1"
    return flags


def main():
    job_key = sys.argv[1]
    spec = JOBS[job_key]
    flags = build_flags(spec["cls"])
    for k, v in flags.items():
        os.environ[k] = v
    out_dir = os.path.join(HERE, "results")
    os.makedirs(out_dir, exist_ok=True)

    meta = {"job": job_key, "cls": spec["cls"], "bid": spec.get("bid"),
            "flags": flags,
            "started_utc": datetime.now(timezone.utc).isoformat()}
    t0 = time.time()
    try:
        import regression_test as rt
        from jw_golden import JW_CASES
        from Takeoff_DIRECT import run_analysis
        # NOTE: no harness-level cold-draw retry here — the draw-median
        # orchestrator owns per-draw retries now.
        result = run_analysis(
            [spec["pdf"]], contact_name="K3 Marathon 2026-08-25",
            contact_email="sboyce80+k3marathon@gmail.com",
            scope_notes="", rate_overrides=None, multi_pass=True)
        for kind, src in (("result.json", result.get("output_json_path")),
                          ("result.pdf", result.get("output_pdf_path"))):
            if src and os.path.exists(src):
                shutil.copy(src, os.path.join(out_dir,
                                              f"{job_key}.{kind}"))
        analysis = result.get("analysis") or {}
        rep = analysis.get("_job_draw_median") or {}
        data = {"analysis": analysis,
                "cost_estimate": result.get("cost_estimate", {})}
        m = rt.extract_metrics(data)
        rows = []
        if spec["cls"] == "rider":
            targets = (rt.REFERENCE_CASES.get(job_key, {})
                       .get("targets") or {})
            for k, sp in targets.items():
                if k in ("cost_estimate_subtotal", "footprint_sqft"):
                    continue
                t = sp[0] if isinstance(sp, (list, tuple)) else sp
                a = m.get(k)
                if a is None or not t:
                    continue
                rows.append({"metric": k, "actual": float(a),
                             "target": float(t),
                             "err_pct": abs(float(a) - float(t))
                             / float(t) * 100})
        else:
            case = JW_CASES.get(job_key) or {}
            for k, (t, tol) in (case.get("targets") or {}).items():
                if k == "cost_estimate_subtotal":
                    continue
                a = m.get(k)
                if a is None or not t:
                    continue
                rows.append({"metric": k, "actual": float(a),
                             "target": float(t),
                             "err_pct": abs(float(a) - float(t))
                             / float(t) * 100})
        ce = result.get("cost_estimate", {}) or {}
        sub = float(ce.get("subtotal") or 0)
        bid = spec.get("bid")
        meta.update({
            "ok": True, "subtotal": sub,
            "delta_pct": (round((sub - bid) / bid * 100, 1)
                          if bid else None),
            "in_band_10": (abs(sub - bid) / bid <= 0.10) if bid else None,
            "mean_abs_err_pct": (sum(r["err_pct"] for r in rows)
                                 / len(rows) if rows else None),
            "rows": rows,
            "draw_report": {k: v for k, v in rep.items()
                            if k != "compositions"},
            "draw_compositions": rep.get("compositions"),
            "manual_review": bool(analysis.get("manual_review_required")),
            "elapsed_s": round(time.time() - t0, 1)})
    except Exception as e:
        meta.update({"ok": False, "error": repr(e),
                     "traceback": traceback.format_exc()[-4000:],
                     "elapsed_s": round(time.time() - t0, 1)})
    with open(os.path.join(out_dir, f"{job_key}.json"), "w") as f:
        json.dump(meta, f, indent=2)
    sys.exit(0 if meta.get("ok") else 1)


if __name__ == "__main__":
    main()
