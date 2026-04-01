#!/usr/bin/env python3
"""
Nightshift AI — Regression Test Suite
======================================
Compares output JSON files against known-good reference values to catch
regressions after code changes.

Usage:
    # Check specific output(s) against references
    python3 regression_test.py --check output/construction_analysis_*.json
    python3 regression_test.py --check ~/Downloads/construction_analysis_20260331_195708.json

    # Snapshot current output as a new reference (prints dict entry to paste in)
    python3 regression_test.py --snapshot output/some_file.json --name "my_project"

    # List all reference cases
    python3 regression_test.py --list
"""

import argparse
import json
import glob
import os
import sys

# ---------------------------------------------------------------------------
# Reference cases — validated against manual takeoffs or Rider review
# Format: metric_name: (expected_value, tolerance_fraction)
#   e.g., (85353, 0.15) means expected=85353, pass if within ±15%
# Assertions: (metric, operator, value, message) — hard pass/fail checks
# ---------------------------------------------------------------------------
REFERENCE_CASES = {
    "364_main": {
        "display_name": "364 Main Street, Beacon",
        "match_keywords": ["364", "main", "beacon"],
        "source": "Rider manual Excel takeoff (364 Mainstreet Beacon Take Offs)",
        "targets": {
            "total_paintable_wall_sqft": (85353, 0.15),
            "total_paintable_ceiling_sqft": (26839, 0.15),
            "total_base_trim_lf": (8629, 0.15),
            "total_doors_full_paint": (155, 0.25),
            "total_doors_hm_panel": (28, 0.25),
            "total_windows_painted_interior": (26, 0.30),
            "total_stair_sections": (11, 0.25),
            "cost_estimate_subtotal": (162456, 0.15),
        },
        "assertions": [],
    },
    "grenadier_danbury": {
        "display_name": "Grenadier of Danbury (Dealership)",
        "match_keywords": ["grenadier", "danbury"],
        "source": "Rider review (March 2026)",
        "targets": {
            "total_doors_full_paint": (8, 0.50),
            "total_doors_hm_panel": (25, 0.20),
            "total_stair_sections": (5, 0.40),
        },
        "assertions": [
            ("total_doors_full_paint", ">", 0,
             "Storefront filter must not zero all full-paint doors"),
        ],
    },
    "route22_condo": {
        "display_name": "Route 22 Condo B (Silo Ridge)",
        "match_keywords": ["4651"],
        "source": "Rider review (March 2026)",
        "targets": {
            "total_doors_full_paint": (85, 0.15),
            "total_windows_painted_interior": (108, 0.10),
        },
        "assertions": [
            ("effective_rooms", "<", 100,
             "Should not double-count units from typical + floor plans"),
        ],
    },
}


# ---------------------------------------------------------------------------
# Metric extraction from output JSON
# ---------------------------------------------------------------------------
def extract_metrics(data):
    """Pull all testable metrics from an output JSON dict."""
    analysis = data.get("analysis", {})
    agg = analysis.get("aggregated_totals", {})
    pi = analysis.get("project_info", {})
    cost = data.get("cost_estimate", {})

    metrics = {}

    # Aggregated totals
    for key in ("total_paintable_wall_sqft", "total_paintable_ceiling_sqft",
                "total_cmu_wall_sqft", "total_dryfall_ceiling_sqft",
                "total_base_trim_lf", "total_doors_full_paint",
                "total_doors_hm_panel", "total_doors_frame_only",
                "total_windows_painted_interior", "total_windows_all",
                "total_stair_sections", "total_gyp_between_stairs_sqft",
                "total_level_5_finish_sqft", "total_concrete_floor_sqft",
                "total_wallcovering_sqft", "total_soffit_sqft"):
        val = agg.get(key)
        if val is not None:
            metrics[key] = float(val)

    # Cost
    if cost.get("subtotal") is not None:
        metrics["cost_estimate_subtotal"] = float(cost["subtotal"])

    # Project info
    for key in ("total_stories", "total_units", "footprint_sqft"):
        val = pi.get(key)
        if val is not None:
            metrics[key] = float(val)

    # Effective rooms (computed)
    effective = 0
    for floor in analysis.get("floors", []):
        for room in floor.get("rooms", []):
            mult = room.get("unit_multiplier", 1)
            if mult is None:
                mult = 1
            effective += max(1, int(float(mult)))
    metrics["effective_rooms"] = float(effective)

    # Template rooms
    template = sum(len(f.get("rooms", [])) for f in analysis.get("floors", []))
    metrics["template_rooms"] = float(template)

    return metrics


