"""Regression test for NIGHTSHIFT_SMALL_COMMERCIAL_FIX.

Decouples the validated Dutchess phantom-floor dedup
(_dedupe_small_commercial_floors) from the full per-sheet path so it can ship
on its own. Guards:
  (1) Flag OFF and per-sheet OFF -> dedup is a no-op (identical to before).
  (2) Flag ON  and per-sheet OFF -> dedup runs for small-commercial buildings.
  (3) Per-sheet ON with flag OFF -> still runs (back-compat preserved).
  (4) Small-commercial detection still gates it (multifamily untouched).
Offline, no API.
"""
import os
import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def _sc():
    return {"project_info": {"building_type": "commercial", "total_units": 1},
            "floors": [{"floor_name": "First Floor", "rooms": [
                {"room_id": "", "room_name": "Womens Restroom", "in_scope": True,
                 "source_page": 5, "dimensions": {"wall_area_sqft": 200},
                 "materials": {"walls": "GYP"}, "elements": {}},
                {"room_id": "", "room_name": "Mens Restroom", "in_scope": True,
                 "source_page": 5, "dimensions": {"wall_area_sqft": 200},
                 "materials": {"walls": "GYP"}, "elements": {}},
                {"room_id": "", "room_name": "Storage", "in_scope": True,
                 "source_page": 9, "dimensions": {"wall_area_sqft": 150},
                 "materials": {"walls": "GYP"}, "elements": {}},
            ]}]}


def _multifamily():
    return {"project_info": {"building_type": "multifamily residential",
                             "total_units": 24},
            "floors": [{"floor_name": "1", "rooms": [
                {"room_id": "101", "room_name": "Unit 101 Living", "in_scope": True,
                 "source_page": 3, "dimensions": {"wall_area_sqft": 300},
                 "materials": {"walls": "GYP"}, "elements": {}},
            ]}]}


# 1) Flag OFF, per-sheet OFF -> no-op
os.environ["NIGHTSHIFT_SMALL_COMMERCIAL_FIX"] = "0"
a = _sc()
T._dedupe_small_commercial_floors(a)
check(a.get("_sc_floor_deduped") is None, "flag-off/per-sheet-off should skip")

# 2) Flag ON, per-sheet OFF -> runs
os.environ["NIGHTSHIFT_SMALL_COMMERCIAL_FIX"] = "1"
a = _sc()
T._dedupe_small_commercial_floors(a)
check(a.get("_sc_floor_deduped") is True, "flag-on should run dedup standalone")

# 3) Per-sheet ON, flag OFF -> still runs (back-compat)
os.environ["NIGHTSHIFT_SMALL_COMMERCIAL_FIX"] = "0"
a = _sc()
a["_per_sheet_extraction"] = True
T._dedupe_small_commercial_floors(a)
check(a.get("_sc_floor_deduped") is True, "per-sheet path must still run")

# 4) Multifamily untouched even with flag ON
os.environ["NIGHTSHIFT_SMALL_COMMERCIAL_FIX"] = "1"
check(T._is_small_commercial_building(_multifamily()) is False,
      "multifamily must never be classified small-commercial")
a = _multifamily()
T._dedupe_small_commercial_floors(a)
check(a.get("_sc_floor_deduped") is None,
      "multifamily must be skipped by small-commercial gate")

os.environ["NIGHTSHIFT_SMALL_COMMERCIAL_FIX"] = "0"
print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
