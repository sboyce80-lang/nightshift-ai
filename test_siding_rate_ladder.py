#!/usr/bin/env python3
"""Siding rate ladder (Rider-derived, approved 2026-08-24): V-groove
profile rows price at $2.20/SF on their own line; panel stays $4.85.
Honey priced 2.2x high on rate alone."""
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


# breakdown classifies V-groove share
ext = {"hardie_siding_sqft": 4000, "elevation_breakdown": [
    {"elevation": "N", "material": "hardie V-Groove siding painted PT01",
     "width_ft": 100, "width_basis": "100'-0\"", "height_ft": 10,
     "height_basis": "10 ft per section", "openings_deduct_sqft": 0,
     "area_sqft": 1000, "basis": "documented"},
    {"elevation": "S", "material": "hardie panel siding",
     "width_ft": 100, "width_basis": "100'-0\"", "height_ft": 30,
     "height_basis": "EL. 30 ft", "openings_deduct_sqft": 0,
     "area_sqft": 3000, "basis": "documented"}]}
out = T._apply_elevation_breakdown(dict(ext))
check(out["siding_class_sqft"]["v_groove"] == 1000.0,
      f"v-groove class wrong: {out.get('siding_class_sqft')}")
check(out["hardie_siding_sqft"] == 4000.0,
      f"siding total wrong: {out['hardie_siding_sqft']}")

# pricing splits the lines at their rates
costs = T.calculate_costs(
    {"total_paintable_wall_sqft": 100},
    exterior={"hardie_siding_sqft": 4000,
              "siding_class_sqft": {"v_groove": 1000},
              "notes": "painted siding per schedule",
              "paint_evidence": "PAINT ALL SIDING"},
    building_type="commercial")
li = {str(x.get("item")): float(x.get("total") or 0)
      for x in costs["line_items"]}
vg = [k for k in li if "V-Groove" in k]
hp = [k for k in li if "Hardie" in k]
check(vg and "1,000 sqft @ $2.20" in vg[0],
      f"v-groove line wrong: {vg}")
check(hp and "3,000 sqft @ $4.85" in hp[0],
      f"panel line wrong: {hp}")

# no class data -> single line, unchanged behavior
costs2 = T.calculate_costs(
    {"total_paintable_wall_sqft": 100},
    exterior={"hardie_siding_sqft": 4000,
              "notes": "painted siding", "paint_evidence": "PAINT SIDING"},
    building_type="commercial")
li2 = [str(x.get("item")) for x in costs2["line_items"]
       if "Siding" in str(x.get("item"))]
check(any("4,000 sqft @ $4.85" in s for s in li2),
      f"legacy single line broken: {li2}")
check(all("V-Groove" not in s or "- 0 sqft" in s for s in li2),
      f"phantom v-groove line: {li2}")

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
