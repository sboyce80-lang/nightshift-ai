"""Offline replay: prove HARD_NUMBERS_ONLY suppresses fabricated scope on the
Ridgeview run without touching Claude.

Loads the cached prod extraction, re-runs _recalculate_totals + calculate_costs
with the policy ON (assert fabrications gone, boosts intact) and OFF (assert the
old 486 LF cornice / 8,355 SF wallcovering reappear → reversible).

Run:  .venv/bin/python scripts/verify_hard_numbers_ridgeview.py
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import Takeoff_DIRECT as td

SRC = REPO / "output" / "ridgeview_RIDER_223103.json"


def _cornice_qty(line_items):
    for li in line_items:
        if li["item"].startswith("Exterior Cornice"):
            return li["qty"]
    return None


def _line_qty(line_items, prefix):
    for li in line_items:
        if li["item"].startswith(prefix):
            return li["qty"]
    return None


def run(policy: bool):
    """Replay aggregation + costing on a fresh copy of the analysis."""
    td.HARD_NUMBERS_ONLY = policy  # toggle module-level flag at runtime
    data = json.loads(SRC.read_text())
    analysis = copy.deepcopy(data["analysis"])
    # Force the heuristics to recompute from per-room data rather than trusting
    # the cached (post-heuristic) aggregated_totals.
    analysis.pop("aggregated_totals", None)

    td._recalculate_totals(analysis)
    agg = analysis["aggregated_totals"]

    costs = td.calculate_costs(
        agg,
        exterior=analysis.get("exterior", {}),
        building_type=analysis.get("project_info", {}).get("building_type", ""),
        project_info=analysis.get("project_info", {}),
        analysis=analysis,
    )
    li = costs["line_items"]
    return {
        "wallcovering_sqft": agg.get("total_wallcovering_sqft", 0),
        "dryfall_sqft": agg.get("total_dryfall_ceiling_sqft", 0),
        "stained_wood_sqft": agg.get("total_stained_wood_sqft", 0),
        "wall_sqft": agg.get("total_paintable_wall_sqft", 0),
        "doors_full": agg.get("total_doors_full_paint", 0),
        "cornice_qty": _cornice_qty(li),
        "footprint_interior_line": _line_qty(li, "Interior (Footprint)"),
        "subtotal": costs["subtotal"],
    }


def main() -> int:
    if not SRC.exists():
        print(f"FATAL: {SRC} not found", file=sys.stderr)
        return 2

    on = run(True)
    off = run(False)

    print("metric                     policy ON        policy OFF")
    print("-" * 60)
    for k in ("wallcovering_sqft", "dryfall_sqft", "stained_wood_sqft",
              "wall_sqft", "doors_full", "cornice_qty",
              "footprint_interior_line", "subtotal"):
        print(f"{k:<26} {str(on[k]):>12}     {str(off[k]):>12}")

    print("\nAssertions (policy ON = hard numbers only):")
    checks = [
        ("cornice == 0", on["cornice_qty"] == 0),
        ("wallcovering == 0", on["wallcovering_sqft"] == 0),
        ("dryfall == 0", on["dryfall_sqft"] == 0),
        ("stained wood == 0", on["stained_wood_sqft"] == 0),
        ("no footprint-pricing line", on["footprint_interior_line"] is None),
        ("walls retained (>0, boosts intact)", on["wall_sqft"] > 0),
        ("doors retained (>0)", on["doors_full"] > 0),
        ("reversible: cornice returns w/ flag off", (off["cornice_qty"] or 0) > 0),
        ("reversible: wallcovering returns w/ flag off",
         (off["wallcovering_sqft"] or 0) > 0),
        ("ON subtotal < OFF subtotal (fabrications removed)",
         on["subtotal"] < off["subtotal"]),
        ("doors unchanged ON vs OFF (boosts/supplements not gated)",
         on["doors_full"] == off["doors_full"]),
        ("walls ON >= OFF (gated WC carve-out returns to paint)",
         on["wall_sqft"] >= off["wall_sqft"]),
    ]
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
