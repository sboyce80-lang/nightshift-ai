#!/usr/bin/env python3
"""Marathon child: run ONE golden job at the candidate prod posture.

Flag model (prototypes the class-gate policy from ROADMAP_10PCT.md):
  ALL jobs   -> prod-equivalent baseline + the JW measurement-gate set
                (parsed from rerun_batch.sh) MINUS the class-gated flags,
                PLUS the merged fix flags (#36/#37), + mandatory review.
  JW-class   -> additionally the allowance flags + CEILING_ASSUME_PAINTED
                (JW's golden bids include that scope; Rider's do not).

Usage: run_marathon_child.py <job_key>
Writes results/<job>.json (score meta) + results/<job>.result.json (full).
"""
import os, sys, json, re, time, shutil, traceback
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

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
CLASS_GATED = {  # JW-class only (allowance convention + assume-painted)
    "NIGHTSHIFT_CEILING_ASSUME_PAINTED",
    "NIGHTSHIFT_SEALED_CONCRETE_ALLOWANCE",
    "NIGHTSHIFT_WINDOW_SASH_OPS",
    "NIGHTSHIFT_LEVEL5_ALLOWANCE",
    "NIGHTSHIFT_POWER_WASH_ALLOWANCE",
}
NEW_FIX_FLAGS = {  # merged PRs #36-#40, all classes
    "NIGHTSHIFT_WILL_SCOPE_REMOVAL": "1",
    "NIGHTSHIFT_ELEV_REQUIRE_SHEETS": "1",
    "NIGHTSHIFT_STAIR_CROSS_SHEET_DEDUP": "1",
    # round 2 (2026-08-24): typical->instance transfers, siding
    # allowance policy, ACT evidence guard
    "NIGHTSHIFT_WC_TYPICAL_MATCH": "1",
    "NIGHTSHIFT_DOOR_TYPICAL_TRANSFER": "1",
    "NIGHTSHIFT_FACTORY_FINISH_ALLOWANCE": "1",
    "NIGHTSHIFT_SALES_FLOOR_ACT_EVIDENCE": "1",
    # round 3 (2026-08-24): M1 structured exterior measurement
    "NIGHTSHIFT_ELEV_STRUCTURED_MEASURE": "1",
    # round 4 (2026-08-24): M3 paint schedule gate
    "NIGHTSHIFT_PAINT_SCHEDULE_GATE": "1",
    # round 5 (2026-08-24): same-floor dedup + WC deduct floor
    "NIGHTSHIFT_SAME_FLOOR_ROOM_DEDUP": "1",
    "NIGHTSHIFT_WC_DEDUCT_FLOOR": "1",
    # round 6 (2026-08-25): exterior-pass consensus
    "NIGHTSHIFT_ELEV_PASS_CONSENSUS": "3",
}

BATCH = os.path.join(ROOT, "nsai_batch_2026-08-20")
JOBS = {
    # Rider-class
    "dutchess_livestock": {"cls": "rider", "pdf": os.path.join(
        ROOT, "golden", "plans", "Dutchess_Livestock_Bidding_Documents.pdf"),
        "bid": 21072.45, "bid_note": "Jan26 revision final section (r20-37); June 22,758 superseded"},
    "fishkill_397": {"cls": "rider", "pdf": os.path.join(
        ROOT, "spike_samples", "397Fishkill.pdf"),
        "bid": 129448.0, "bid_note": "xlsx total incl exterior $45.6k"},
    "364_main": {"cls": "rider", "pdf": os.path.join(
        ROOT, "spike_samples", "364Main.pdf"), "bid": 162456.0},
    "tsc_fusion_highland": {"cls": "rider", "pdf": os.path.join(
        ROOT, "golden", "plans", "TSC_Fusion_Highland_Rev2.pdf"),
        "bid": None, "bid_note": "quantities only (xlsx carries no rates)"},
    "honey_farms_malta": {"cls": "rider", "pdf": os.path.join(
        ROOT, "golden", "plans", "Honey_Farms_Malta_100pct_Pricing_Set.pdf"),
        "bid": 28564.0},
    # JW-class (annotation-stripped plans; JW bids from jw_golden)
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
    os.environ["NIGHTSHIFT_PROGRESS_FILE"] = os.path.join(
        out_dir, f"{job_key}.progress.txt")

    meta = {"job": job_key, "cls": spec["cls"], "bid": spec.get("bid"),
            "flags": flags,
            "started_utc": datetime.now(timezone.utc).isoformat()}
    t0 = time.time()
    try:
        import regression_test as rt
        from jw_golden import JW_CASES
        from Takeoff_DIRECT import run_analysis
        result = run_analysis(
            [spec["pdf"]], contact_name="Marathon 2026-08-23",
            contact_email="sboyce80+marathon@gmail.com",
            scope_notes="", rate_overrides=None, multi_pass=True)
        # Cold-draw auto-retry (2026-08-25): the pipeline flags
        # implausible draws and clears their checkpoints; one fresh
        # retry, keep the second draw either way (its verdict is at
        # least as informative and mandatory review holds both).
        suspect = (result.get("analysis") or {}).get("_cold_draw_suspect")
        if suspect:
            print(f"   ♻️  cold-draw suspect {suspect} — one fresh retry",
                  flush=True)
            meta["cold_draw_first_attempt"] = suspect
            result = run_analysis(
                [spec["pdf"]], contact_name="Marathon 2026-08-23",
                contact_email="sboyce80+marathon@gmail.com",
                scope_notes="", rate_overrides=None, multi_pass=True)
        for kind, src in (("result.json", result.get("output_json_path")),
                          ("result.pdf", result.get("output_pdf_path"))):
            if src and os.path.exists(src):
                shutil.copy(src, os.path.join(out_dir,
                                              f"{job_key}.{kind}"))
        data = {"analysis": result.get("analysis", {}),
                "cost_estimate": result.get("cost_estimate", {})}
        m = rt.extract_metrics(data)
        # quantity targets: REFERENCE_CASES for rider, JW_CASES for jw
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
            "manual_review": bool((result.get("analysis") or {})
                                  .get("manual_review_required")
                                  or result.get("manual_review_required")),
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
