#!/usr/bin/env python3
"""CI determinism gate: the pricing chain must replay bit-identically.

Phase 2 exit criterion, made CI-enforceable without customer rosters:
a synthetic but structurally rich analysis (multi-floor, unit
multipliers, finish schedule, elements, ledgered adjustments) runs
through build_priced_takeoff twice — at the default posture and again
with the Phase 1 flag set ON — and the resulting aggregated_totals must
be byte-identical between runs. Any diff is a nondeterminism bug in the
gate chain (set iteration, id()-keyed maps leaking into decisions,
dict-order dependence). The stored-roster variant of this check
(replay_board.py --determinism) needs local artifacts; this fixture
version runs everywhere, on every PR.
"""
import copy
import io
import contextlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("CLAUDE_API_KEY", "test")
import Takeoff_DIRECT as T  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def fixture():
    rooms1 = []
    for i in range(14):
        rooms1.append({
            "room_id": f"R1{i:02d}", "room_number": f"1{i:02d}",
            "room_name": f"Office {i}", "in_scope": True,
            "dimensions": {"wall_area_sqft": 320 + 7 * i,
                           "ceiling_area_sqft": 140 + 3 * i,
                           "ceiling_height_feet": 9},
            "materials": {"walls": "gypsum", "ceiling": "GWB",
                          "ceiling_painted": True},
            "elements": {"doors_full_paint": 1 + (i % 2),
                         "base_trim_lf": 40 + i}})
    rooms2 = [{
        "room_id": "TYP-A", "room_number": "201", "room_name": "Typ Suite",
        "in_scope": True, "unit_multiplier": 6,
        "dimensions": {"wall_area_sqft": 480, "ceiling_area_sqft": 210,
                       "ceiling_height_feet": 9},
        "materials": {"walls": "gypsum", "ceiling": "GWB",
                      "ceiling_painted": True},
        "elements": {"doors_full_paint": 2, "base_trim_lf": 55}}]
    sched = [{"room_number": f"1{i:02d}", "room_name": f"Office {i}",
              "wall_finish": "PT-1", "ceiling_finish": "GWB"}
             for i in range(14)]
    agg = {"total_paintable_wall_sqft": sum(
               r["dimensions"]["wall_area_sqft"] for r in rooms1) + 6 * 480,
           "total_paintable_ceiling_sqft": sum(
               r["dimensions"]["ceiling_area_sqft"] for r in rooms1) + 6 * 210,
           "total_doors_full_paint": sum(
               r["elements"]["doors_full_paint"] for r in rooms1) + 12,
           "total_base_trim_lf": sum(
               r["elements"]["base_trim_lf"] for r in rooms1) + 6 * 55,
           "total_wallcovering_sqft": 800.0,
           "total_windows_painted_interior": 9}
    led = [{"stage": "aggregation", "item": k, "from": 0.0,
            "to": float(v), "delta": float(v), "source": "measured",
            "basis": "fixture"} for k, v in sorted(agg.items())]
    return {
        "project_info": {"building_type": "commercial office"},
        "room_finish_schedule": sched,
        "floors": [{"floor_name": "L1", "rooms": rooms1},
                   {"floor_name": "L2", "rooms": rooms2}],
        "aggregated_totals": dict(agg),
        "_quantity_adjustments": led,
        "notes": [],
    }


PHASE1_FLAGS = {
    "NIGHTSHIFT_SCHEDULE_ROOM_SCOPE": "1",
    "NIGHTSHIFT_SCHEDULE_SCOPE_AUTHORITATIVE": "1",
    "NIGHTSHIFT_SCHEDULE_ROOM_ANCHOR": "1",
    "NIGHTSHIFT_PRICE_UNPRICED_CLASSES": "1",
    "NIGHTSHIFT_VME_CEILINGS": "1",
    # DOOR_SCHEDULE_LEDGER left off here: it reads PDFs, absent in CI.
}


def run_once(flags):
    saved = {k: os.environ.get(k) for k in flags}
    os.environ.update(flags)
    try:
        a = fixture()
        with contextlib.redirect_stdout(io.StringIO()):
            out = T.build_priced_takeoff(copy.deepcopy(a))
        return json.dumps(out.get("aggregated_totals"), sort_keys=True)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.update(
                {k: v})


print("chain determinism checks")

a1 = run_once({})
a2 = run_once({})
check(a1 == a2, "default posture: two replays are bit-identical")

b1 = run_once(PHASE1_FLAGS)
b2 = run_once(PHASE1_FLAGS)
check(b1 == b2, "phase-1 flag set: two replays are bit-identical")
if b1 != b2:
    print("   run1:", b1[:200])
    print("   run2:", b2[:200])

print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all chain determinism checks passed")
