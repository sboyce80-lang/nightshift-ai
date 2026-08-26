#!/usr/bin/env python3
"""Interior-only bid convention (NIGHTSHIFT_INTERIOR_ONLY_CONVENTION).

Steven 2026-08-25: Dutchess (+100%) and 364 Main (+20%) missed almost
entirely on exterior/window scope their Rider bids exclude, while
Fishkill's Rider bid INCLUDES exterior — per-customer convention, never
a class default. Locks in: flag off = untouched; on = exterior dict +
window-op aggregates struck to $0 with stashed quantities, note + RFI;
interior painted windows survive; idempotent."""
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


def _analysis():
    return {
        "exterior": {"exterior_paint_sqft": 3200.0,
                     "hardie_siding_sqft": 1500.0,
                     "window_trim_lf": 240.0,
                     "notes": "south elevation painted siding"},
        "aggregated_totals": {
            "total_paintable_wall_sqft": 9000.0,
            "total_windows_painted_interior": 12,
            "total_window_casings_painted": 25,
            "total_window_stools_painted": 25,
            "total_windows_field_paintable": 25,
        },
        "notes": [],
    }


print("1) Flag off -> untouched")
os.environ.pop("NIGHTSHIFT_INTERIOR_ONLY_CONVENTION", None)
a = T._enforce_interior_only_convention(_analysis())
check(a.get("_interior_only_convention") is None
      and a["exterior"]["exterior_paint_sqft"] == 3200.0,
      "flag off must not touch the analysis")

print("2) Flag on -> exterior + window ops struck, stash kept")
os.environ["NIGHTSHIFT_INTERIOR_ONLY_CONVENTION"] = "1"
a = T._enforce_interior_only_convention(_analysis())
rec = a["_interior_only_convention"]
check(a["exterior"]["exterior_paint_sqft"] == 0
      and a["exterior"]["hardie_siding_sqft"] == 0
      and a["exterior"]["window_trim_lf"] == 0,
      f"exterior quantities not struck: {a['exterior']}")
check(a["aggregated_totals"]["total_window_casings_painted"] == 0
      and a["aggregated_totals"]["total_windows_field_paintable"] == 0,
      "window-op aggregates not struck")
check(rec["struck_quantities"].get("exterior_paint_sqft") == 3200.0
      and rec["struck_quantities"].get("agg.total_window_stools_painted")
      == 25, f"stash incomplete: {rec}")
check(a["aggregated_totals"]["total_windows_painted_interior"] == 12,
      "interior painted windows must survive")
check(a["aggregated_totals"]["total_paintable_wall_sqft"] == 9000.0,
      "interior walls must survive")
check(any("Interior-Only Convention" in str(n) for n in a["notes"]),
      "strikeable note missing")
rfis = a.get("_pre_pricing_rfis") or a.get("rfi_items") or []
check(any("interior-only convention" in str(r).lower()
          for r in rfis) or any(
    "Scope Convention" in str(r) for r in rfis),
    f"RFI missing: {rfis}")

print("3) Idempotent")
a2 = T._enforce_interior_only_convention(a)
check(a2["_interior_only_convention"] is rec, "second pass must no-op")

print("4) Nothing to strike -> clean record, no note")
os.environ["NIGHTSHIFT_INTERIOR_ONLY_CONVENTION"] = "1"
clean = {"exterior": {}, "aggregated_totals":
         {"total_paintable_wall_sqft": 5000.0}, "notes": []}
c = T._enforce_interior_only_convention(clean)
check(c["_interior_only_convention"]["applied"] is False
      and not c["notes"], "empty exterior must not add notes/RFIs")

print("5) Struck quantities cannot reach the cost estimate")
a3 = _analysis()
a3 = T._enforce_interior_only_convention(a3)
costs = T.calculate_costs(a3["aggregated_totals"],
                          exterior=a3["exterior"],
                          building_type="commercial",
                          analysis=a3)
ext_lines = [li for li in costs.get("line_items", [])
             if any(t in str(li.get("item", "")).lower()
                    for t in ("exterior", "hardie", "siding",
                              "window trim", "sash"))]
check(not any(_l for _l in ext_lines if T._num(_l.get("total")) > 0),
      f"struck scope still priced: {ext_lines}")

os.environ.pop("NIGHTSHIFT_INTERIOR_ONLY_CONVENTION", None)
print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ all interior-only convention checks passed")
