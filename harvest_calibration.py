#!/usr/bin/env python3
"""Phase 2.1 — harvest calibration rows from EXISTING pipeline outputs.

A calibration row = {confidence evidence inputs from a run} -> {true % error
vs that job's verified tier-1 targets}. run_calibration_batch.py generates these
by RE-RUNNING the pipeline (slow, API). But every saved output/construction_
analysis_*.json already IS a completed run — so we can build rows from them with
no new API spend, matching each output to its verified case by building name /
source file.

Calibration targets the DEPLOYED path: by default only legacy (per-sheet OFF)
runs are harvested (pass --include-per-sheet to also take per-sheet runs, kept
as distinct job ids). Quantity targets only; cost/footprint excluded (KS rates
differ from Rider's).

Usage:
  python3 harvest_calibration.py --dry-run        # show candidate rows
  python3 harvest_calibration.py                  # append new rows
  python3 harvest_calibration.py --include-per-sheet
"""
import argparse
import glob
import json
import os

import confidence as C
import regression_test as rt

HERE = os.path.dirname(os.path.abspath(__file__))
CALIB = os.path.join(HERE, "golden", "calibration_data.json")
EXCLUDE = {"cost_estimate_subtotal", "footprint_sqft"}


def _job_text(doc):
    """All identifying strings for matching: building name + source files."""
    parts = []
    bi = doc.get("building_inventory") or {}
    parts.append(str(bi.get("project_name", "")))
    for f in (doc.get("source_files") or []):
        parts.append(str(f))
    for f in (doc.get("files_analyzed") or []):
        parts.append(str(f))
    return " ".join(parts).lower()


def match_case(doc):
    txt = _job_text(doc)
    for cid, case in rt.REFERENCE_CASES.items():
        if case.get("tier") != 1 or not case.get("targets"):
            continue
        if any(kw.lower() in txt for kw in case.get("match_keywords", [])):
            return cid
    return None


def true_error_pct(doc, case_id):
    case = rt.REFERENCE_CASES[case_id]
    data = {"analysis": doc.get("analysis", {}) or {},
            "cost_estimate": doc.get("cost_estimate", {}) or {}}
    metrics = rt.extract_metrics(data)
    errs, detail = [], []
    for key, spec in (case.get("targets") or {}).items():
        if key in EXCLUDE:
            continue
        target = spec[0] if isinstance(spec, (list, tuple)) else spec
        actual = metrics.get(key)
        if actual is None or not target:
            continue
        e = abs(float(actual) - float(target)) / float(target) * 100.0
        errs.append(e)
        detail.append(f"{key}: {actual:.0f} vs {target:.0f} ({e:.0f}%)")
    return (sum(errs) / len(errs) if errs else None), detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-per-sheet", action="store_true")
    ap.add_argument("--glob", default="output/construction_analysis_*.json")
    args = ap.parse_args()

    table = json.load(open(CALIB)) if os.path.exists(CALIB) else {"min_calibration": C.MIN_CALIBRATION, "rows": []}
    existing_paths = {r.get("output_path") for r in table.get("rows", [])}

    cands = []
    for f in sorted(glob.glob(os.path.join(HERE, args.glob)), key=os.path.getmtime):
        base = os.path.basename(f)
        try:
            doc = json.load(open(f))
        except Exception:
            continue
        cid = match_case(doc)
        if not cid:
            continue
        a = doc.get("analysis", {}) or {}
        per_sheet = bool(a.get("_per_sheet_extraction"))
        if per_sheet and not args.include_per_sheet:
            continue
        if base in existing_paths:
            continue
        te, detail = true_error_pct(doc, cid)
        if te is None:
            continue
        inputs = C.compute_confidence_inputs(a, doc.get("cost_estimate", {}) or {})
        # Quality gate: a complete modern run has a coverage signal (the ledger).
        # Runs with coverage_pct=None predate the ledger / are partial — they
        # carry incomplete evidence and would only add noise to the calibration.
        if inputs.get("coverage_pct") is None:
            continue
        mode = "persheet" if per_sheet else "legacy"
        cands.append({
            "job": f"{cid}_{mode}_{base.split('_')[-1].replace('.json','')}",
            "case_id": cid, "mode": mode, "true_error_pct": round(te, 1),
            "true_error_metric": "mean abs % error vs tier-1 quantity targets",
            "detail": detail, "inputs": inputs, "output_path": base,
        })

    # Keep up to PER_CASE_CAP most-recent runs per case — run-to-run variance
    # is legitimate signal (predict_error bins by evidence score), but cap so a
    # job with many saved runs (fishkill) can't dominate the table.
    PER_CASE_CAP = 3
    counts, dedup = {}, []
    for c in reversed(cands):          # most recent first
        key = c["case_id"]
        if counts.get(key, 0) >= PER_CASE_CAP:
            continue
        counts[key] = counts.get(key, 0) + 1
        dedup.append(c)
    dedup.reverse()

    print(f"Existing rows: {len(table['rows'])}  (need {C.MIN_CALIBRATION})")
    print(f"Harvestable NEW rows (deduped by case+mode): {len(dedup)}")
    for c in dedup:
        i = c["inputs"]
        print(f"  + {c['job']:42} err={c['true_error_pct']:6.1f}%  "
              f"cov={i.get('coverage_pct')} anchor={i.get('anchor_pct')} "
              f"n_rooms={i.get('n_rooms')} gate={i.get('hard_gate_tripped')}")
    total = len(table["rows"]) + len(dedup)
    print(f"\nProjected total: {total}  -> calibration {'ACTIVE' if total >= C.MIN_CALIBRATION else 'still dormant'}")

    if args.dry_run:
        print("\n(dry-run — no rows written)")
        return
    for c in dedup:
        table["rows"].append({k: v for k, v in c.items() if k not in ("case_id", "mode")})
    json.dump(table, open(CALIB, "w"), indent=2)
    print(f"\nWrote {len(dedup)} rows -> {CALIB}  (status: {C.calibration_status(CALIB)})")


if __name__ == "__main__":
    main()
