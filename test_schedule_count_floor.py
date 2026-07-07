"""Regression tests for the schedule room-count floor (PNC Milwaukee 2026-07-06).

The I601 finish schedule lists 54 numbered rooms; extraction emits identical
offices unnumbered, the canonical merge fuses them (name+geometry identity)
and the redraw dedup drops more — two runs shipped ~12k SF walls vs Scott's
verified 25.5k. _reconcile_schedule_room_count treats the schedule's numbered
room list as the inventory floor: restore dedup-dropped rooms the schedule
confirms, replicate median same-type dims for rooms extraction missed, and
0-sqft+RFI when no template exists.

Offline, no API.
"""
import os

import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


def _room(rid, name, walls, ceil=0, in_scope=True, reason=None,
          ceiling_painted=False):
    r = {"room_id": rid, "room_name": name, "in_scope": in_scope,
         "dimensions": {"wall_area_sqft": walls, "ceiling_area_sqft": ceil},
         "materials": {"ceiling_painted": ceiling_painted}}
    if reason:
        r["scope_exclusion_reason"] = reason
    return r


def _analysis(rooms, sched_nums_names):
    return {
        "floors": [{"floor_name": "Floor 15", "rooms": rooms}],
        "aggregated_totals": {
            "total_paintable_wall_sqft": sum(
                r["dimensions"]["wall_area_sqft"] for r in rooms
                if r.get("in_scope", True)),
            "total_paintable_ceiling_sqft": 0,
        },
        "project_info": {"total_rooms_found": len(rooms)},
        "room_finish_schedule": [
            {"room_number": n, "room_name": nm} for n, nm in sched_nums_names],
        "notes": [],
    }


SCHED = [("1501", "Reception"), ("1509", "Leader"), ("1510", "Leader"),
         ("1511", "Leader"), ("1512", "Leader"), ("1520", "Corridor"),
         ("1544", "Pantry")]

os.environ["NIGHTSHIFT_SCHEDULE_COUNT_FLOOR"] = "1"

# ── PNC shape: schedule rooms fused away get replicated ─────────────────────
print("\nSchedule rooms extraction missed are replicated from same-type median")
rooms = [
    _room("F15-1501", "Reception", 700),
    _room("F15-1509", "Leader", 480),   # the one surviving fused office
    _room("F15-1520", "Corridor", 900),
    _room("F15-1544", "Pantry", 300),
]
a = T._reconcile_schedule_room_count(_analysis(rooms, SCHED))
agg = a["aggregated_totals"]
rec = a["_schedule_count_floor"]
check(sorted(rec["added"]) == ["1510", "1511", "1512"],
      f"leaders 1510-1512 replicated (got {rec['added']})")
check(agg["total_paintable_wall_sqft"] == 2380 + 3 * 480,
      f"walls bumped by 3x480 ({agg['total_paintable_wall_sqft']})")
check(any(n.startswith("[Schedule Count Floor]") for n in a["notes"]),
      "audit note added")
replicas = [r for fl in a["floors"] for r in fl["rooms"]
            if r.get("_schedule_count_replica")]
check(len(replicas) == 3 and all(r["elements"] == {} for r in replicas),
      "replicas carry no doors/windows")
check(all(r["dimensions"].get("wallcovering_sqft", 0) == 0 for r in replicas),
      "replicas carry no wallcovering")
n_before = len(a["notes"])
a2 = T._reconcile_schedule_room_count(a)
check(len(a2["notes"]) == n_before, "idempotent on second call")

# ── Restore beats replicate when the dedup dropped the real room ────────────
print("\nDedup-dropped schedule-confirmed rooms are restored, not replicated")
rooms = [
    _room("F15-1501", "Reception", 700),
    _room("F15-1509", "Leader", 480),
    _room("F15-1510", "Leader", 520, in_scope=False,
          reason="small-commercial: same floor re-drawn on sheet p3; ..."),
    _room("F15-1511", "Leader", 470, in_scope=False,
          reason="small-commercial: same floor re-drawn on sheet p3; ..."),
    _room("F15-1512", "Leader", 480),
    _room("F15-1520", "Corridor", 900),
    _room("F15-1544", "Pantry", 300),
]
a = T._reconcile_schedule_room_count(_analysis(rooms, SCHED))
rec = a["_schedule_count_floor"]
check(sorted(rec["restored"]) == ["1510", "1511"],
      f"1510/1511 restored with their MEASURED dims (got {rec['restored']})")
check(rec["added"] == [], "nothing replicated when the real room exists")
check(a["aggregated_totals"]["total_paintable_wall_sqft"] == 2860 + 520 + 470,
      "restored rooms use their own measured walls")

# ── Respected exclusions stay excluded ──────────────────────────────────────
print("\nNon-dedup exclusions are respected")
rooms = [
    _room("F15-1501", "Reception", 700),
    _room("F15-1509", "Leader", 480),
    _room("F15-1510", "Leader", 520, in_scope=False,
          reason="Explicitly labeled NOT IN SCOPE on the plan"),
    _room("F15-1511", "Leader", 470),
    _room("F15-1512", "Leader", 480),
    _room("F15-1520", "Corridor", 900),
    _room("F15-1544", "Pantry", 300),
]
a = T._reconcile_schedule_room_count(_analysis(rooms, SCHED))
rec = a["_schedule_count_floor"]
check("1510" not in rec.get("restored", []) and "1510" not in rec.get("added", []),
      "NOT-IN-SCOPE room neither restored nor replicated")

# ── No template -> zero-dim + RFI ───────────────────────────────────────────
print("\nNo same-type template -> zero-dim + RFI")
rooms = [_room("F15-1501", "Reception", 700),
         _room("F15-1520", "Corridor", 900),
         _room("F15-1509", "Leader", 480),
         _room("F15-1510", "Leader", 500),
         _room("F15-1511", "Leader", 470),
         _room("F15-1512", "Leader", 480)]
sched = SCHED + [("1550", "Cryochamber")]
a = T._reconcile_schedule_room_count(_analysis(rooms, sched))
rec = a["_schedule_count_floor"]
check("1550" in rec["zero_dim"], "template-less room included at 0 sqft")
check(any(n.startswith("[RFI: Room Inventory]") for n in a["notes"]),
      "RFI emitted for zero-dim rooms")
# Pantry 1544 has no in-scope pantry template either -> also zero-dim
check("1544" in rec["zero_dim"], "no-pool schedule room -> zero-dim not guess")

# ── Gates ───────────────────────────────────────────────────────────────────
print("\nGates")
a_in = _analysis([_room("F15-1501", "Reception", 700)], SCHED[:3])
a = T._reconcile_schedule_room_count(a_in)
check("_schedule_count_floor" not in a or not a.get("_schedule_count_floor"),
      "schedule with <5 numbered rows -> inert")

os.environ["NIGHTSHIFT_SCHEDULE_COUNT_FLOOR"] = "0"
rooms = [_room("F15-1501", "Reception", 700)]
a = T._reconcile_schedule_room_count(_analysis(rooms, SCHED))
check(a["aggregated_totals"]["total_paintable_wall_sqft"] == 700,
      "flag off -> untouched")
os.environ.pop("NIGHTSHIFT_SCHEDULE_COUNT_FLOOR", None)


print("\n=== ALL PASS ===" if not fails else f"\n=== {len(fails)} FAIL ===")
import sys
sys.exit(1 if fails else 0)
