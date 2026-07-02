"""Tests for the 4d fix: cross-sheet dedup on blank building_type.

June-10 review finding 4d: _dedupe_cross_sheet_rooms bailed out entirely when
building_type was blank/unknown, leaving floor-plan + RCP duplicates fully
double-counted on exactly the jobs where extraction context was weakest.

Fix under test: blank type now infers the dedup mode from the analysis's own
unit signals — any multi-unit signal (total_units > 1, room multiplier > 1,
or a unit token in a room id/name) selects the conservative residential
unit-identity rules; no signals selects single-tenant name-identity rules.
Kill switch NIGHTSHIFT_BLANK_TYPE_DEDUP (default ON) restores the old bail.

Offline, no API.
"""
import copy
import os
import sys

import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


def room(name, page, walls=0, ceiling=0, painted=False, mult=1, rid=None):
    return {
        "room_id": rid or name,
        "room_name": name,
        "source_page": page,
        "in_scope": True,
        "unit_multiplier": mult,
        "dimensions": {"wall_area_sqft": walls, "perimeter_lf": walls / 9.0,
                       "ceiling_area_sqft": ceiling, "floor_area_sqft": 0},
        "materials": {"walls": "GYP", "ceiling": "GYP",
                      "ceiling_painted": painted},
        "elements": {"base_trim_lf": 0},
        "notes": "",
    }


def blank_type_analysis():
    """Blank building_type; 'Sales Area' measured on the floor plan (walls,
    no ceiling) AND on the RCP (ceiling, thinner walls) — a true cross-sheet
    duplicate. 'Office' appears once."""
    return {
        "project_info": {"building_type": "", "total_units": 1},
        "floors": [{
            "floor_name": "First Floor",
            "rooms": [
                room("Sales Area", 5, walls=1000, ceiling=0, painted=False),
                room("Sales Area", 9, walls=950, ceiling=400, painted=True),
                room("Office", 5, walls=300, ceiling=120, painted=True),
            ],
        }],
    }


def walls_total(a):
    return sum(
        r["dimensions"]["wall_area_sqft"]
        for fl in a["floors"] for r in fl["rooms"])


def ceil_total(a):
    return sum(
        r["dimensions"]["ceiling_area_sqft"]
        for fl in a["floors"] for r in fl["rooms"]
        if r["materials"].get("ceiling_painted"))


# ---------------------------------------------------------------------------
print("\n1) Blank type, no unit signals -> single-tenant name dedup runs")
os.environ.pop("NIGHTSHIFT_BLANK_TYPE_DEDUP", None)  # default ON
a = blank_type_analysis()
T._dedupe_cross_sheet_rooms(a)
check(a.get("_cross_sheet_rooms_deduped") is True, "dedup ran (flag set)")
check(walls_total(a) == 1300,
      f"duplicate walls collapsed (1000 kept + 300 office): got {walls_total(a)}")
check(ceil_total(a) == 520,
      "RCP ceiling backfilled onto the keeper + office kept: got "
      f"{ceil_total(a)}")
keeper = a["floors"][0]["rooms"][0]
check(keeper["materials"]["ceiling_painted"] is True
      and keeper["dimensions"]["ceiling_area_sqft"] == 400,
      "keeper carries the symmetric-vote painted ceiling")

print("\n1b) Kill switch OFF reproduces the legacy bail")
os.environ["NIGHTSHIFT_BLANK_TYPE_DEDUP"] = "0"
b = blank_type_analysis()
T._dedupe_cross_sheet_rooms(b)
check(walls_total(b) == 2250,
      f"flag OFF: duplicates left double-counted (legacy): got {walls_total(b)}")
os.environ.pop("NIGHTSHIFT_BLANK_TYPE_DEDUP", None)

# ---------------------------------------------------------------------------
print("\n2) Blank type WITH unit signals -> conservative unit-identity rules")
c = {
    "project_info": {"building_type": "", "total_units": 1},
    "floors": [{
        "floor_name": "Second Floor",
        "rooms": [
            # Same unit + same room across two sheets: merges.
            room("Unit 201 Bedroom", 4, walls=800, ceiling=0, rid="UNIT-201"),
            room("Unit 201 Bedroom", 7, walls=780, ceiling=160, painted=True,
                 rid="UNIT-201"),
            # Token-less duplicated name: unit mode must NOT merge it.
            room("Corridor", 4, walls=500, ceiling=200, painted=True),
            room("Corridor", 7, walls=500, ceiling=200, painted=True),
        ],
    }],
}
T._dedupe_cross_sheet_rooms(c)
check(walls_total(c) == 800 + 500 + 500,
      f"unit room merged, token-less rooms untouched: got {walls_total(c)}")

# ---------------------------------------------------------------------------
print("\n3) Known building types behave exactly as before")
d = {
    "project_info": {"building_type": "commercial", "total_units": 4},
    "floors": [{"floor_name": "First Floor", "rooms": [
        room("Sales Area", 5, walls=1000),
        room("Sales Area", 9, walls=950, ceiling=400, painted=True),
    ]}],
}
T._dedupe_cross_sheet_rooms(d)
check(walls_total(d) == 1950,
      "commercial multi-unit hard guard still bails (no merge)")

e = {
    "project_info": {"building_type": "residential", "total_units": 8},
    "floors": [{"floor_name": "First Floor", "rooms": [
        room("Unit 101 Bath", 3, walls=200, rid="UNIT-101"),
        room("Unit 101 Bath", 6, walls=190, ceiling=50, painted=True,
             rid="UNIT-101"),
    ]}],
}
T._dedupe_cross_sheet_rooms(e)
check(walls_total(e) == 200, "residential unit dedup unchanged")

# ---------------------------------------------------------------------------
print("\n4) Idempotent: second call is a no-op")
f = blank_type_analysis()
T._dedupe_cross_sheet_rooms(f)
snap = copy.deepcopy(f["floors"])
T._dedupe_cross_sheet_rooms(f)
check(f["floors"] == snap, "no further mutation on second call")

# ---------------------------------------------------------------------------
print()
if fails:
    print(f"❌ {len(fails)} FAILURE(S):")
    for m in fails:
        print(f"   - {m}")
    sys.exit(1)
print("✅ all blank-building_type dedup tests passed")
