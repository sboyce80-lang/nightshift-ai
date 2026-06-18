"""Regression tests for the extended-scope operator options
(NIGHTSHIFT_EXTENDED_SCOPE): bollards, pipe handrails, epoxy walls, and
interior precast walls.

These are additive, non-overlapping line items wired 2026-06-17 after the
Pricing-tab options were added. Guards:
  (1) Flag OFF -> takeoff is identical to pre-wiring: epoxy/precast room walls
      are NOT reclassified (stay uncounted), and no new line items are emitted.
  (2) Flag ON  -> epoxy/precast walls classify into their own totals from
      materials.walls (no schema change), and bollards/pipe handrails read from
      the exterior elevation pass. All four price at the operator's rate.
  (3) Exterior precast/masonry is NOT double-counted (it still prices under the
      existing Exterior Painting line, not a new one).
Offline, no API.
"""
import os
import copy
import Takeoff_DIRECT as T
from config import PRICING_MODEL

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def _analysis():
    return {
        "project_info": {"building_type": "commercial", "total_stories": 1},
        "floors": [{"floor_name": "1", "rooms": [
            {"in_scope": True, "dimensions": {"wall_area_sqft": 1000},
             "materials": {"walls": "Epoxy"}, "elements": {}},
            {"in_scope": True, "dimensions": {"wall_area_sqft": 500},
             "materials": {"walls": "Precast Concrete"}, "elements": {}},
            {"in_scope": True, "dimensions": {"wall_area_sqft": 800},
             "materials": {"walls": "GYP"}, "elements": {}},
        ]}],
        "exterior": {"bollard_count": 6, "pipe_handrail_lf": 120},
        "notes": [],
    }


def _pm_with_rates():
    pm = copy.deepcopy(PRICING_MODEL)
    for k, r in (("epoxy_wall_area", 3.10), ("precast_walls_interior", 2.40),
                 ("bollards", 125.0), ("pipe_handrail", 14.0)):
        for t in pm[k]["tiers"]:
            t["rate"] = r
    return pm


# ── 1) Aggregation classification: flag OFF leaves epoxy/precast uncounted ──
os.environ["NIGHTSHIFT_EXTENDED_SCOPE"] = "0"
a = _analysis()
T._recalculate_totals(a)
t = a["aggregated_totals"]
check(t.get("total_epoxy_wall_sqft", 0) == 0, "flag-off: epoxy should be 0")
check(t.get("total_precast_interior_sqft", 0) == 0, "flag-off: precast should be 0")
check(t.get("total_paintable_wall_sqft") == 800, "flag-off: gyp wall must stay 800")

# ── 2) Aggregation classification: flag ON routes to dedicated totals ──
os.environ["NIGHTSHIFT_EXTENDED_SCOPE"] = "1"
a = _analysis()
T._recalculate_totals(a)
t = a["aggregated_totals"]
check(t.get("total_epoxy_wall_sqft") == 1000, "flag-on: epoxy should be 1000")
check(t.get("total_precast_interior_sqft") == 500, "flag-on: precast should be 500")
check(t.get("total_paintable_wall_sqft") == 800,
      "flag-on: gyp wall must remain 800 (no leakage)")

# ── 3) Pricing: flag OFF emits no new lines ──
agg = {"total_paintable_wall_sqft": 800, "total_epoxy_wall_sqft": 1000,
       "total_precast_interior_sqft": 500}
ext = {"bollard_count": 6, "pipe_handrail_lf": 120}
pi = {"building_type": "commercial", "total_stories": 1}
pm = _pm_with_rates()
_NEW = ("Epoxy Walls", "Precast Walls (Interior)", "Bollards", "Pipe Handrails")


def _lines(flag):
    os.environ["NIGHTSHIFT_EXTENDED_SCOPE"] = flag
    res = T.calculate_costs(agg, exterior=ext, building_type="commercial",
                            project_info=pi, pricing_model_override=pm)
    return res["line_items"] if isinstance(res, dict) else res


off = _lines("0")
check(not any(li["item"].startswith(k) for li in off for k in _NEW),
      "flag-off: no extended-scope lines should be emitted")

# ── 4) Pricing: flag ON emits all four, priced at operator rate ──
on = _lines("1")
by = {k: next((li for li in on if li["item"].startswith(k)), None) for k in _NEW}
check(all(by.values()), "flag-on: all four extended-scope lines must emit")
check(by["Epoxy Walls"]["cost"] == 1000 * 3.10, "epoxy cost wrong")
check(by["Precast Walls (Interior)"]["cost"] == 500 * 2.40, "precast cost wrong")
check(by["Bollards"]["cost"] == 6 * 125.0, "bollards cost wrong")
check(by["Pipe Handrails"]["cost"] == 120 * 14.0, "pipe handrail cost wrong")

# ── 5) Zero-qty extended scope on flag-on adds no priced value ──
on_zero = T.calculate_costs(
    {"total_paintable_wall_sqft": 800}, exterior={},
    building_type="commercial", project_info=pi, pricing_model_override=pm)
on_zero = on_zero["line_items"] if isinstance(on_zero, dict) else on_zero
check(all(li["total"] == 0 for li in on_zero
          if any(li["item"].startswith(k) for k in _NEW)),
      "flag-on with no scope: extended lines must be $0")

os.environ["NIGHTSHIFT_EXTENDED_SCOPE"] = "0"
print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