def identify_project(data):
    """Match an output JSON to a reference case by keywords in document name/notes."""
    doc_name = str(data.get("document", "")).lower()
    source_files = " ".join(str(f) for f in (data.get("source_files") or [])).lower()
    pi = data.get("analysis", {}).get("project_info", {})
    project_name = str(pi.get("project_name", "")).lower()
    building_type = str(pi.get("building_type", "")).lower()
    notes_text = " ".join(
        str(n) for n in data.get("analysis", {}).get("notes", [])
    ).lower()

    search_text = f"{doc_name} {source_files} {project_name} {building_type} {notes_text}"

    for case_id, case in REFERENCE_CASES.items():
        keywords = case["match_keywords"]
        if all(kw.lower() in search_text for kw in keywords):
            return case_id
    return None


# ---------------------------------------------------------------------------
# Check logic
# ---------------------------------------------------------------------------
def check_file(json_path, verbose=True):
    """Check a single output JSON against its matching reference. Returns (pass, fail, skip) counts."""
    with open(json_path, "r") as f:
        data = json.load(f)

    case_id = identify_project(data)
    if case_id is None:
        if verbose:
            doc = data.get("document", os.path.basename(json_path))
            print(f"\n⚪ {os.path.basename(json_path)}")
            print(f"   Document: {doc}")
            print(f"   No matching reference case found — skipped")
        return 0, 0, 1

    case = REFERENCE_CASES[case_id]
    metrics = extract_metrics(data)

    if verbose:
        print(f"\n{'='*72}")
        print(f"  Project: {case['display_name']}")
        print(f"  File:    {os.path.basename(json_path)}")
        print(f"  Ref:     {case['source']}")
        print(f"{'='*72}")
        print()
        print(f"  {'Metric':<35} {'Expected':>10} {'Actual':>10} {'Tol':>6} {'Status'}")
        print(f"  {'─'*68}")

    passed = 0
    failed = 0

    for metric_name, (expected, tolerance) in case.get("targets", {}).items():
        actual = metrics.get(metric_name)
        if actual is None:
            status = "⚠️  MISSING"
            pct_str = ""
            actual_str = "N/A"
            failed += 1
        else:
            if expected == 0:
                pct = 0.0 if actual == 0 else 1.0
            else:
                pct = actual / expected
            within = abs(1.0 - pct) <= tolerance

            if within:
                status = f"✅ PASS ({pct:.0%})"
                passed += 1
            else:
                status = f"❌ FAIL ({pct:.0%})"
                failed += 1
            actual_str = f"{actual:,.0f}"

        if verbose:
            print(f"  {metric_name:<35} {expected:>10,.0f} {actual_str:>10} "
                  f"{'±' + str(int(tolerance*100)) + '%':>6} {status}")

    # Assertions
    assertions = case.get("assertions", [])
    if assertions and verbose:
        print()
        print(f"  {'Assertions'}")
        print(f"  {'─'*68}")

    for metric_name, op, value, message in assertions:
        actual = metrics.get(metric_name)
        if actual is None:
            ok = False
            actual_str = "N/A"
        else:
            actual_str = f"{actual:,.0f}"
            if op == ">":
                ok = actual > value
            elif op == "<":
                ok = actual < value
            elif op == ">=":
                ok = actual >= value
            elif op == "<=":
                ok = actual <= value
            elif op == "==":
                ok = actual == value
            elif op == "!=":
                ok = actual != value
            else:
                ok = False

        if ok:
            passed += 1
            status = "✅ PASS"
        else:
            failed += 1
            status = "❌ FAIL"

        if verbose:
            print(f"  {status} {metric_name} {op} {value}: "
                  f"actual={actual_str} — {message}")

    if verbose:
        print()
        total = passed + failed
        if failed == 0:
            print(f"  RESULT: {passed}/{total} PASSED ✅")
        else:
            print(f"  RESULT: {passed}/{total} passed, {failed} FAILED ❌")

    return passed, failed, 0


