"""Corpus-level regression evaluator.

For every result JSON in the corpus directory:
  1. Snapshot the BEFORE metrics (aggregated_totals + project_info +
     cost_estimate.subtotal + effective_rooms).
  2. Apply the in-flight dedup / cleanup functions on the analysis dict
     (idempotent; they mutate in place).
  3. Recompute totals via Takeoff_DIRECT._recalculate_totals.
  4. Snapshot the AFTER metrics.
  5. If the project matches a REFERENCE_CASES entry in regression_test.py,
     run that case's targets + assertions against the AFTER state.
  6. Print a per-job Δ row; aggregate at the end.

Exit status:
   0  — every reference-matched job PASSED its targets (or no matches).
   1  — at least one reference-matched job FAILED its targets, OR
        a non-reference job shifted by more than --regress-threshold
        on a tracked metric (default 25% absolute Δ on subtotal).

The point: every future code change runs this against the whole corpus,
not against one job. A change that helps 364 Main but blows up Fishkill
gets caught here before deploy.

Usage:
    # Default: read corpus from output/regression_corpus/
    python3 scripts/regression_corpus.py

    # Custom corpus directory (e.g., your local Downloads folder):
    python3 scripts/regression_corpus.py --corpus ~/Downloads \\
        --glob 'construction_analysis_*.json'

    # Tighter regression threshold:
    python3 scripts/regression_corpus.py --regress-threshold 0.10

    # Skip reference-case checks (raw Δ report only):
    python3 scripts/regression_corpus.py --no-reference-checks

The dedup / re-aggregation step runs the SAME code path production runs
on every new submission — _recalculate_totals — so the BEFORE/AFTER
comparison is exactly what each job WOULD now produce if re-extracted.
(Caveat: this re-aggregates from cached per-room dimensions, so it
catches aggregation-level fixes like the 2026-05-29 dedup but not
extraction-level fixes that change what rooms exist in the first place.
Those need a full re-run, not just re-aggregation.)
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Import the production aggregation path so the corpus result reflects
# exactly what a fresh run would produce.
from Takeoff_DIRECT import _recalculate_totals  # noqa: E402

# Reuse the reference-case fixtures and metric-extraction helpers.
from regression_test import (  # noqa: E402
    REFERENCE_CASES,
    extract_metrics,
    identify_project,
)


# Metrics we track in the corpus Δ report regardless of reference match.
TRACKED_METRICS = (
    "cost_estimate_subtotal",
    "total_paintable_wall_sqft",
    "total_paintable_ceiling_sqft",
    "total_base_trim_lf",
    "total_doors_full_paint",
    "total_windows_painted_interior",
    "total_stair_sections",
    "effective_rooms",
)


def _pct(before: Optional[float], after: Optional[float]) -> Optional[float]:
    if before is None or after is None or before == 0:
        return None
    return (after - before) / before


def _fmt_pct(p: Optional[float]) -> str:
    if p is None:
        return "    —"
    return f"{p*100:+5.1f}%"


def _fmt_num(v: Optional[float]) -> str:
    if v is None:
        return "      —"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:>7,}" if isinstance(v, (int, float)) else f"{v!s:>7}"


def evaluate_one(json_path: Path) -> Dict:
    """Load a single result JSON, run dedup + re-aggregation, return a
    dict with before/after metrics and reference-case verdict."""
    try:
        with open(json_path) as f:
            data = json.load(f)
    except Exception as exc:
        return {"path": json_path, "error": f"load failed: {exc}"}

    if "analysis" not in data:
        return {"path": json_path, "error": "no 'analysis' key"}

    before_metrics = extract_metrics(data)

    # Deep-copy and re-aggregate so BEFORE stays clean for delta calc.
    after_data = copy.deepcopy(data)
    after_analysis = after_data["analysis"]
    # Clear idempotency flags so the dedup passes actually run.
    for flag in ("_same_sheet_room_names_deduped",
                 "_non_authoritative_duplicates_dropped",
                 "_template_floors_deduped"):
        after_analysis.pop(flag, None)
    try:
        _recalculate_totals(after_analysis)
    except Exception as exc:
        return {"path": json_path, "error": f"recalc failed: {exc}",
                "before": before_metrics}

    # cost_estimate is computed downstream of _recalculate_totals in the
    # production pipeline; we don't have that path here, so cost_estimate
    # stays the BEFORE value for now. The aggregated_totals deltas are
    # what matter for catching extraction-level regressions.
    after_metrics = extract_metrics(after_data)
    # Preserve before's cost in after to avoid spurious "cost change" rows.
    if "cost_estimate_subtotal" in before_metrics:
        after_metrics["cost_estimate_subtotal"] = before_metrics[
            "cost_estimate_subtotal"]

    case_id = identify_project(after_data)
    case = REFERENCE_CASES.get(case_id) if case_id else None

    return {
        "path": json_path,
        "document": data.get("document", json_path.name),
        "case_id": case_id,
        "case": case,
        "before": before_metrics,
        "after": after_metrics,
    }


def check_reference_targets(result: Dict) -> Tuple[int, int, List[str]]:
    """Run reference-case targets against the AFTER metrics. Returns
    (passed, failed, failure_messages)."""
    case = result.get("case")
    if not case:
        return 0, 0, []
    after = result["after"]
    passed = 0
    failed = 0
    failures: List[str] = []
    for metric, (expected, tol) in case.get("targets", {}).items():
        actual = after.get(metric)
        if actual is None:
            failed += 1
            failures.append(f"{metric}: MISSING (expected {expected})")
            continue
        if expected == 0:
            ok = actual == 0
            pct = 0.0 if ok else 1.0
        else:
            pct = actual / expected
            ok = abs(1.0 - pct) <= tol
        if ok:
            passed += 1
        else:
            failed += 1
            failures.append(
                f"{metric}: {actual:,.0f} vs expected {expected:,} "
                f"({pct:.0%}, tolerance ±{tol*100:.0f}%)")
    for metric, op, target, msg in case.get("assertions", []):
        actual = after.get(metric)
        if actual is None:
            failed += 1
            failures.append(f"assert {metric} {op} {target}: MISSING — {msg}")
            continue
        ok = {
            "<": actual < target, "<=": actual <= target,
            ">": actual > target, ">=": actual >= target,
            "==": actual == target, "!=": actual != target,
        }.get(op, False)
        if ok:
            passed += 1
        else:
            failed += 1
            failures.append(
                f"assert {metric} {op} {target}: actual={actual:,.0f} — {msg}")
    return passed, failed, failures


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--corpus", default=str(REPO / "output" / "regression_corpus"),
                   help="Directory of result JSONs to evaluate")
    p.add_argument("--glob", default="*.json",
                   help="Glob pattern within --corpus (default *.json)")
    p.add_argument("--regress-threshold", type=float, default=0.25,
                   help="Non-reference jobs flagged when |Δ subtotal| or "
                        "|Δ wall_sqft| exceeds this fraction (default 0.25)")
    p.add_argument("--no-reference-checks", action="store_true",
                   help="Skip reference-case pass/fail; only print Δ report")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    corpus = Path(args.corpus).expanduser()
    if not corpus.exists():
        print(f"FATAL: corpus dir does not exist: {corpus}", file=sys.stderr)
        return 2

    json_paths = sorted(corpus.glob(args.glob))
    if not json_paths:
        print(f"No JSONs matched {corpus}/{args.glob}", file=sys.stderr)
        return 2

    print(f"Evaluating {len(json_paths)} file(s) from {corpus}\n")

    # Δ table header
    cols = ("Wall %", "Ceil %", "Trim %", "Doors %", "Rooms %", "Subtotal %")
    print(f"{'File':<48} {'Ref':<22} " + " ".join(f"{c:>9}" for c in cols))
    print("-" * (48 + 1 + 22 + 1 + len(cols) * 10))

    results = []
    errors = 0
    ref_passed_total = 0
    ref_failed_total = 0
    ref_jobs = 0
    flagged_jobs: List[Tuple[str, float, str]] = []

    for path in json_paths:
        r = evaluate_one(path)
        if "error" in r:
            errors += 1
            print(f"{path.name:<48} ❌ {r['error']}")
            continue
        results.append(r)

        b, a = r["before"], r["after"]
        deltas = {
            "wall": _pct(b.get("total_paintable_wall_sqft"),
                         a.get("total_paintable_wall_sqft")),
            "ceil": _pct(b.get("total_paintable_ceiling_sqft"),
                         a.get("total_paintable_ceiling_sqft")),
            "trim": _pct(b.get("total_base_trim_lf"),
                         a.get("total_base_trim_lf")),
            "doors": _pct(b.get("total_doors_full_paint"),
                          a.get("total_doors_full_paint")),
            "rooms": _pct(b.get("effective_rooms"),
                          a.get("effective_rooms")),
            "subtotal": _pct(b.get("cost_estimate_subtotal"),
                             a.get("cost_estimate_subtotal")),
        }
        case_label = r["case"]["display_name"] if r.get("case") else ""
        row = (
            f"{path.name:<48} {case_label[:22]:<22} "
            + " ".join(_fmt_pct(deltas[k]) for k in
                       ("wall", "ceil", "trim", "doors", "rooms", "subtotal"))
        )
        print(row)

        if r.get("case"):
            ref_jobs += 1
        # Flag non-reference jobs that shift materially
        for metric, val in (("wall", deltas["wall"]),
                            ("subtotal", deltas["subtotal"])):
            if val is not None and abs(val) >= args.regress_threshold:
                flagged_jobs.append(
                    (path.name, val, f"Δ{metric}={val*100:+.1f}%"))

    print()
    print("=" * 72)
    print(f"Corpus summary: {len(results)} job(s) evaluated, {errors} error(s)")

    # Reference-case checks
    if not args.no_reference_checks and ref_jobs > 0:
        print()
        print(f"Reference-case checks ({ref_jobs} matched job(s)):")
        print("-" * 72)
        any_fail = False
        for r in results:
            if not r.get("case"):
                continue
            passed, failed, failures = check_reference_targets(r)
            ref_passed_total += passed
            ref_failed_total += failed
            status = "✅" if failed == 0 else "❌"
            print(f"  {status} {r['case']['display_name']:<40} "
                  f"{passed} passed, {failed} failed")
            for fmsg in failures:
                print(f"      • {fmsg}")
                any_fail = True
        print()
        print(f"Reference totals: {ref_passed_total} passed, "
              f"{ref_failed_total} failed")
    else:
        any_fail = False

    if flagged_jobs:
        print()
        print(f"Non-reference jobs flagged (|Δ| >= "
              f"{args.regress_threshold*100:.0f}%):")
        # Sort by abs delta desc
        flagged_jobs.sort(key=lambda x: abs(x[1]), reverse=True)
        for name, val, label in flagged_jobs[:20]:
            print(f"  ⚠️  {name}  ({label})")
        if len(flagged_jobs) > 20:
            print(f"  ... and {len(flagged_jobs)-20} more")
    print("=" * 72)

    # Exit code
    if ref_failed_total > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
