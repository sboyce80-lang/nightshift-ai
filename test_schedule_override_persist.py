"""Regression tests for the schedule-override revert (Purdy Ave, 2026-07-01).

11-15 Purdy Ave (mixed-use, 8 units) had its authoritative door schedule
override — 136 -> 14 full-paint, 12 -> 6 HM — silently reverted before pricing.
Root cause: _apply_schedule_overrides writes schedule counts into
aggregated_totals ONLY, and _backfill_missing_wall_heights (cross-sheet height
back-fill, flag ON in prod) calls _recalculate_totals afterwards, which re-sums
door counts from room data and restores the room-level 136/12. The quantity
ledger recorded the revert under the "wall_boost" stage snapshot. Impact:
~122 phantom full-paint doors (~$18k) and the pipeline's own door-count
warning fired.

Fix under test: _apply_schedule_overrides stashes its final authoritative
counts in analysis["_schedule_authoritative_counts"]; _recalculate_totals
re-asserts them after every re-sum (NIGHTSHIFT_SCHEDULE_OVERRIDE_PERSIST,
default ON). Downstream stages that legitimately change those counts
(_reconcile_door_schedule_scope, _apply_commercial_window_exclusion) update
the stash so their corrections persist too.

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


def make_analysis():
    """Purdy-shaped analysis: room-level extraction over-counts doors,
    door schedule is authoritative and much lower."""
    return {
        "project_info": {
            "building_type": "mixed-use",
            "total_units": 8,
            "total_stories": 8,
            "footprint_sqft": 818,
        },
        "floors": [
            {
                "floor_name": "First Floor",
                "rooms": [
                    {
                        "room_name": "Lobby",
                        "in_scope": True,
                        "unit_multiplier": 1,
                        "dimensions": {
                            "wall_area_sqft": 2000, "ceiling_area_sqft": 500,
                            "perimeter_lf": 200, "ceiling_height_feet": 10,
                            "floor_area_sqft": 500,
                        },
                        "materials": {"walls": "GYP", "ceiling": "GYP",
                                      "ceiling_painted": True},
                        "elements": {"doors_full_paint": 136,
                                     "doors_hm_panel": 12,
                                     "base_trim_lf": 200},
                    },
                ],
            }
        ],
        "aggregated_totals": {
            "total_doors_full_paint": 136,
            "total_doors_hm_panel": 12,
            "total_doors_frame_only": 0,
            "total_windows_painted_interior": 0,
            "total_windows_all": 52,
            "total_stair_sections": 4,
        },
        "schedule_data": {
            "door_schedule": {
                "total_doors_full_paint": 14,
                "total_doors_hm_panel": 6,
                "total_doors_frame_only": 0,
            },
        },
        "notes": [],
    }


# ---------------------------------------------------------------------------
print("\n1) Purdy revert regression: schedule doors survive _recalculate_totals")
os.environ.pop("NIGHTSHIFT_SCHEDULE_OVERRIDE_PERSIST", None)  # default ON
a = make_analysis()
a = T._apply_schedule_overrides(a)
agg = a["aggregated_totals"]
check(agg["total_doors_full_paint"] == 14 and agg["total_doors_hm_panel"] == 6,
      "schedule override applied (136/12 -> 14/6): got "
      f"{agg['total_doors_full_paint']}/{agg['total_doors_hm_panel']}")
check(a.get("_schedule_authoritative_counts", {}).get("total_doors_full_paint") == 14,
      "authoritative stash written")
T._recalculate_totals(a)
agg = a["aggregated_totals"]
check(agg["total_doors_full_paint"] == 14 and agg["total_doors_hm_panel"] == 6,
      "doors NOT reverted by recalc (the Purdy bug): got "
      f"{agg['total_doors_full_paint']}/{agg['total_doors_hm_panel']}")

print("\n1b) Idempotent: a second recalc changes nothing")
snap = dict(a["aggregated_totals"])
T._recalculate_totals(a)
check(a["aggregated_totals"]["total_doors_full_paint"] == snap["total_doors_full_paint"]
      and a["aggregated_totals"]["total_doors_hm_panel"] == snap["total_doors_hm_panel"],
      "second recalc idempotent for door counts")

# ---------------------------------------------------------------------------
print("\n2) Kill switch: flag OFF reproduces legacy revert behavior")
os.environ["NIGHTSHIFT_SCHEDULE_OVERRIDE_PERSIST"] = "0"
b = make_analysis()
b = T._apply_schedule_overrides(b)
T._recalculate_totals(b)
agg = b["aggregated_totals"]
check(agg["total_doors_full_paint"] == 136 and agg["total_doors_hm_panel"] == 12,
      "flag OFF: recalc re-sums room doors (legacy): got "
      f"{agg['total_doors_full_paint']}/{agg['total_doors_hm_panel']}")
os.environ.pop("NIGHTSHIFT_SCHEDULE_OVERRIDE_PERSIST", None)

# ---------------------------------------------------------------------------
print("\n3) Stair upward override persists through recalc")
c = make_analysis()
c["schedule_data"]["stair_info"] = {"total_stair_sections": 6}
c = T._apply_schedule_overrides(c)
check(c["aggregated_totals"]["total_stair_sections"] == 6,
      "stairs overridden upward 4 -> 6")
T._recalculate_totals(c)
check(c["aggregated_totals"]["total_stair_sections"] == 6,
      "stair override survives recalc (rooms sum to 0): got "
      f"{c['aggregated_totals']['total_stair_sections']}")

# ---------------------------------------------------------------------------
print("\n4) Commercial window exclusion is NOT resurrected by recalc")
d = make_analysis()
d["project_info"]["building_type"] = "commercial"
d["project_info"]["total_units"] = 1
d["has_window_schedule"] = True
d["schedule_data"]["window_schedule"] = {
    "windows_painted_interior": 10, "total_windows": 20}
d = T._apply_schedule_overrides(d)
check(d["aggregated_totals"]["total_windows_painted_interior"] == 10,
      "window schedule override applied (10 painted)")
d = T._apply_commercial_window_exclusion(d)
check(d["aggregated_totals"]["total_windows_painted_interior"] == 0,
      "commercial exclusion zeroed painted sashes")
T._recalculate_totals(d)
check(d["aggregated_totals"]["total_windows_painted_interior"] == 0,
      "exclusion survives recalc (stash refreshed to 0): got "
      f"{d['aggregated_totals']['total_windows_painted_interior']}")

# ---------------------------------------------------------------------------
print("\n5) Door reconciliation's reclassified values persist through recalc")
os.environ["NIGHTSHIFT_DOOR_SCHEDULE_FIX"] = "1"
e = {
    "project_info": {"building_type": "commercial/industrial",
                     "_user_scope_notes":
                     "New hollow metal doors and frames to be painted"},
    "floors": [
        {"floor_name": "First Floor",
         "rooms": [{"room_name": "Shop", "in_scope": True,
                    "unit_multiplier": 1,
                    "dimensions": {"wall_area_sqft": 1000,
                                   "ceiling_area_sqft": 400,
                                   "perimeter_lf": 100,
                                   "ceiling_height_feet": 10,
                                   "floor_area_sqft": 400},
                    "materials": {"walls": "GYP", "ceiling": "GYP",
                                  "ceiling_painted": True},
                    "elements": {"doors_hm_panel": 9}}]}
    ],
    "aggregated_totals": {"total_doors_full_paint": 0,
                          "total_doors_hm_panel": 9,
                          "total_doors_frame_only": 0},
    "schedule_data": {"door_schedule": {"total_doors_full_paint": 0,
                                        "total_doors_hm_panel": 3,
                                        "total_doors_frame_only": 0,
                                        "notes": ""}},
    "notes": [],
}
e = T._apply_schedule_overrides(e)
check(e["aggregated_totals"]["total_doors_hm_panel"] == 3,
      "schedule override applied (9 -> 3 hm)")
e = T._reconcile_door_schedule_scope(e)
check(e["aggregated_totals"]["total_doors_full_paint"] == 3
      and e["aggregated_totals"]["total_doors_hm_panel"] == 0,
      "frames-painted scope reclassified 3 hm -> 3 full_paint")
T._recalculate_totals(e)
check(e["aggregated_totals"]["total_doors_full_paint"] == 3
      and e["aggregated_totals"]["total_doors_hm_panel"] == 0,
      "reclassified values survive recalc (stash refreshed): got "
      f"{e['aggregated_totals']['total_doors_full_paint']}/"
      f"{e['aggregated_totals']['total_doors_hm_panel']}")
os.environ.pop("NIGHTSHIFT_DOOR_SCHEDULE_FIX", None)

# ---------------------------------------------------------------------------
print("\n6) No schedule -> recalc re-sums from rooms normally (no stash)")
f = make_analysis()
del f["schedule_data"]
f.pop("_schedule_authoritative_counts", None)
T._recalculate_totals(f)
check(f["aggregated_totals"]["total_doors_full_paint"] == 136,
      "no schedule: room-summed doors kept (136): got "
      f"{f['aggregated_totals']['total_doors_full_paint']}")
check("_schedule_authoritative_counts" not in f, "no stash written")

# ---------------------------------------------------------------------------
print()
if fails:
    print(f"❌ {len(fails)} FAILURE(S):")
    for m in fails:
        print(f"   - {m}")
    sys.exit(1)
print("✅ all schedule-override persistence tests passed")
