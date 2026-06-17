"""Regression tests for the small-commercial floor fixes
(NIGHTSHIFT_SMALL_COMMERCIAL_FIX), from the 2026-06-16 Dutchess validation:

  (A) _drop_non_floor_plan_pseudo_floors: foundation / building-sections /
      roof-plan / construction-details sheets promoted to 'floors' (0 paintable
      area) are dropped; floor + footprint counts stop inflating.
  (B) _dedupe_small_commercial_floors: the per-sheet gate is waived under the
      flag so legacy/multi-pass small-commercial jobs (Dutchess never set
      _per_sheet_extraction) get their cross-sheet re-draws deduped.

Guards: a non-floor-NAMED floor that carries real area is NOT dropped; flag-off
is a no-op. Offline; also checks the saved Dutchess prod JSON when present.
"""
import os
os.environ["NIGHTSHIFT_SMALL_COMMERCIAL_FIX"] = "1"
import json
import Takeoff_DIRECT as T

fails = []


def check(c, m):
    if not c:
        fails.append(m)


def _room(name, wall=0, ceiling=0, page=1):
    return {"room_name": name, "in_scope": True, "source_page": page,
            "materials": {"walls": "GYP", "ceiling": "GYP", "ceiling_painted": True},
            "dimensions": {"wall_area_sqft": wall, "ceiling_area_sqft": ceiling}}


def _wallsum(an):
    return sum(T._num((r.get("dimensions") or {}).get("wall_area_sqft", 0))
              for fl in an.get("floors", []) for r in fl.get("rooms", [])
              if isinstance(r, dict) and r.get("in_scope", True))


# (A) phantom non-floor sheets dropped; real floors kept.
an = {
    "project_info": {"building_type": "commercial", "total_floors_analyzed": 6},
    "floors": [
        {"floor_name": "Foundation Plan (Slab Level)", "rooms": []},
        {"floor_name": "First Floor", "rooms": [_room("Storage 101", 400)]},
        {"floor_name": "Second Floor", "rooms": [_room("Office 201", 300)]},
        {"floor_name": "Building Sections (A105)", "rooms": [_room("Sec", 0)]},
        {"floor_name": "Roof Plan (A108)", "rooms": []},
        {"floor_name": "Construction Details Sheet A-112", "rooms": [_room("Det", 0)]},
    ],
}
T._drop_non_floor_plan_pseudo_floors(an)
names = [f["floor_name"] for f in an["floors"]]
check(names == ["First Floor", "Second Floor"], f"phantom drop wrong: {names}")
check(an["project_info"]["total_floors_analyzed"] == 2, "floor count not updated")

# Guard: non-floor NAME but real area -> NOT dropped (fail-safe).
an2 = {"project_info": {"building_type": "commercial"},
       "floors": [{"floor_name": "Roof Plan (occupied roof deck)",
                   "rooms": [_room("Deck", 1200, 1500)]}]}
T._drop_non_floor_plan_pseudo_floors(an2)
check(len(an2["floors"]) == 1, "real-area floor wrongly dropped")

# (B) dedup runs on small-commercial with NO _per_sheet_extraction (flag waives).
an3 = {
    "project_info": {"building_type": "commercial"},
    "floors": [{"floor_name": "First Floor", "rooms": [
        _room("Women's 104", 1000, page=1), _room("Men's 108", 900, page=1),
        _room("Storage 101", 400, page=1), _room("Office 113", 500, page=1),
        # page 2 = enlarged re-draws of the same types -> should drop
        _room("Women's Restroom West", 1100, page=2),
        _room("Men's Restroom", 800, page=2),
    ]}],
}
before = _wallsum(an3)
check(not an3.get("_per_sheet_extraction"), "fixture should have no per-sheet flag")
T._dedupe_small_commercial_floors(an3)
after = _wallsum(an3)
check(after < before, f"dedup did not run without per-sheet (before={before}, after={after})")
check(abs(after - 2800) < 1, f"expected page-1 kept (2800), got {after}")

# Flag OFF -> both are no-ops.
os.environ["NIGHTSHIFT_SMALL_COMMERCIAL_FIX"] = "0"
an4 = {"project_info": {"building_type": "commercial"},
       "floors": [{"floor_name": "Roof Plan", "rooms": [_room("x", 0)]}]}
T._drop_non_floor_plan_pseudo_floors(an4)
check(len(an4["floors"]) == 1, "flag-off should not drop floors")
an5 = json.loads(json.dumps(an3))  # already-deduped copy; ensure no per-sheet
an5.pop("_sc_floor_deduped", None)
b5 = _wallsum(an5)
T._dedupe_small_commercial_floors(an5)
check(_wallsum(an5) == b5, "flag-off dedup should be a no-op without per-sheet")
os.environ["NIGHTSHIFT_SMALL_COMMERCIAL_FIX"] = "1"

# Real Dutchess prod JSON, when present.
p = "/tmp/results_json/Dutchess.json"
if os.path.exists(p):
    an = json.load(open(p))["analysis"]
    T._drop_non_floor_plan_pseudo_floors(an)
    check(len(an["floors"]) == 2, f"Dutchess floors should be 2, got {len(an['floors'])}")
    an.pop("_sc_floor_deduped", None)
    b = _wallsum(an)
    T._dedupe_small_commercial_floors(an)
    a = _wallsum(an)
    check(a < b - 3000, f"Dutchess dedup should drop >3000 SF (b={b:.0f} a={a:.0f})")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
