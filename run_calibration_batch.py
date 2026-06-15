#!/usr/bin/env python3
"""Batch calibration runner (Phase 2.4 activation).

Generates calibration rows for golden/calibration_data.json by RUNNING the
pipeline on each verified project's plan set and measuring the result against
its tier-1 regression targets. A row = {confidence inputs from the run} ->
{true per-job error vs Rider's verified quantities}. Once the table has
>= confidence.MIN_CALIBRATION rows, confidence.predict_error switches from the
evidence-model prior to data-calibrated bins.

Each project is run SEQUENTIALLY (per-sheet on, checkpoints on, MERGE_UNION
off — prod config) so API rate limits and worker memory aren't contended.
Re-running is cheap: per-sheet checkpoints persist, so a project already
extracted skips straight to the cached sheets.

true_error_pct = mean absolute percent error across the case's QUANTITY
targets only (walls/ceilings/doors/trim/windows/stairs). Cost subtotal and
footprint are excluded: KS rates differ from Rider's, so $ error is not an
extraction-accuracy signal (the fishkill_397 case notes this explicitly).

Add a project by dropping its plan PDF locally and listing it in MANIFEST
with its regression case id.

Usage: python3 run_calibration_batch.py [case_id ...]   (default: all in MANIFEST)
"""
import os, sys, json, math
from datetime import datetime, timezone

# Calibrate the DEPLOYED pipeline. Per-sheet extraction is OFF in prod (still
# unstable — 2026-06-13 Fishkill walls swung 51k->101k on the same PDF), so
# confidence must be calibrated against the legacy path that customers actually
# get. Flip NIGHTSHIFT_PER_SHEET_EXTRACTION=1 here only once per-sheet is
# stabilized AND becomes the prod default.
os.environ.setdefault("NIGHTSHIFT_PER_SHEET_EXTRACTION", "0")
os.environ.pop("NIGHTSHIFT_MERGE_UNION", None)
os.environ.pop("NIGHTSHIFT_MERGE_PREFER_COMPLETE", None)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import regression_test as rt
import confidence as C
from Takeoff_DIRECT import run_analysis

CALIB_PATH = os.path.join(HERE, "golden", "calibration_data.json")

# case_id -> local plan PDF. Extend as more verified plan sets are pulled in.
MANIFEST = {
    "fishkill_397": os.path.join(HERE, "spike_samples", "397Fishkill.pdf"),
    "364_main": os.path.join(HERE, "spike_samples", "364Main.pdf"),
    "dutchess_livestock": os.path.join(
        HERE, "golden", "plans", "Dutchess_Livestock_Bidding_Documents.pdf"),
}

# Quantity targets only (extraction accuracy); $ + footprint excluded.
_EXCLUDE_FROM_ERROR = {"cost_estimate_subtotal", "footprint_sqft"}


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def true_error_pct(result, case_id):
    """Mean absolute percent error of the run vs the case's quantity targets."""
    case = rt.REFERENCE_CASES[case_id]
    data = {"analysis": result.get("analysis", {}),
            "cost_estimate": result.get("cost_estimate", {})}
    metrics = rt.extract_metrics(data)
    errs, detail = [], []
    for key, spec in (case.get("targets") or {}).items():
        if key in _EXCLUDE_FROM_ERROR:
            continue
        target = spec[0] if isinstance(spec, (list, tuple)) else spec
        actual = metrics.get(key)
        if actual is None or not target:
            continue
        e = abs(float(actual) - float(target)) / float(target) * 100.0
        errs.append(e)
        detail.append(f"{key}: {actual:.0f} vs {target:.0f} ({e:.0f}%)")
    if not errs:
        return None, detail
    return round(sum(errs) / len(errs), 1), detail


def load_table():
    if os.path.exists(CALIB_PATH):
        try:
            with open(CALIB_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"min_calibration": C.MIN_CALIBRATION, "rows": []}


def main():
    only = set(sys.argv[1:])
    table = load_table()
    rows = {r.get("job"): r for r in table.get("rows", [])}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for case_id, pdf in MANIFEST.items():
        if only and case_id not in only:
            continue
        log(f"===== {case_id} =====")
        if not os.path.exists(pdf):
            log(f"  SKIP — plan PDF missing: {pdf}")
            continue
        case = rt.REFERENCE_CASES.get(case_id)
        if not case or case.get("tier") != 1:
            log(f"  SKIP — {case_id} is not a tier-1 verified case")
            continue
        try:
            result = run_analysis([pdf], contact_name="Calibration",
                                  contact_email="calib@knightshift.local",
                                  scope_notes="", rate_overrides=None,
                                  multi_pass=True)
            inputs = C.compute_confidence_inputs(
                result.get("analysis", {}), result.get("cost_estimate", {}))
            te, detail = true_error_pct(result, case_id)
            if te is None:
                log(f"  WARN — no comparable quantity targets for {case_id}")
                continue
            rows[case_id] = {
                "job": case_id,
                "verified_on": today,
                "true_error_pct": te,
                "true_error_metric": "mean abs % error vs tier-1 quantity targets",
                "detail": detail,
                "inputs": inputs,
            }
            log(f"  DONE {case_id}: true_error={te}% | "
                f"predicted(uncal)={C.predict_error(inputs)['predicted_error_pct']}%")
            for d in detail:
                log(f"     {d}")
            # Persist incrementally so a mid-batch crash keeps prior rows.
            table["rows"] = list(rows.values())
            table["min_calibration"] = C.MIN_CALIBRATION
            with open(CALIB_PATH, "w") as f:
                json.dump(table, f, indent=2)
        except Exception as e:
            import traceback
            log(f"  FAILED {case_id}: {e!r}")
            traceback.print_exc()

    n = len(table.get("rows", []))
    log(f"CALIBRATION TABLE: {n} row(s) "
        f"({'ACTIVE' if n >= C.MIN_CALIBRATION else f'need {C.MIN_CALIBRATION - n} more to activate'})")


if __name__ == "__main__":
    main()