# ---------------------------------------------------------------------------
# Snapshot mode
# ---------------------------------------------------------------------------
def snapshot_file(json_path, name):
    """Print a reference case dict entry from an output JSON."""
    with open(json_path, "r") as f:
        data = json.load(f)

    metrics = extract_metrics(data)
    doc = data.get("document", os.path.basename(json_path))

    print(f"\n# Snapshot from: {os.path.basename(json_path)}")
    print(f"# Document: {doc}")
    print(f'"{name}": {{')
    print(f'    "display_name": "{name}",')
    print(f'    "match_keywords": ["FILL_IN"],')
    print(f'    "source": "Snapshot {os.path.basename(json_path)}",')
    print(f'    "targets": {{')
    for key, val in sorted(metrics.items()):
        if val > 0:
            print(f'        "{key}": ({val:.0f}, 0.15),')
    print(f'    }},')
    print(f'    "assertions": [],')
    print(f'}},')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Nightshift AI Regression Test Suite")
    parser.add_argument("--check", nargs="+",
                        help="JSON file(s) or directory to check against references")
    parser.add_argument("--snapshot", type=str,
                        help="JSON file to snapshot as a new reference")
    parser.add_argument("--name", type=str, default="new_project",
                        help="Name for snapshot reference case")
    parser.add_argument("--list", action="store_true",
                        help="List all reference cases")
    args = parser.parse_args()

    if args.list:
        print("\n=== Nightshift AI Regression Reference Cases ===\n")
        for case_id, case in REFERENCE_CASES.items():
            print(f"  {case_id}: {case['display_name']}")
            print(f"    Source: {case['source']}")
            print(f"    Keywords: {case['match_keywords']}")
            print(f"    Metrics: {len(case.get('targets', {}))} targets, "
                  f"{len(case.get('assertions', []))} assertions")
            print()
        return

    if args.snapshot:
        snapshot_file(args.snapshot, args.name)
        return

    if args.check:
        # Expand glob patterns and directories
        json_files = []
        for path in args.check:
            if os.path.isdir(path):
                json_files.extend(sorted(glob.glob(os.path.join(path, "*.json"))))
            elif "*" in path:
                json_files.extend(sorted(glob.glob(path)))
            elif os.path.isfile(path):
                json_files.append(path)
            else:
                print(f"⚠️  Not found: {path}")

        if not json_files:
            print("No JSON files found to check.")
            sys.exit(1)

        total_passed = 0
        total_failed = 0
        total_skipped = 0
        matched_files = 0

        print(f"\n{'='*72}")
        print(f"  Nightshift AI Regression Test Suite")
        print(f"  Checking {len(json_files)} file(s)")
        print(f"{'='*72}")

        for jf in json_files:
            p, f, s = check_file(jf)
            total_passed += p
            total_failed += f
            total_skipped += s
            if s == 0:
                matched_files += 1

        print(f"\n{'='*72}")
        print(f"  SUMMARY: {matched_files} project(s) matched, "
              f"{total_skipped} skipped")
        print(f"  Metrics: {total_passed} passed, {total_failed} failed")
        if total_failed > 0:
            print(f"  STATUS: ❌ REGRESSION DETECTED")
            sys.exit(1)
        elif matched_files == 0:
            print(f"  STATUS: ⚠️  No matching reference cases found")
            sys.exit(0)
        else:
            print(f"  STATUS: ✅ ALL PASSED")
            sys.exit(0)
        print(f"{'='*72}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
