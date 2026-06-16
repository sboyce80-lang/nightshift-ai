"""Regression tests for the ceiling_painted over-count (TSC / Highland job).

Two stacked defects priced ~$36K of unpainted ceiling:
  A. ceiling_painted arrives as the STRING "False"; bool("False") is True, so
     a raw truthiness gate counts every unpainted ceiling.
  B. ACT / acoustic / exposed ceilings get flagged painted (by the model or by
     the cross-sheet OR/vote) and the whole deck gets priced.

On the live TSC job these together turned 1,596 SF of real (GYP) paintable
ceiling into 42,494 SF. These tests pin the behaviour so it can't regress.
"""
import sys

import Takeoff_DIRECT as T

_fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        _fails.append(name)


print("A — _as_bool coerces schema-boolean strings correctly")
check('"False" -> False', T._as_bool("False") is False)
check('"false" -> False', T._as_bool("false") is False)
check('"" -> False', T._as_bool("") is False)
check("None -> False", T._as_bool(None) is False)
check("0 -> False", T._as_bool(0) is False)
check('"True" -> True', T._as_bool("True") is True)
check("True -> True", T._as_bool(True) is True)
check("1 -> True", T._as_bool(1) is True)


def _room(name, ceil_mat, painted, area):
    return {
        "room_id": name, "room_name": name, "in_scope": True,
        "dimensions": {"wall_area_sqft": 100, "ceiling_area_sqft": area,
                       "floor_area_sqft": area},
        "materials": {"walls": "GYP", "ceiling": ceil_mat, "ceiling_painted": painted},
        "elements": {},
        "notes": "",
    }


def _ceiling_total(rooms):
    a = T._recalculate_totals({"project_info": {"building_type": "commercial retail"},
                               "floors": [{"floor_name": "1", "rooms": rooms}]})
    return a["aggregated_totals"]["total_paintable_ceiling_sqft"], a


print("\nDefect A — string 'False' is NOT counted as painted")
tot, a = _ceiling_total([_room("R", "GYP", "False", 5000)])
check("GYP ceiling flagged string 'False' -> 0 SF", tot == 0, f"got {tot}")
check("flag normalized to real bool in saved room",
      a["floors"][0]["rooms"][0]["materials"]["ceiling_painted"] is False)

print("\nDefect B — ACT is not painted even when flagged True")
tot, _ = _ceiling_total([_room("Sales", "ACT", "True", 20000)])
check("ACT ceiling flagged 'True' -> 0 SF", tot == 0, f"got {tot}")
tot, _ = _ceiling_total([_room("Sales", "ACT-1", True, 16000)])
check("ACT-1 ceiling flagged bool True -> 0 SF", tot == 0, f"got {tot}")

print("\nNo false negatives — real GYP painted ceilings still count")
tot, _ = _ceiling_total([_room("Office", "GYP", "True", 300)])
check("GYP ceiling flagged 'True' -> 300 SF", tot == 300, f"got {tot}")
tot, _ = _ceiling_total([_room("Office", "GWB", True, 250)])
check("GWB ceiling bool True -> 250 SF", tot == 250, f"got {tot}")

print("\nMixed set mirrors the TSC job (GYP kept, ACT + 'False' dropped)")
rooms = [
    _room("Retail Sales", "ACT", "True", 20115),   # B
    _room("Stockroom", "ACT", "True", 4065),       # B
    _room("Main Sales Floor", "ACT-1", "False", 16000),  # A + B
    _room("Vestibule", "GYP", "True", 282),        # keep
    _room("Mgr Office", "GYP", "True", 171),       # keep
]
tot, _ = _ceiling_total(rooms)
check("only the two GYP rooms survive -> 453 SF", tot == 453, f"got {tot}")

print(f"\n=== {'ALL PASS' if not _fails else str(len(_fails)) + ' FAILED: ' + ', '.join(_fails)} ===")
sys.exit(1 if _fails else 0)
