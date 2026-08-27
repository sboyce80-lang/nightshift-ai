#!/usr/bin/env python3
"""Legacy siding suppression stands down when the evidence gate ruled.

Honey K=3 round 1 (2026-08-27): the per-item exterior evidence gate
kept 3,714 SF of field-painted hardie as a strikeable allowance; then
calculate_costs' legacy per-JOB keyword scan hit 'pre-finished' (metal
downspouts / Longboard soffit — real factory items) and zeroed the
siding at pricing, printing a 0-sqft allowance line. When the gate has
ruled (its record is on the analysis), the keyword scan must not
override it; explicit scope-notes exclusions still win."""
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


def _hardie_line(costs):
    for li in costs["line_items"]:
        s = str(li.get("item", ""))
        if s.startswith("Ext. Hardie") or "Hardie Siding" in s:
            return li
    return None


EXT = {
    "exterior_paint_sqft": 0,
    "hardie_siding_sqft": 3714.0,
    "notes": ("Hardie siding painted PT01; pre-finished metal downspouts "
              "matching PT01; Longboard soffit pre-finished product"),
    "_factory_finish_allowance": {"hardie_siding_sqft": "quote"},
}
AGG = {"total_paintable_wall_sqft": 5000.0}

print("1) Gate ruled -> keyword suppression stands down, siding prices")
a = {"_exterior_evidence_gate": {"evidence": True, "zeroed": {}},
     "notes": []}
costs = T.calculate_costs(dict(AGG), exterior=dict(EXT),
                          building_type="commercial", analysis=a)
li = _hardie_line(costs)
check(li is not None and float(li.get("qty") or 0) == 3714.0
      and float(li.get("total") or 0) > 0,
      f"gate-kept hardie must price: {li}")

print("2) No gate record -> legacy suppression still applies")
costs = T.calculate_costs(dict(AGG), exterior=dict(EXT),
                          building_type="commercial", analysis={"notes": []})
li = _hardie_line(costs)
check(li is None or float(li.get("qty") or 0) == 0,
      f"legacy behavior must hold without the gate: {li}")

print("3) Explicit scope-notes exclusion beats the gate")
a = {"_exterior_evidence_gate": {"evidence": True, "zeroed": {}},
     "notes": []}
costs = T.calculate_costs(
    dict(AGG), exterior=dict(EXT), building_type="commercial",
    project_info={"_scope_notes": "no exterior paint"}, analysis=a)
li = _hardie_line(costs)
check(li is None or float(li.get("qty") or 0) == 0,
      f"user scope exclusion must always win: {li}")

print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ all suppression stand-down checks passed")
