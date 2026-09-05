#!/usr/bin/env python3
"""VME ceilings gate (NIGHTSHIFT_VME_CEILINGS).

Locks in: polygons replace painted-ceiling areas in BOTH directions;
classification is respected (ACT/unpainted/dryfall rooms never move even
when a polygon exists); the coverage floor and sanity band abstain with a
counterfactual; a roster with zero painted ceilings abstains (dead-roster
rule); room data and the aggregate move by the same delta; flag off =
inert; idempotent; crash-safe.
"""
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


def rm(name, ceil_sf, painted=True, mat="GYP", in_scope=True, mult=1):
    return {"room_name": name, "in_scope": in_scope,
            "unit_multiplier": mult,
            "materials": {"ceiling": mat, "ceiling_painted": painted},
            "dimensions": {"ceiling_area_sqft": float(ceil_sf)}}


def shadow(rooms):
    return {"engine": "room-geometry-shadow-v1",
            "pages": [{"pdf": "plans.pdf", "page": 2,
                       "rooms": {n: {"area_sqft": a, "status": "measured"}
                                 for n, a in rooms.items()},
                       "rooms_measured": len(rooms)}]}


def analysis(rooms, shadow_rooms, agg_ceil=None):
    room_sum = sum(r["dimensions"]["ceiling_area_sqft"]
                   * r["unit_multiplier"] for r in rooms
                   if r["materials"]["ceiling_painted"]
                   and "dryfall" not in r["materials"]["ceiling"].lower()
                   and r["in_scope"])
    return {"floors": [{"floor_name": "1st Floor", "rooms": rooms}],
            "aggregated_totals": {"total_paintable_ceiling_sqft":
                                  agg_ceil if agg_ceil is not None
                                  else room_sum},
            "_room_geometry_shadow": shadow(shadow_rooms),
            "notes": []}


print("VME ceilings checks")
os.environ.pop("NIGHTSHIFT_VME_CEILINGS", None)

# flag off: inert
a = analysis([rm("Office", 500)], {"Office": 300})
a = T._apply_vme_ceilings(a)
check("_vme_ceilings" not in a, "flag off: gate fully inert")

os.environ["NIGHTSHIFT_VME_CEILINGS"] = "1"

# both directions: the LLM over-read shrinks (the +452% class)...
a = analysis([rm("Office", 500), rm("Lobby", 400)],
             {"Office": 300, "Lobby": 350})
a = T._apply_vme_ceilings(a)
v = a["_vme_ceilings"]
check(v["applied"] is True, f"healthy roster applies: {v}")
check(a["floors"][0]["rooms"][0]["dimensions"]["ceiling_area_sqft"] == 300.0,
      "over-read room SHRINKS to its polygon (only-increase asymmetry gone)")
check(a["aggregated_totals"]["total_paintable_ceiling_sqft"] == 650.0,
      f"aggregate moves by the same delta: "
      f"{a['aggregated_totals']['total_paintable_ceiling_sqft']}")
check(a["floors"][0]["rooms"][0]["dimensions"].get("ceiling_area_source")
      == "polygon", "room records its area source")

# ...and an under-read grows
a = analysis([rm("Office", 200)], {"Office": 300})
a = T._apply_vme_ceilings(a)
check(a["aggregated_totals"]["total_paintable_ceiling_sqft"] == 300.0,
      "under-read room grows to its polygon")

# classification respected: ACT and unpainted rooms never move
a = analysis([rm("Office", 500), rm("Corridor", 800, painted=False,
                                    mat="ACT")],
             {"Office": 450, "Corridor": 700})
a = T._apply_vme_ceilings(a)
check(a["floors"][0]["rooms"][1]["dimensions"]["ceiling_area_sqft"]
      == 800.0,
      "ACT room untouched even though a polygon exists")
check(a["aggregated_totals"]["total_paintable_ceiling_sqft"] == 450.0,
      "unpainted rooms contribute nothing to the geometric total")

# dryfall untouched
a = analysis([rm("Office", 500), rm("Warehouse", 5000, mat="Dryfall")],
             {"Office": 450, "Warehouse": 4000})
a = T._apply_vme_ceilings(a)
check(a["floors"][0]["rooms"][1]["dimensions"]["ceiling_area_sqft"]
      == 5000.0, "dryfall room untouched")

# unit multiplier respected
a = analysis([rm("Unit A", 400, mult=10)], {"Unit A": 350})
a = T._apply_vme_ceilings(a)
check(a["aggregated_totals"]["total_paintable_ceiling_sqft"] == 3500.0,
      f"multiplier x10 carries through: "
      f"{a['aggregated_totals']['total_paintable_ceiling_sqft']}")

# coverage floor: measured polygons must carry >=60% of painted area
a = analysis([rm("Office", 100), rm("BigHall", 900)], {"Office": 100})
a = T._apply_vme_ceilings(a)
v = a["_vme_ceilings"]
check(v["applied"] is False and "polygons carry" in str(v.get("reason", "")),
      f"10% coverage abstains: {v.get('reason')}")
check(a["floors"][0]["rooms"][0]["dimensions"]["ceiling_area_sqft"]
      == 100.0, "abstain leaves rooms untouched")
check(v.get("geometric_painted_sf") is not None,
      "abstain records the counterfactual")

# sanity band: 3x geometry abstains
a = analysis([rm("Office", 100)], {"Office": 300})
a = T._apply_vme_ceilings(a)
check(a["_vme_ceilings"]["applied"] is False and
      "sanity" in a["_vme_ceilings"]["reason"],
      "x3.00 outside (0.4, 2.5) abstains")

# dead roster: zero painted ceilings abstains
a = analysis([rm("Office", 500, painted=False, mat="ACT")],
             {"Office": 450})
a = T._apply_vme_ceilings(a)
check(a["_vme_ceilings"]["applied"] is False and
      "dead-roster" in a["_vme_ceilings"]["reason"],
      "zero painted ceilings abstains (dead-roster rule)")

# no shadow / no polygons
a = analysis([rm("Office", 500)], {})
a = T._apply_vme_ceilings(a)
check(a["_vme_ceilings"]["applied"] is False,
      "empty shadow abstains")
a = analysis([rm("Office", 500)], {"Office": 450})
del a["_room_geometry_shadow"]
a = T._apply_vme_ceilings(a)
check(a["_vme_ceilings"]["applied"] is False, "missing shadow abstains")

# idempotent
a = analysis([rm("Office", 500)], {"Office": 450})
a = T._apply_vme_ceilings(a)
first = a["aggregated_totals"]["total_paintable_ceiling_sqft"]
a = T._apply_vme_ceilings(a)
check(a["aggregated_totals"]["total_paintable_ceiling_sqft"] == first,
      "second call is a no-op (idempotent)")

# crash-safe
a = {"floors": "boom",
     "_room_geometry_shadow": shadow({"Office": 450}),
     "aggregated_totals": {}}
out = T._apply_vme_ceilings(a)
check(out is a and out["_vme_ceilings"]["applied"] is False,
      "internal crash records an error and returns the analysis")

os.environ.pop("NIGHTSHIFT_VME_CEILINGS", None)
print()
if fails:
    print(f"❌ {len(fails)} VME ceilings check(s) failed")
    sys.exit(1)
print("✅ all VME ceilings checks passed")
