"""Regression tests for the number-aware small-commercial floor dedup
(PNC Milwaukee, 2026-07-06).

Type-only dedup was built for UNNUMBERED floor redraws (Dutchess). On PNC's
numbered 54-room office floor split across sheets, a DIFFERENT office on a
secondary page was dropped for merely sharing a type with the authoritative
page — runs 2 and 3 each lost ~11k SF of real walls to this. Now: a numbered
room absent from the authoritative page is distinct (kept); a numbered room
present there is a certain redraw (dropped, even across types); unnumbered
rooms keep the original type behavior.

Offline, no API.
"""
import os

import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


def _room(name, num, page, walls, in_scope=True):
    return {"room_name": name, "room_number": num, "source_page": page,
            "in_scope": in_scope, "dimensions": {"wall_area_sqft": walls}}


def _analysis(rooms):
    return {
        "_per_sheet_extraction": True,
        "project_info": {"building_type": "commercial", "total_units": 0},
        "floors": [{"floor_number": "15", "rooms": rooms}],
    }


def _inscope_walls(analysis):
    return sum(r["dimensions"]["wall_area_sqft"]
               for fl in analysis["floors"] for r in fl["rooms"]
               if r.get("in_scope", True))


# ── PNC shape: numbered offices split across sheets ─────────────────────────
print("\nNumbered rooms: distinct numbers on secondary sheets are kept")
rooms = [
    # authoritative page p6 (A101): 4 rooms
    _room("Office", "1501", 6, 500), _room("Office", "1502", 6, 500),
    _room("Conference", "1504", 6, 600), _room("Corridor", "1520", 6, 800),
    # secondary page p3 (G101): 2 DIFFERENT offices + 1 true redraw
    _room("Office", "1510", 3, 450), _room("Office", "1511", 3, 450),
    _room("Office", "1501", 3, 480),  # same number as p6 -> redraw, drop
    # secondary page p7: differently-TYPED room with a number p6 already has
    _room("Storage", "1504", 7, 120),  # number match beats type mismatch -> drop
]
a = _analysis(rooms)
T._dedupe_small_commercial_floors(a)
walls = _inscope_walls(a)
kept_nums = sorted(r["room_number"] for fl in a["floors"] for r in fl["rooms"]
                   if r.get("in_scope", True))
check(walls == 500 + 500 + 600 + 800 + 450 + 450,
      f"distinct-numbered offices 1510/1511 kept (in-scope walls={walls})")
check("1510" in kept_nums and "1511" in kept_nums,
      "different offices on secondary sheet survive type-match")
dropped = [r for fl in a["floors"] for r in fl["rooms"]
           if not r.get("in_scope", True)]
check(len(dropped) == 2 and {_r["room_number"] for _r in dropped} == {"1501", "1504"},
      "same-number redraws dropped (1501 on p3, 1504 on p7)")

# ── Dutchess shape: unnumbered redraws still dedupe by type ─────────────────
print("\nUnnumbered rooms: original type dedup preserved")
rooms = [
    _room("Office", "", 6, 500), _room("Office", "", 6, 500),
    _room("Storage", "", 6, 200), _room("Corridor", "", 6, 300),
    # secondary page re-draws the same floor without numbers
    _room("Office", "", 3, 480), _room("Storage", "", 3, 190),
    _room("Kitchen", "", 3, 250),  # page-unique type -> kept
]
a = _analysis(rooms)
T._dedupe_small_commercial_floors(a)
walls = _inscope_walls(a)
check(walls == 500 + 500 + 200 + 300 + 250,
      f"unnumbered same-type dupes dropped, page-unique kitchen kept ({walls})")

# ── Kill switch reverts to type-only ────────────────────────────────────────
print("\nKill switch")
os.environ["NIGHTSHIFT_SC_DEDUP_NUMBER_AWARE"] = "0"
rooms = [
    _room("Office", "1501", 6, 500), _room("Office", "1502", 6, 500),
    _room("Conference", "1504", 6, 600),
    _room("Office", "1510", 3, 450),  # would be kept when number-aware
]
a = _analysis(rooms)
T._dedupe_small_commercial_floors(a)
os.environ.pop("NIGHTSHIFT_SC_DEDUP_NUMBER_AWARE", None)
check(_inscope_walls(a) == 500 + 500 + 600,
      "flag=0 restores legacy type-only behavior")

# ── Gates untouched ─────────────────────────────────────────────────────────
print("\nGates")
a = _analysis([_room("Office", "1501", 6, 500)])
a["project_info"]["total_units"] = 40  # multifamily -> not small commercial
T._dedupe_small_commercial_floors(a)
check(_inscope_walls(a) == 500, "multifamily gate still bypasses dedup")

a = _analysis([_room("Office", "1501", 6, 500),
               _room("Office", "1502", 3, 450)])
del a["_per_sheet_extraction"]
os.environ.pop("NIGHTSHIFT_SMALL_COMMERCIAL_FIX", None)
T._dedupe_small_commercial_floors(a)
check(_inscope_walls(a) == 950, "per-sheet/flag gate still required")


print("\n=== ALL PASS ===" if not fails else f"\n=== {len(fails)} FAIL ===")
import sys
sys.exit(1 if fails else 0)
