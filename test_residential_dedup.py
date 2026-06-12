#!/usr/bin/env python3
"""Offline tests for Phase 1(b): residential cross-sheet dedup,
enlarged-plan pseudo-floor dedup, and the symmetric ceiling vote.

Pins the fixes for the 2026-06-12 Fishkill validation findings:
  * enlarged unit-plan sheets extracted as pseudo-floors duplicated rooms
    already on the ranged floors (~4-10x wall inflation);
  * cross-sheet dedup was commercial-gated and skipped residential
    entirely;
  * the ceiling vote only ever flipped painted->False, so the keeper
    (floor-plan instance, no ceiling data) lost the RCP instance's
    ceiling entirely.

Run: python3 test_residential_dedup.py
"""
import importlib.util as iu
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = iu.spec_from_file_location("T", os.path.join(HERE, "Takeoff_DIRECT.py"))
T = iu.module_from_spec(spec)
spec.loader.exec_module(T)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def room(name, wall=0, ceil=0, floor_a=0, page=1, rid=None, mult=1,
         ceiling_painted=False):
    return {
        "room_id": rid or name.replace(" ", "-").upper(),
        "room_name": name,
        "source_page": page,
        "unit_multiplier": mult,
        "dimensions": {"wall_area_sqft": wall, "ceiling_area_sqft": ceil,
                       "floor_area_sqft": floor_a, "perimeter_lf": 0},
        "materials": {"ceiling_painted": ceiling_painted},
        "elements": {},
        "in_scope": True,
    }


def wall_sum(analysis):
    return sum(
        T._num(r["dimensions"]["wall_area_sqft"]) *
        max(1, int(T._num(r.get("unit_multiplier", 1))))
        for fl in analysis["floors"] for r in fl.get("rooms", []))


def test_residential_cross_sheet():
    # Same unit's bedroom on floor plan (p5) and RCP (p9) -> merged.
    # Different units' bedrooms (201 vs 202) -> NOT merged.
    a = {
        "project_info": {"building_type": "residential multifamily",
                         "total_units": 12},
        "floors": [{
            "floor_name": "2nd Floor",
            "rooms": [
                room("Unit 201 Bedroom", wall=400, page=5),
                room("Unit 201 Bedroom", wall=380, ceil=150, page=9,
                     rid="U201-BED-RCP", ceiling_painted=True),
                room("Unit 202 Bedroom", wall=410, page=5),
            ],
        }],
    }
    T._dedupe_cross_sheet_rooms(a)
    walls = [T._num(r["dimensions"]["wall_area_sqft"])
             for r in a["floors"][0]["rooms"]]
    check("residential: same-unit cross-sheet duplicate zeroed",
          sorted(walls) == [0, 400, 410], walls)
    keeper = a["floors"][0]["rooms"][0]
    check("residential: symmetric ceiling vote flips keeper painted",
          keeper["materials"]["ceiling_painted"] is True, keeper["materials"])
    check("residential: keeper ceiling area backfilled from RCP instance",
          T._num(keeper["dimensions"]["ceiling_area_sqft"]) == 150,
          keeper["dimensions"])
    check("residential: different units NOT merged",
          T._num(a["floors"][0]["rooms"][2]["dimensions"]["wall_area_sqft"]) == 410)


def test_residential_no_unit_token_untouched():
    # Two generic "Corridor" rooms on different pages, no unit tokens —
    # must NOT merge in residential mode (could be two real corridors).
    a = {
        "project_info": {"building_type": "apartment"},
        "floors": [{
            "floor_name": "1st Floor",
            "rooms": [room("Corridor", wall=300, page=4),
                      room("Corridor", wall=280, page=8, rid="CORR-B")],
        }],
    }
    T._dedupe_cross_sheet_rooms(a)
    walls = sorted(T._num(r["dimensions"]["wall_area_sqft"])
                   for r in a["floors"][0]["rooms"])
    check("residential: token-less same-name rooms left alone",
          walls == [280, 300], walls)


def test_union_variant_dedup():
    # Multi-pass-union naming variants of ONE room, all on the same page,
    # with drifting dimensions — must collapse to the most complete.
    # Distinct numbered rooms ('Bath 1' vs 'Bath 2') must survive.
    a = {
        "project_info": {"building_type": "mixed-use residential",
                         "total_units": 15},
        "floors": [{
            "floor_name": "2nd Floor — Residential",
            "rooms": [
                room("Unit 201 — Master Bedroom", wall=510, page=6, rid="A1"),
                room("Unit 201 Bedroom (Master)", wall=460, page=6, rid="A2"),
                room("Master Bedroom — Unit 201", wall=460, page=6, rid="A3"),
                room("Unit 201 — Bath 1", wall=276, page=6, rid="B1"),
                room("Unit 201 — Bath 2", wall=240, page=6, rid="B2"),
                # Generic single-word set: two closets with different sizes
                # are presumed REAL; near-identical ones are duplicates.
                room("Unit 201 — Closet", wall=368, page=6, rid="C1"),
                room("Closet — Unit 201", wall=150, page=6, rid="C2"),
            ],
        }],
    }
    T._dedupe_cross_sheet_rooms(a)
    by_id = {r["room_id"]: T._num(r["dimensions"]["wall_area_sqft"])
             for r in a["floors"][0]["rooms"]}
    check("union variants: master bedroom collapsed to one instance",
          by_id["A1"] == 510 and by_id["A2"] == 0 and by_id["A3"] == 0, by_id)
    check("union variants: numbered baths both survive",
          by_id["B1"] == 276 and by_id["B2"] == 240, by_id)
    check("union variants: differing generic closets both survive",
          by_id["C1"] == 368 and by_id["C2"] == 150, by_id)


