#!/usr/bin/env python3
"""Roster-fit guards on the schedule scope boundary (round-5 recipe).

Locks in the five pre-mutation guards on _apply_schedule_room_scope:
G1 matched-row coverage ≥50%, G2 MAX_DROP hard refusal (the formerly
dead NIGHTSHIFT_SCHEDULE_SCOPE_MAX_DROP flag now rules), G3 template
units (unit_multiplier>1) never dropped + numeric-only matching in
multiplied buildings, G4 >50% null room numbers stands the roster down,
G5 row floor (schedule rows ≥ half the in-scope rooms). Every stand-down
happens before any room mutates, is recorded, noted, and RFI'd. Kill
switch NIGHTSHIFT_ROSTER_FIT_GUARDS=0 restores the old behavior.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["NIGHTSHIFT_SCHEDULE_ROOM_SCOPE"] = "1"
os.environ["NIGHTSHIFT_ROSTER_FIT_GUARDS"] = "1"
os.environ.pop("NIGHTSHIFT_SCHEDULE_SCOPE_MAX_DROP", None)

import Takeoff_DIRECT as T  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def room(num, name, mult=1, doors=0):
    r = {"room_id": f"R{num or name}", "room_name": name,
         "room_number": num, "in_scope": True,
         "dimensions": {"wall_area_sqft": 400, "ceiling_area_sqft": 150,
                        "ceiling_height_feet": 9},
         "elements": {"doors_full_paint": doors}}
    if mult > 1:
        r["unit_multiplier"] = mult
    return r


def sched_rows(nums):
    return [{"room_number": n, "room_name": f"Room {n}",
             "wall_finish": "PT-1", "ceiling_finish": "GWB"}
            for n in nums]


def analysis(rooms, schedule, doors_total=0):
    return {"room_finish_schedule": schedule,
            "floors": [{"rooms": rooms}],
            "aggregated_totals": {"total_doors_full_paint": doors_total},
            "notes": []}


def scope_rec(a):
    return a.get("_schedule_room_scope") or {}


print("roster-fit guard checks")

# ── G4: null room numbers break the join → stand down ────────────────────
rooms = [room(None, f"Space {i}") for i in range(6)] + \
        [room(f"10{i}", f"Office {i}") for i in range(4)]
a = analysis(rooms, sched_rows([f"10{i}" for i in range(8)]))
out = T._apply_schedule_room_scope(a)
check(scope_rec(out).get("noop") == "null_room_numbers",
      "G4: 6/10 rooms without numbers stands the roster down")
check(all(r.get("in_scope") for r in out["floors"][0]["rooms"]),
      "G4: no room was mutated on the stand-down path")

# ── G5: schedule fragment (rows < half the rooms) → stand down ───────────
rooms = [room(f"{100 + i}", f"Office {i}") for i in range(20)]
a = analysis(rooms, sched_rows([f"{100 + i}" for i in range(6)]))
out = T._apply_schedule_room_scope(a)
check(scope_rec(out).get("noop") == "schedule_row_floor",
      "G5: 6-row schedule against 20 rooms is a fragment — stood down")

# ── G1: schedule rows that match nothing → stand down ────────────────────
rooms = [room(f"{100 + i}", f"Office {i}") for i in range(10)]
a = analysis(rooms, sched_rows([f"{900 + i}" for i in range(12)]))
out = T._apply_schedule_room_scope(a)
check(scope_rec(out).get("noop") == "match_coverage",
      "G1: zero matched rows — the join does not exist, stood down")

# ── G2: the dead flag now rules — planned drop beyond 35% refuses ───────
rooms = [room(f"{100 + i}", f"Office {i}") for i in range(6)] + \
        [room(f"{800 + i}", f"Extra {i}") for i in range(8)]
a = analysis(rooms, sched_rows(
    [f"{100 + i}" for i in range(6)] + [f"{700 + i}" for i in range(6)]))
out = T._apply_schedule_room_scope(a)
check(scope_rec(out).get("noop") == "max_drop",
      "G2: 8/14 rooms (57%) would drop — beyond 0.35, hard refusal")
check(all(r.get("in_scope") for r in out["floors"][0]["rooms"]),
      "G2: refusal happens before mutation — every room still in scope")
check(any("roster-fit guard" in str(n) for n in out.get("notes", [])),
      "G2: the stand-down is loud (note present)")

os.environ["NIGHTSHIFT_SCHEDULE_SCOPE_MAX_DROP"] = "0.7"
a = analysis([room(f"{100 + i}", f"Office {i}") for i in range(6)] +
             [room(f"{800 + i}", f"Extra {i}") for i in range(8)],
             sched_rows([f"{100 + i}" for i in range(6)] +
                        [f"{700 + i}" for i in range(6)]))
out = T._apply_schedule_room_scope(a)
check(scope_rec(out).get("applied") is True
      and scope_rec(out).get("rooms_dropped") == 8,
      "G2: raising MAX_DROP to 0.7 lets the same boundary apply")
os.environ.pop("NIGHTSHIFT_SCHEDULE_SCOPE_MAX_DROP", None)

# ── Happy path: a fitting roster still bounds scope ──────────────────────
rooms = [room(f"{100 + i}", f"Office {i}", doors=2) for i in range(10)] + \
        [room("980", "Mech Penthouse", doors=2), room("981", "Roof Access")]
a = analysis(rooms, sched_rows([f"{100 + i}" for i in range(10)]),
             doors_total=24)
out = T._apply_schedule_room_scope(a)
rec = scope_rec(out)
check(rec.get("applied") is True and rec.get("rooms_dropped") == 2,
      "fit roster: 2 unscheduled rooms out, 10 kept — boundary applies")
check(out["aggregated_totals"]["total_doors_full_paint"] == 22.0,
      "fit roster: dropped room's doors decremented from aggregates")

# ── G3: template units are never dropped ─────────────────────────────────
rooms = [room(f"{100 + i}", f"Unit {i}") for i in range(8)] + \
        [room("501", "Typical Suite", mult=12, doors=3)]
a = analysis(rooms, sched_rows([f"{100 + i}" for i in range(8)]),
             doors_total=36)
out = T._apply_schedule_room_scope(a)
rec = scope_rec(out)
tpl = next(r for r in out["floors"][0]["rooms"]
           if r.get("room_number") == "501")
check(tpl.get("in_scope") is True,
      "G3: unmatched unit_multiplier=12 room stays in scope")
check(rec.get("rooms_protected_template") == 1,
      "G3: protection is recorded")
check(out["aggregated_totals"]["total_doors_full_paint"] == 36,
      "G3: protected template's 36 doors still priced")
check(any("template" in str(n).lower() for n in out.get("notes", [])),
      "G3: protection is loud (note present)")

# ── G3b: multiplied building demands numeric identity ───────────────────
# A name-only match in a unit-multiplied building must not count: five
# distinct template units can all 'best match' one generic row.
rooms = [room(f"{100 + i}", f"Unit {i}") for i in range(8)] + \
        [room(None, "Corridor Suite")]          # name-only candidate
a = analysis(rooms, sched_rows([f"{100 + i}" for i in range(8)]) +
             [{"room_number": None, "room_name": "Corridor Suite",
               "wall_finish": "PT-1"}])
a["floors"][0]["rooms"][0]["unit_multiplier"] = 4   # makes building multiplied
out = T._apply_schedule_room_scope(a)
corr = next(r for r in out["floors"][0]["rooms"]
            if r.get("room_name") == "Corridor Suite")
check(corr.get("in_scope") is False,
      "G3b: name-only match doesn't hold scope in a multiplied building")

# ── Kill switch restores old behavior ────────────────────────────────────
os.environ["NIGHTSHIFT_ROSTER_FIT_GUARDS"] = "0"
rooms = [room(f"{100 + i}", f"Office {i}") for i in range(6)] + \
        [room(f"{800 + i}", f"Extra {i}") for i in range(8)]
a = analysis(rooms, sched_rows([f"{100 + i}" for i in range(6)] +
                               [f"{700 + i}" for i in range(6)]))
out = T._apply_schedule_room_scope(a)
check(scope_rec(out).get("applied") is True,
      "kill switch: guards off reproduces the old (unguarded) drop")
os.environ["NIGHTSHIFT_ROSTER_FIT_GUARDS"] = "1"

print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all roster-fit guard checks passed")
