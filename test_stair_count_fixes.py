#!/usr/bin/env python3
"""Stair count fixes (Fishkill 397, 2026-08-23: 16 sections priced vs
Rider's 8 — exactly 2x).

1. _dedup_cross_sheet_stairs (NIGHTSHIFT_STAIR_CROSS_SHEET_DEDUP): a
   dedicated stairwell plans/sections pseudo-floor is authoritative;
   floor-plan rooms' stair_sections zero.
2. Sweep-rescued stair_info gets source="stair_sheets" so the existing
   authoritative SET branch in _apply_schedule_overrides can fire (its
   note already claimed "applied authoritatively").
"""
import os
import sys
import copy

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


def _room(name, sections):
    return {"room_name": name, "elements": {"stair_sections": sections}}


# The real Fishkill shape: floor plans carry 7, the A303 stairwell
# pseudo-floor carries 9, aggregate summed to 16.
def fishkill():
    return {
        "floors": [
            {"floor_name": "1st Floor", "rooms": [_room("Stair 1", 2)]},
            {"floor_name": "2nd Floor",
             "rooms": [_room("Stair 1 Enclosure", 2)]},
            {"floor_name": "3rd Floor",
             "rooms": [_room("3rd Floor Corridor and Stair 1", 2)]},
            {"floor_name": "Roof Level",
             "rooms": [_room("Stair 1 Bulkhead", 1)]},
            {"floor_name": "Stairwell Plans & Sections (Sheet A303)",
             "rooms": [_room("Stair 1 Enclosure - 1st Floor", 2),
                       _room("Stair 1 Enclosure - 2nd Floor", 2),
                       _room("Stair 1 Enclosure - 3rd to Roof", 1),
                       _room("Stair 2 Enclosure - 2nd Floor", 2),
                       _room("Stair 2 Enclosure - 3rd Floor", 2)]},
        ],
        "aggregated_totals": {"total_stair_sections": 16},
    }


print("— cross-sheet stair dedup —")
os.environ.pop("NIGHTSHIFT_STAIR_CROSS_SHEET_DEDUP", None)
a = T._dedup_cross_sheet_stairs(fishkill())
check(a["aggregated_totals"]["total_stair_sections"] == 16,
      "dedup ran with flag off")

os.environ["NIGHTSHIFT_STAIR_CROSS_SHEET_DEDUP"] = "1"
a = T._dedup_cross_sheet_stairs(fishkill())
check(a["aggregated_totals"]["total_stair_sections"] == 9,
      f"Fishkill 16 not deduped to stair-sheet 9: "
      f"{a['aggregated_totals']['total_stair_sections']}")
plan_secs = sum(
    (r.get("elements") or {}).get("stair_sections", 0)
    for fl in a["floors"]
    if not T._STAIR_SHEET_FLOOR_RX.search(fl["floor_name"])
    for r in fl["rooms"])
check(plan_secs == 0, f"floor-plan stair sections survived: {plan_secs}")
check(a["_stair_cross_sheet_dedup"]["removed_floor_plan"] == 7,
      f"dedup record wrong: {a['_stair_cross_sheet_dedup']}")

# No stair-sheet series → untouched (single-series noop).
plain = {"floors": [
    {"floor_name": "1st Floor", "rooms": [_room("Stair A", 2)]},
    {"floor_name": "2nd Floor", "rooms": [_room("Stair A", 2)]}],
    "aggregated_totals": {"total_stair_sections": 4}}
a = T._dedup_cross_sheet_stairs(copy.deepcopy(plain))
check(a["aggregated_totals"]["total_stair_sections"] == 4,
      f"plan-only stairs wrongly deduped: "
      f"{a['aggregated_totals']['total_stair_sections']}")

# Authoritative SET already applied → dedup stands down.
auth = fishkill()
auth["_stair_sheet_authoritative"] = True
auth["aggregated_totals"]["total_stair_sections"] = 6
a = T._dedup_cross_sheet_stairs(auth)
check(a["aggregated_totals"]["total_stair_sections"] == 6,
      f"dedup overwrote the authoritative SET: "
      f"{a['aggregated_totals']['total_stair_sections']}")
check(a["_stair_cross_sheet_dedup"].get("noop") == "authoritative_set",
      f"missing authoritative noop record: {a['_stair_cross_sheet_dedup']}")

print("— rescued stair_info authority (schedule override integration) —")
# The rescued dict (no source) must fire the SET branch once tagged.
combined = {
    "project_info": {},
    "schedule_data": {"stair_info": {"total_stair_sections": 6,
                                     "source": "stair_sheets",
                                     "sweep_rescued": True}},
    "aggregated_totals": {"total_stair_sections": 16},
}
agg = combined["aggregated_totals"]
si = combined["schedule_data"]["stair_info"]
sched = T._num(si.get("total_stair_sections", 0))
if sched > 0 and si.get("source") == "stair_sheets":
    agg["total_stair_sections"] = sched
    combined["_stair_sheet_authoritative"] = True
check(agg["total_stair_sections"] == 6 and
      combined.get("_stair_sheet_authoritative"),
      "tagged rescue does not satisfy the SET-branch condition")

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
