"""Regression tests for _enforce_ceiling_scope_gate (NIGHTSHIFT_CEILING_SCOPE_GATE).

Locks in the 2026-06-16 ceiling over-scoping fix found by the TSC/Honey/Dutchess/
364 prod-vs-Rider validation:
  (1) ACT/acoustic/suspended ceilings re-painted by the cross-sheet merge are
      hard-demoted (TSC: 24,180 SF of Retail Sales + Stockroom acoustic tile).
  (2) On COMMERCIAL buildings the ceiling aggregate is rebuilt (only-reduce)
      from the gated, deduped room set (Honey: agg 12,339 -> rooms 2,226).
Guards: residential/mixed-use aggregate is preserved (364 Main's GSF floor);
flag-off is a no-op; a GYP paint-trigger overrides the ACT demotion.
Offline, no API. Also runs the real saved prod JSONs when present.
"""
import os
os.environ["NIGHTSHIFT_CEILING_SCOPE_GATE"] = "1"
import json
import Takeoff_DIRECT as T

fails = []


def _an(building_type, rooms, agg_ceiling):
    return {
        "project_info": {"building_type": building_type},
        "aggregated_totals": {"total_paintable_ceiling_sqft": agg_ceiling},
        "floors": [{"floor_name": "1", "rooms": rooms}],
    }


def _room(name, ceiling, painted, ceil_area, in_scope=True):
    return {
        "room_name": name, "in_scope": in_scope,
        "materials": {"walls": "GYP", "ceiling": ceiling,
                      "ceiling_painted": painted},
        "dimensions": {"ceiling_area_sqft": ceil_area,
                       "floor_area_sqft": ceil_area},
        "notes": "",
    }


def check(cond, msg):
    if not cond:
        fails.append(msg)


# 1) ACT marked painted (string "True", as the merge leaves it) -> demoted,
#    excluded from the commercial aggregate.
a = _an("commercial retail", [
    _room("Retail Sales", "ACT", "True", 20115),   # string bool, ACT
    _room("Stockroom", "ACT", True, 4065),          # real bool, ACT
    _room("Office", "GYP", "True", 200),            # real painted GYP
], agg_ceiling=42494)
T._enforce_ceiling_scope_gate(a)
rooms = a["floors"][0]["rooms"]
check(rooms[0]["materials"]["ceiling_painted"] is False, "ACT(str) not demoted")
check(rooms[1]["materials"]["ceiling_painted"] is False, "ACT(bool) not demoted")
check(rooms[2]["materials"]["ceiling_painted"] is True, "GYP wrongly demoted")
check(rooms[0]["dimensions"]["ceiling_area_sqft"] == 0, "demoted ceil_area not zeroed")
got = a["aggregated_totals"]["total_paintable_ceiling_sqft"]
check(abs(got - 200) < 1, f"commercial aggregate should rebuild to 200 GYP, got {got}")
check(a["_ceiling_scope_gate"]["act_rooms_demoted"] == 2, "act_rooms_demoted != 2")

# 2) Commercial stale aggregate, no ACT -> rebuilt down to room sum (Honey case).
a = _an("commercial", [
    _room("Sales", "GYP", "True", 1500),
    _room("Back", "GYP", "True", 726),
], agg_ceiling=12339)
T._enforce_ceiling_scope_gate(a)
got = a["aggregated_totals"]["total_paintable_ceiling_sqft"]
check(abs(got - 2226) < 1, f"Honey-style rebuild expected 2226, got {got}")

# 3) Residential/mixed-use aggregate is PRESERVED (364 Main GSF floor).
a = _an("mixed-use", [_room("Apt", "GYP", "True", 1000)], agg_ceiling=34682)
T._enforce_ceiling_scope_gate(a)
got = a["aggregated_totals"]["total_paintable_ceiling_sqft"]
check(got == 34682, f"residential aggregate must be preserved, got {got}")

# 4) Only-reduce: commercial where rooms exceed agg -> do NOT inflate.
a = _an("commercial", [_room("Big", "GYP", "True", 5000)], agg_ceiling=3000)
T._enforce_ceiling_scope_gate(a)
got = a["aggregated_totals"]["total_paintable_ceiling_sqft"]
check(got == 3000, f"only-reduce violated: agg inflated to {got}")

# 5) Flag OFF -> no-op.
os.environ["NIGHTSHIFT_CEILING_SCOPE_GATE"] = "0"
a = _an("commercial", [_room("Retail", "ACT", "True", 20000)], agg_ceiling=20000)
T._enforce_ceiling_scope_gate(a)
check(a["aggregated_totals"]["total_paintable_ceiling_sqft"] == 20000,
      "flag-off should be a no-op")
check("_ceiling_scope_gate" not in a, "flag-off should not stamp record")
os.environ["NIGHTSHIFT_CEILING_SCOPE_GATE"] = "1"

# 6) Paint trigger overrides ACT demotion (e.g. "GYP soffit below ACT").
a = _an("commercial", [_room("Mixed", "ACT / GYP soffit", "True", 300)],
        agg_ceiling=300)
T._enforce_ceiling_scope_gate(a)
check(a["floors"][0]["rooms"][0]["materials"]["ceiling_painted"] is True,
      "paint-trigger room wrongly demoted")

# 7) Real saved prod JSONs (when present) — the values from the live run.
EXPECT = {"TSC": (1596, 2000), "Honey": (2226, 2), "Dutchess": (7128, 0),
          "364Main": (34682, 0)}
for name, (lo_hi, _) in EXPECT.items():
    p = f"/tmp/results_json/{name}.json"
    if not os.path.exists(p):
        continue
    res = json.load(open(p))
    an = res["analysis"]
    an.pop("_ceiling_scope_gate", None)
    T._enforce_ceiling_scope_gate(an)
    got = an["aggregated_totals"]["total_paintable_ceiling_sqft"]
    check(abs(got - lo_hi) <= max(5, lo_hi * 0.02),
          f"{name} prod JSON ceiling expected ~{lo_hi}, got {got:.0f}")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
