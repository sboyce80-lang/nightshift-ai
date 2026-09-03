#!/usr/bin/env python3
"""Record what geometry would have billed — on every job, changing no price.

Phase 2 of the scope-inversion plan. Across 16 job records the vector
measurement engine is permitted to own the wall number on 5; six of the
eleven abstentions are scope judgements rather than measurement failures.
We had no systematic record of how far the discarded geometric number sat
from the billed one, so the Phase 4 decision (make confirmed scope
authoritative, retire the inference guards) would have rested on an
argument instead of a corpus.

The first two records disagree in OPPOSITE directions — Profeta bills 35%
under geometry, ULUM bills 2x over it — which is precisely why the question
cannot be settled by reasoning about it.

Locks in: (1) a record is written on both the promoted and abstained paths,
(2) it NEVER moves a price, (3) the kill switch makes it fully inert.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CLAUDE_API_KEY", "x")
os.environ["NIGHTSHIFT_VME_AUTHORITATIVE_WALLS"] = "1"

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  X {msg}")
    else:
        print("  ok " + msg.split(":")[0])


def load(cf_flag):
    os.environ["NIGHTSHIFT_VME_COUNTERFACTUAL"] = cf_flag
    sys.modules.pop("Takeoff_DIRECT", None)
    import Takeoff_DIRECT as T
    return T


def _analysis(run_lf=1747.3, heights=(9, 9, 10, 10, 10), billed=10277.0,
              footprint=5400, in_scope_area=7002, pages=3):
    rooms = [{"in_scope": True,
              "dimensions": {"ceiling_height_feet": h,
                             "floor_area_sqft": in_scope_area / len(heights),
                             "wall_area_sqft": 100}}
             for h in heights]
    return {
        "project_info": {"footprint_sqft": footprint},
        "floors": [{"rooms": rooms}],
        "aggregated_totals": {"total_paintable_wall_sqft": billed,
                              "total_cmu_wall_sqft": 0},
        "_vme_shadow_v2": {
            "total_wall_run_lf": run_lf, "n_floor_pages": pages,
            "by_page": [{"page": i, "wall_run_lf": run_lf / pages,
                         "scale_source": "text"} for i in range(pages)]},
        "notes": [], "rfi_items": [],
    }


T = load("1")

# --- 1) An abstaining job still records what geometry would have billed.
a = _analysis()
T._apply_vme_authoritative_walls(a)
cf = a.get("_vme_counterfactual") or {}
check(bool(cf), "no counterfactual written on the abstain path")
check(cf.get("promoted") is False, "abstention recorded as promoted")
check(cf.get("geometric_wall_run_lf") == 1747.3,
      f"run LF not carried: {cf.get('geometric_wall_run_lf')}")
check(cf.get("geometric_wall_sqft") and cf["geometric_wall_sqft"] > 0,
      f"geometric SF not computed: {cf.get('geometric_wall_sqft')}")
check(cf.get("billed_wall_sqft") == 10277.0,
      f"billed figure not captured: {cf.get('billed_wall_sqft')}")
check(cf.get("geom_over_billed"), "ratio not computed")
check(cf.get("abstain_reason"), "abstain reason not carried")
check(cf.get("scale_sources") == ["text"],
      f"scale provenance lost: {cf.get('scale_sources')}")

# --- 2) It NEVER moves a price. This is the whole contract of Phase 2.
for label, kw in (("abstaining", {}),
                  ("few heights", {"heights": (9,)}),
                  ("no geometry", {"run_lf": 0.0})):
    base = _analysis(**kw)
    before = dict(base["aggregated_totals"])
    a2 = copy.deepcopy(base)
    T._apply_vme_authoritative_walls(a2)
    check(a2["aggregated_totals"] == before,
          f"{label}: aggregated_totals moved — Phase 2 must not reprice")

# --- 3) Recording survives the earliest abstention (before heights exist).
a3 = _analysis(heights=(9,))
T._apply_vme_authoritative_walls(a3)
cf3 = a3.get("_vme_counterfactual") or {}
check(bool(cf3), "no record on the too-few-heights abstention")
check(cf3.get("n_room_heights") == 1,
      f"height count not captured: {cf3.get('n_room_heights')}")
check(cf3.get("geometric_wall_sqft") is None,
      "invented a geometric SF without a measured height")

# --- 4) Kill switch is fully inert.
T0 = load("0")
a4 = _analysis()
T0._apply_vme_authoritative_walls(a4)
check("_vme_counterfactual" not in a4, "recorder ran with the flag OFF")

sys.modules.pop("Takeoff_DIRECT", None)
os.environ.pop("NIGHTSHIFT_VME_COUNTERFACTUAL", None)
import Takeoff_DIRECT as T
a5 = _analysis()
T._apply_vme_authoritative_walls(a5)
check("_vme_counterfactual" in a5, "recorder is not ON by default")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
