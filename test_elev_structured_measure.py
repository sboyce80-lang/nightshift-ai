#!/usr/bin/env python3
"""Structured elevation measurement (NIGHTSHIFT_ELEV_STRUCTURED_MEASURE):
exterior areas rebuild from per-elevation width×height rows with
recomputed arithmetic; scale-measured rows price but are tracked, and a
majority-scale RFI note ships. Live calibration: Fishkill rows sum 5,749
vs Rider 6,197 (−7%) where the freeform estimate ran 2.4×; Honey 3,431
vs 3,914 (−12%)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import Takeoff_DIRECT as T  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


# material routing
check(T._elev_material_key("Hardie V-Groove fiber cement") ==
      "hardie_siding_sqft", "hardie routing")
check(T._elev_material_key("painted CMU") == "exterior_paint_sqft",
      "cmu routing")
check(T._elev_material_key("soffit panels") == "soffit_sqft",
      "soffit routing")

# Fishkill-shaped: model claims 12,400 but documented rows justify 6,150;
# one estimated row (no printed dim) carries the rest.
ext = {
    "hardie_siding_sqft": 12400,
    "exterior_paint_sqft": 0,
    "elevation_breakdown": [
        {"elevation": "North", "material": "hardie", "width_ft": 88,
         "width_basis": "gridline 1-9 span 88'-0\"", "height_ft": 35,
         "height_basis": "EL. 565'-0\" to EL. 530'-0\" = 35.0 ft",
         "openings_deduct_sqft": 780, "area_sqft": 2300,
         "basis": "documented"},
        {"elevation": "South", "material": "hardie", "width_ft": 88,
         "width_basis": "88'-0\" printed", "height_ft": 35,
         "height_basis": "EL. 565'-0\" to EL. 530'-0\"",
         "openings_deduct_sqft": 200, "area_sqft": 2880,
         "basis": "documented"},
        {"elevation": "East", "material": "hardie", "width_ft": 30,
         "width_basis": "scaled from drawing", "height_ft": 35,
         "height_basis": "estimated from stories",
         "openings_deduct_sqft": 0, "area_sqft": 1050,
         "basis": "estimated"},
        # arithmetic-liar row: claims 9,999 but 20x10-0=200; documented
        {"elevation": "West", "material": "hardie", "width_ft": 20,
         "width_basis": "20'-0\" dim", "height_ft": 10,
         "height_basis": "12'-0\" floor-to-floor, 10 ft band",
         "openings_deduct_sqft": 0, "area_sqft": 9999,
         "basis": "documented"},
    ],
}
out = T._apply_elevation_breakdown(dict(ext))
# all rows price w/ recomputed arithmetic: doc 5380 + est 1050 = 6430
check(abs(out["hardie_siding_sqft"] - 6430.0) < 0.6,
      f"rebuild wrong: {out['hardie_siding_sqft']}")
check(abs(out["scale_measured_sqft"]["hardie_siding_sqft"] - 1050) < 0.6,
      f"scale bucket wrong: {out.get('scale_measured_sqft')}")
check(out["_structured_measure"]["status"] == "applied", "status missing")
check("scale-measured" in str(out.get("notes")), "measure note missing")

# documented claim without a numeric height basis demotes to estimated
ext2 = {"hardie_siding_sqft": 500, "elevation_breakdown": [
    {"elevation": "N", "material": "hardie", "width_ft": 50,
     "height_ft": 10, "height_basis": "typical height",
     "openings_deduct_sqft": 0, "area_sqft": 500,
     "basis": "documented"}]}
out2 = T._apply_elevation_breakdown(dict(ext2))
check(out2["hardie_siding_sqft"] == 500,
      f"undocumented row not priced-with-flag: {out2['hardie_siding_sqft']}")
check(out2["scale_measured_sqft"]["hardie_siding_sqft"] == 500,
      "undocumented row not tracked as scale-measured")
check("Majority" in str(out2.get("notes")), "majority-scale RFI note missing")

# no breakdown -> fail-open, totals kept, flagged
out3 = T._apply_elevation_breakdown({"hardie_siding_sqft": 900})
check(out3["hardie_siding_sqft"] == 900, "no-breakdown totals changed")
check(out3["_structured_measure"]["status"] == "no_breakdown",
      "no-breakdown flag missing")

# prompt gating
os.environ.pop("NIGHTSHIFT_ELEV_STRUCTURED_MEASURE", None)
check(not T._elev_structured_measure_enabled(), "flag default not off")
os.environ["NIGHTSHIFT_ELEV_STRUCTURED_MEASURE"] = "1"
check(T._elev_structured_measure_enabled(), "flag enable broken")
os.environ.pop("NIGHTSHIFT_ELEV_STRUCTURED_MEASURE", None)
check("MEASUREMENT DISCIPLINE" in T._ELEV_STRUCTURED_SECTION,
      "section constant malformed")

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
