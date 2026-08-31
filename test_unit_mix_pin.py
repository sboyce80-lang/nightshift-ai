#!/usr/bin/env python3
"""Unit-mix pin (NIGHTSHIFT_UNIT_MIX_PIN, sub-flag of UNIT_MIX_GATE).

Hudson K=3 round 1 (2026-08-26): 'Unit A' ×34 (the hotel's whole key
count) priced alongside separately-drawn C/D/E/F/G units — ~47 covered
units in a 34-key building, and each extraction draw resolved the
double-count differently (rooms 64/130/78, walls 89k/179k/40k). The pin
makes Σ(unit-type multipliers) equal the declared unit count by
reducing the catch-all typicals, largest first; aggregates shrink by
exactly the removed contributions. Removes double-count only — never
scales UP (under-coverage keeps the F3 detect+RFI behavior)."""
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


def _clear():
    for k in ("NIGHTSHIFT_UNIT_MIX_GATE", "NIGHTSHIFT_UNIT_MIX_PIN"):
        os.environ.pop(k, None)


def hudson():
    # 34-unit hotel: Unit A typical x34 (2 rooms) + drawn units C(x1),
    # D/F/G (x6), E(x1) + common areas -> covered 42, excess 8
    return {
        "project_info": {"total_units": 34},
        "floors": [
            {"floor_name": "Typical Unit A", "rooms": [
                {"room_name": "Unit A Guest Room", "unit_type": "Unit A",
                 "unit_multiplier": 34, "in_scope": True,
                 "dimensions": {"wall_area_sqft": 400.0,
                                "ceiling_area_sqft": 250.0},
                 "elements": {"doors_full_paint": 2}},
                {"room_name": "Unit A Bath", "unit_type": "Unit A",
                 "unit_multiplier": 34, "in_scope": True,
                 "dimensions": {"wall_area_sqft": 150.0},
                 "elements": {}},
            ]},
            {"floor_name": "Unit C Enlarged", "rooms": [
                {"room_name": "Unit C Living", "unit_type": "Unit C",
                 "unit_multiplier": 1, "in_scope": True,
                 "dimensions": {"wall_area_sqft": 500.0}, "elements": {}},
            ]},
            {"floor_name": "Units D/F/G", "rooms": [
                {"room_name": "Unit D/F/G Suite", "unit_type": "Unit D/F/G",
                 "unit_multiplier": 6, "in_scope": True,
                 "dimensions": {"wall_area_sqft": 450.0}, "elements": {}},
            ]},
            {"floor_name": "Unit E", "rooms": [
                {"room_name": "Unit E Suite", "unit_type": "Unit E",
                 "unit_multiplier": 1, "in_scope": True,
                 "dimensions": {"wall_area_sqft": 480.0}, "elements": {}},
            ]},
            {"floor_name": "1st Floor", "rooms": [
                {"room_name": "Lobby", "unit_type": "",
                 "unit_multiplier": 1, "in_scope": True,
                 "dimensions": {"wall_area_sqft": 900.0}, "elements": {}},
            ]},
        ],
        # walls: A (400+150)*34 + C 500 + DFG 450*6 + E 480 + lobby 900
        "aggregated_totals": {
            "total_paintable_wall_sqft": 550.0 * 34 + 500 + 2700 + 480 + 900,
            "total_paintable_ceiling_sqft": 250.0 * 34,
            "total_doors_full_paint": 2 * 34,
        },
        "notes": [],
    }


print("1) Gate on, pin off: detect-only (legacy F3)")
_clear()
os.environ["NIGHTSHIFT_UNIT_MIX_GATE"] = "1"
a = T._enforce_unit_mix_coverage(hudson())
rec = a["_unit_mix_gate"]
check("pinned" not in rec, f"pin must not run without its flag: {rec}")
check(a["aggregated_totals"]["total_paintable_wall_sqft"]
      == 550.0 * 34 + 4580, "aggregates untouched without pin")

print("\n2) Pin on: coverage 42 -> 34, catch-all absorbs the excess")
_clear()
os.environ["NIGHTSHIFT_UNIT_MIX_GATE"] = "1"
os.environ["NIGHTSHIFT_UNIT_MIX_PIN"] = "1"
a = T._enforce_unit_mix_coverage(hudson())
rec = a["_unit_mix_gate"]
check(rec.get("pinned", {}).get("covered_before") == 42
      and rec["pinned"]["covered_after"] == 34,
      f"coverage not pinned: {rec}")