def test_commercial_mode_unchanged():
    # Original Five Below behavior must survive: same name, >1 page,
    # single-tenant commercial -> zeroed.
    a = {
        "project_info": {"building_type": "commercial retail", "total_units": 1},
        "floors": [{
            "floor_name": "1st Floor",
            "rooms": [room("Stockroom", wall=900, page=3),
                      room("Stockroom", wall=850, ceil=500, page=7,
                           rid="STOCK-RCP", ceiling_painted=True)],
        }],
    }
    T._dedupe_cross_sheet_rooms(a)
    walls = sorted(T._num(r["dimensions"]["wall_area_sqft"])
                   for r in a["floors"][0]["rooms"])
    check("commercial: cross-sheet duplicate still zeroed",
          walls == [0, 900], walls)
    keeper = a["floors"][0]["rooms"][0]
    check("commercial: ceiling backfill works there too",
          T._num(keeper["dimensions"]["ceiling_area_sqft"]) == 500
          and keeper["materials"]["ceiling_painted"] is True)


def _fishkill_like():
    """3 ranged floors with unit rooms + 2 enlarged-plan pseudo-floors."""
    def unit_rooms(unit_no, page):
        return [room(f"Unit {unit_no} Living Room", wall=350, floor_a=200, page=page,
                     rid=f"U{unit_no}-LIV"),
                room(f"Unit {unit_no} Bedroom", wall=300, floor_a=140, page=page,
                     rid=f"U{unit_no}-BED"),
                room(f"Unit {unit_no} Bathroom", wall=180, floor_a=50, page=page,
                     rid=f"U{unit_no}-BATH")]
    floors = [
        {"floor_name": "1st Floor",
         "rooms": unit_rooms(101, 5) + unit_rooms(102, 5) +
                  [room("Corridor", wall=500, page=5, rid="CORR-1")]},
        {"floor_name": "2nd Floor — Residential",
         "rooms": unit_rooms(201, 6) + unit_rooms(202, 6)},
        {"floor_name": "3rd Floor — Residential",
         "rooms": unit_rooms(301, 7) + unit_rooms(302, 7)},
        # Pseudo-floors from the enlarged unit-plan sheets:
        {"floor_name": "Unit Types x01–x06 (Typical Residential Unit Plans — A-105/A-106/A-107)",
         "rooms": [room("Unit x01 Living Room", wall=360, page=15, rid="UX01-LIV"),
                   room("Unit x01 Bedroom", wall=310, page=15, rid="UX01-BED"),
                   room("Unit x01 Bathroom", wall=185, page=15, rid="UX01-BATH")]},
        {"floor_name": "Special Unit Plans — Units x01 through x06 (Atypical / Corner Units)",
         "rooms": [room("Unit x02 Living Room", wall=355, page=16, rid="UX02-LIV"),
                   room("Unit x02 Bedroom", wall=305, page=16, rid="UX02-BED")]},
    ]
    return {"project_info": {"building_type": "mixed-use residential",
                             "total_units": 15, "total_stories": 3},
            "floors": floors}


def test_enlarged_plan_floors_zeroed():
    a = _fishkill_like()
    before = wall_sum(a)
    T._dedupe_enlarged_plan_floors(a)
    after = wall_sum(a)
    # The two pseudo-floors carried 360+310+185+355+305 = 1,515 sqft of dup walls
    check("enlarged-plan pseudo-floor geometry zeroed",
          before - after == 1515, f"before={before} after={after}")
    check("real floors untouched",
          T._num(a["floors"][0]["rooms"][0]["dimensions"]["wall_area_sqft"]) == 350)
    check("dedup note appended",
          any("[Enlarged-Plan Dedup]" in str(n) for n in a.get("notes", [])))
    # Idempotent on second call
    T._dedupe_enlarged_plan_floors(a)
    check("idempotent", wall_sum(a) == after)


def test_template_source_protected():
    # When the pseudo-floor carries multipliers, it IS the unit-scope
    # source (floor plans show shells) — must NOT be zeroed.
    a = _fishkill_like()
    tmpl = a["floors"][3]
    for r in tmpl["rooms"]:
        r["unit_multiplier"] = 6
    before_tmpl = sum(T._num(r["dimensions"]["wall_area_sqft"]) for r in tmpl["rooms"])
    T._dedupe_enlarged_plan_floors(a)
    after_tmpl = sum(T._num(r["dimensions"]["wall_area_sqft"]) for r in tmpl["rooms"])
    check("multiplied template floor protected from zeroing",
          before_tmpl == after_tmpl and before_tmpl > 0,
          f"{before_tmpl} -> {after_tmpl}")


def test_thin_ranged_floors_protected():
    # When ranged floors are thin (<10 rooms), the unit plans may be the
    # only real source — pseudo-floors must be left alone.
    a = _fishkill_like()
    a["floors"] = [
        {"floor_name": "1st Floor", "rooms": [room("Lobby", wall=200, page=2)]},
        a["floors"][3],
    ]
    before = wall_sum(a)
    T._dedupe_enlarged_plan_floors(a)
    check("thin ranged floors: pseudo-floor left intact",
          wall_sum(a) == before)


def main():
    test_residential_cross_sheet()
    test_residential_no_unit_token_untouched()
    test_union_variant_dedup()
    test_commercial_mode_unchanged()
    test_enlarged_plan_floors_zeroed()
    test_template_source_protected()
    test_thin_ranged_floors_protected()
    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