# PROPORTIONAL: pool is Unit A x34 + D/F/G x6 = 40; excess 8 splits
# 34/40 -> 6 (A) and 6/40 -> 1 (D/F/G), remainder 1 to the largest
# headroom (A) => A 34->27, D/F/G 6->5. The catch-all is NOT emptied.
check(rec["unit_types"].get("Unit A") == 27,
      f"Unit A takes its proportional share, not all: {rec['unit_types']}")
check(rec["unit_types"].get("Unit D/F/G") == 5,
      f"drawn-unit typical shares the reduction: {rec['unit_types']}")
check(sum(rec["unit_types"].values()) == 34,
      f"coverage must sum to the unit count: {rec['unit_types']}")
rooms = a["floors"][0]["rooms"]
check(all(r["unit_multiplier"] == 27 for r in rooms),
      f"typical rooms not adjusted: "
      f"{[r['unit_multiplier'] for r in rooms]}")
agg = a["aggregated_totals"]
check(agg["total_paintable_wall_sqft"] == 550.0 * 27 + 500 + 450 * 5 + 480 + 900,
      f"wall agg not reduced by the removed multipliers: {agg}")
check(agg["total_paintable_ceiling_sqft"] == 250.0 * 27,
      f"ceiling agg wrong: {agg}")
check(agg["total_doors_full_paint"] == 2 * 27,
      f"door agg wrong: {agg}")
check(a["floors"][1]["rooms"][0]["unit_multiplier"] == 1,
      "singleton drawn units keep their multiplier")

print("\n2b) Hudson shape: catch-alls keep the bulk of their units")
_clear()
os.environ["NIGHTSHIFT_UNIT_MIX_GATE"] = "1"
os.environ["NIGHTSHIFT_UNIT_MIX_PIN"] = "1"
hud = {
    "project_info": {"total_units": 34},
    "floors": [{"floor_name": "F", "rooms": [
        {"room_name": "Unit A Guest", "unit_type": "Unit A",
         "unit_multiplier": 34, "in_scope": True,
         "dimensions": {"wall_area_sqft": 400.0}, "elements": {}},
        {"room_name": "Unit B Guest", "unit_type": "Unit B",
         "unit_multiplier": 17, "in_scope": True,
         "dimensions": {"wall_area_sqft": 380.0}, "elements": {}},
        {"room_name": "Unit C", "unit_type": "Unit C",
         "unit_multiplier": 32, "in_scope": True,
         "dimensions": {"wall_area_sqft": 360.0}, "elements": {}},
    ]}],
    "aggregated_totals": {"total_paintable_wall_sqft":
                          400 * 34 + 380 * 17 + 360 * 32},
    "notes": [],
}
a = T._enforce_unit_mix_coverage(hud)
ut = a["_unit_mix_gate"]["unit_types"]
check(sum(ut.values()) == 34, f"pinned to 34: {ut}")
check(min(ut.values()) > 1,
      f"NO typical may be emptied to 1 (the r2 Hudson failure): {ut}")
check(ut["Unit A"] > ut["Unit B"] and ut["Unit C"] > ut["Unit B"],
      f"relative sizes preserved: {ut}")
check(any("Unit-Mix Pin" in str(n) for n in a["notes"]),
      "pin note missing")

print("\n3) Under-coverage: pin never scales up")
_clear()
os.environ["NIGHTSHIFT_UNIT_MIX_GATE"] = "1"
os.environ["NIGHTSHIFT_UNIT_MIX_PIN"] = "1"
under = hudson()
for f in under["floors"]:
    for r in f["rooms"]:
        if r["unit_type"] == "Unit A":
            r["unit_multiplier"] = 3
a = T._enforce_unit_mix_coverage(under)
check("pinned" not in a["_unit_mix_gate"],
      f"under-coverage must not pin: {a['_unit_mix_gate']}")
check(a["_unit_mix_gate"]["flagged"] is True,
      "under-coverage keeps the F3 flag+RFI")

print("\n4) Schedule-authoritative aggregate keys are protected")
_clear()
os.environ["NIGHTSHIFT_UNIT_MIX_GATE"] = "1"
os.environ["NIGHTSHIFT_UNIT_MIX_PIN"] = "1"
prot = hudson()
prot["_schedule_authoritative_counts"] = {"total_doors_full_paint": 68}
a = T._enforce_unit_mix_coverage(prot)
check(a["aggregated_totals"]["total_doors_full_paint"] == 68,
      f"authoritative door count must not shrink: "
      f"{a['aggregated_totals']}")

_clear()
print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ all unit-mix pin checks passed")
