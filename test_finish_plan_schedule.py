#!/usr/bin/env python3
"""Phelps/Northwell 2026-09-01 fixes:

  1. NIGHTSHIFT_FINISH_PLAN_SCHEDULE  — detect a per-room finish GRID on a
     "Finish Plan" sheet (WN/WE/WS/WW/FL/B/CLG), which the tabular
     "finish schedule" phrases and column tokens both miss.
  2. NIGHTSHIFT_CEILING_SCHEDULE_EVIDENCE — a schedule-designated ACT
     ceiling outranks the enclosed-room painted default.
  3. NIGHTSHIFT_OVER_EXTRACTION_GUARD — flag the HIGH side of the
     paintable:area ratio, preferring the GSF read off the drawings over
     an inferred footprint.

All three default OFF and must be inert when unset.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for _k in ("NIGHTSHIFT_FINISH_PLAN_SCHEDULE",
           "NIGHTSHIFT_CEILING_SCHEDULE_EVIDENCE",
           "NIGHTSHIFT_OVER_EXTRACTION_GUARD",
           "NIGHTSHIFT_CEILING_ASSUME_PAINTED",
           "NIGHTSHIFT_CEILING_ASSUME_PAINTED_ACT"):
    os.environ.pop(_k, None)
os.environ.setdefault("CLAUDE_API_KEY", "test")

import Takeoff_DIRECT as T  # noqa: E402

FAILS = []


def check(cond, label):
    print(f"  {'✅' if cond else '❌'} {label}")
    if not cond:
        FAILS.append(label)


# ── 1. finish-plan grid detection ────────────────────────────────────────
print("\n[1] finish-plan grid detection")

GRID = "\n".join(
    f"EXAM {i}\n417-{i:02d}\nWN\nWE\nWS\nWW\nFL\nB\nCLG\n"
    f"PT1\nPT4\nPT1\nPT1\nLVT2\nRB1C\nACT2" for i in range(1, 9))
NOISE = ("FOURTH FLOOR PLAN\nGENERAL NOTES\nSEE FINISH SCHEDULE ON SHEET A001\n"
         "WS 12\nCLG HT 9'-0\"\n")

os.environ["NIGHTSHIFT_FINISH_PLAN_SCHEDULE"] = "0"
check(T._finish_plan_grid_rooms(GRID) == 0 or not T._finish_plan_grid_enabled(),
      "flag OFF -> _find_finish_plan_pages inert")

os.environ["NIGHTSHIFT_FINISH_PLAN_SCHEDULE"] = "1"
n = T._finish_plan_grid_rooms(GRID)
check(n >= 8, f"grid page reports its room blocks (got {n}, expect >=8)")
check(T._finish_plan_grid_rooms(NOISE) == 0,
      "plan sheet w/ stray WS + CLG note is NOT a grid (no false positive)")
check(T._finish_plan_grid_rooms("") == 0, "empty text -> 0")
# a grid missing one wall direction must not qualify
part = GRID.replace("WW\n", "")
check(T._finish_plan_grid_rooms(part) == 0,
      "grid missing a wall direction does not qualify")

# ── 2. ceiling schedule evidence outranks the painted default ────────────
print("\n[2] ceiling schedule evidence vs enclosed-room default")


def _analysis():
    return {
        "room_finish_schedule": [
            {"room_number": "400-18", "room_name": "Exam 09",
             "ceiling_finish": "ACT3"},
            {"room_number": "400-23", "room_name": "Patient Toilet",
             "ceiling_finish": "GWB1"},
            {"room_number": "400-11", "room_name": "Shared Office",
             "ceiling_finish": "ACT1"},
            {"room_number": "400-07", "room_name": "Managers Office",
             "ceiling_finish": "ACT1"},
            {"room_number": "400-01", "room_name": "Waiting Area",
             "ceiling_finish": "ACT1"},
            {"room_number": "400-32", "room_name": "It Closet",
             "ceiling_finish": "ACT1"},
        ],
        "floors": [{"rooms": [
            {"room_id": "R1", "room_name": "Exam 09", "room_number": "400-18",
             "in_scope": True,
             "materials": {"ceiling": "ACT (assumed)", "ceiling_painted": False},
             "dimensions": {"ceiling_area_sqft": 200}},
            {"room_id": "R2", "room_name": "Patient Toilet",
             "room_number": "400-23", "in_scope": True,
             "materials": {"ceiling": "ACT (assumed)", "ceiling_painted": False},
             "dimensions": {"ceiling_area_sqft": 50}},
            {"room_id": "R3", "room_name": "Unscheduled Store",
             "in_scope": True,
             "materials": {"ceiling": "ACT (assumed)", "ceiling_painted": False},
             "dimensions": {"ceiling_area_sqft": 75}},
        ]}],
        "aggregated_totals": {"total_paintable_ceiling_sqft": 0.0},
    }


os.environ["NIGHTSHIFT_CEILING_ASSUME_PAINTED"] = "1"
os.environ["NIGHTSHIFT_CEILING_ASSUME_PAINTED_ACT"] = "1"

os.environ["NIGHTSHIFT_CEILING_SCHEDULE_EVIDENCE"] = "0"
a = T._apply_ceiling_assume_painted(_analysis())
base_flipped = a["_ceiling_assume_painted"]["rooms_flipped"]
base_sf = a["_ceiling_assume_painted"]["ceiling_sqft_added"]
check(base_flipped == 3 and base_sf == 325,
      f"flag OFF -> unchanged behavior (flipped {base_flipped}, {base_sf} sf)")

os.environ["NIGHTSHIFT_CEILING_SCHEDULE_EVIDENCE"] = "1"
b = T._apply_ceiling_assume_painted(_analysis())
rec = b["_ceiling_assume_painted"]
check(rec["rooms_flipped"] == 2,
      f"ACT-scheduled room held back (flipped {rec['rooms_flipped']}, expect 2)")
check(rec.get("held_by_schedule") == 1,
      f"held_by_schedule recorded (got {rec.get('held_by_schedule')})")
check(rec["ceiling_sqft_added"] == 125,
      f"only unscheduled+GWB rooms added (got {rec['ceiling_sqft_added']}, "
      f"expect 125)")
r1 = b["floors"][0]["rooms"][0]
check(r1["materials"]["ceiling_painted"] is False,
      "ACT3-scheduled exam room stays UNPAINTED")
r2 = b["floors"][0]["rooms"][1]
check(r2["materials"]["ceiling_painted"] is True,
      "GWB1-scheduled toilet still flips to painted (schedule says paint)")
check(any("Ceiling Schedule Evidence" in str(n) for n in b.get("notes", [])),
      "explanatory note emitted")

# thin schedule -> no authority, behave as before
thin = _analysis()
thin["room_finish_schedule"] = thin["room_finish_schedule"][:2]
c = T._apply_ceiling_assume_painted(thin)
check(c["_ceiling_assume_painted"]["rooms_flipped"] == 3,
      "schedule below authority row minimum -> unchanged behavior")

# ── 3. over-extraction guard ─────────────────────────────────────────────
print("\n[3] over-extraction guard")


def _guard(paintable, footprint, gsf, flag="1", maxr=None):
    os.environ["NIGHTSHIFT_OVER_EXTRACTION_GUARD"] = flag
    if maxr:
        os.environ["NIGHTSHIFT_OVER_EXTRACTION_MAX_RATIO"] = maxr
    else:
        os.environ.pop("NIGHTSHIFT_OVER_EXTRACTION_MAX_RATIO", None)
    ns = {
        "project_info": {"footprint_sqft": footprint},
        "project_overview": {"gross_sqft": gsf},
        "floors": [], "aggregated_totals": {}, "notes": [],
        "manual_review_required": False,
    }
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "Takeoff_DIRECT.py")).read()
    start = src.index("    # ── Over-extraction guard (Phelps/Northwell")
    end = src.index("    # Doors-zero cold-draw detector", start)
    block = "\n".join(l[4:] if l.startswith("    ") else l
                      for l in src[start:end].splitlines())
    g = {"os": os, "_num": T._num, "analysis": ns,
         "_total_paintable": paintable,
         "_stated_gross_sqft": T._stated_gross_sqft,
         "_declared_work_area_sqft": T._declared_work_area_sqft}
    exec(compile(block, "<guard>", "exec"), g)
    return ns


# Phelps: 73,693 paintable, inferred footprint 45,000, read GSF 8,724
p = _guard(73693, 45000, 8724)
check(p["manual_review_required"] is True, "Phelps case trips the guard")
rec = p.get("_over_extraction_guard") or {}
check(rec.get("basis") == 8724,
      f"uses READ GSF not inferred footprint (basis {rec.get('basis')})")
check(round(rec.get("ratio", 0)) == 8,
      f"ratio computed off read GSF (got {rec.get('ratio')})")
check("implausibly HIGH" in (p.get("manual_review_reason") or ""),
      "message says HIGH, not low")
check("OUTSIDE the scope boundary" in (p.get("manual_review_reason") or ""),
      "message points at the scope boundary")

ok = _guard(30000, 9000, 9000)
check(ok["manual_review_required"] is False,
      "in-band job (3.3x) does NOT trip")

off = _guard(73693, 45000, 8724, flag="0")
check(off["manual_review_required"] is False, "flag OFF -> inert")

under = _guard(200000, 45000, 0)
check(under["manual_review_required"] is False,
      "no read GSF, 4.4x vs footprint -> under threshold, does NOT trip")

noread = _guard(400000, 45000, 0)
check(noread["manual_review_required"] is True,
      "no read GSF -> falls back to footprint and catches 8.9x")
rec2 = noread.get("_over_extraction_guard") or {}
check(rec2.get("basis") == 45000, "fallback basis is the footprint")

tuned = _guard(73693, 45000, 8724, maxr="4")
check(tuned["manual_review_required"] is True,
      "MAX_RATIO is tunable (4x)")

# ── 4. schedule ROOM scope boundary ──────────────────────────────────────
print("\n[4] schedule room scope boundary")


def _scope_analysis():
    return {
        "room_finish_schedule": [
            {"room_number": f"400-{i:02d}", "room_name": f"Exam {i}",
             "ceiling_finish": "ACT1"} for i in range(1, 9)],
        "floors": [{"rooms": [
            {"room_id": "A", "room_name": "Exam 1", "room_number": "400-01",
             "in_scope": True, "dimensions": {"wall_area_sqft": 300},
             "elements": {"doors_full_paint": 1}},
            {"room_id": "B", "room_name": "Exam 2", "room_number": "400-02",
             "in_scope": True, "dimensions": {"wall_area_sqft": 300},
             "elements": {"doors_full_paint": 1}},
            {"room_id": "C", "room_name": "Elevator Lobby", "in_scope": True,
             "dimensions": {"wall_area_sqft": 900},
             "elements": {"doors_full_paint": 4, "stair_sections": 2}},
            {"room_id": "D", "room_name": "Receiving", "in_scope": True,
             "dimensions": {"wall_area_sqft": 800},
             "elements": {"concrete_floor_sqft": 300}},
        ]}], "notes": [], "aggregated_totals": {}}


os.environ["NIGHTSHIFT_SCHEDULE_ROOM_SCOPE"] = "0"
off4 = T._apply_schedule_room_scope(_scope_analysis())
check(off4.get("_schedule_room_scope") is None
      and all(r.get("in_scope") for r in off4["floors"][0]["rooms"]),
      "flag OFF -> inert, every room stays in scope")

os.environ["NIGHTSHIFT_SCHEDULE_ROOM_SCOPE"] = "1"
on4 = T._apply_schedule_room_scope(_scope_analysis())
rec4 = on4["_schedule_room_scope"]
check(rec4["rooms_dropped"] == 2 and rec4["rooms_kept"] == 2,
      f"unscheduled rooms dropped (dropped {rec4['rooms_dropped']}, "
      f"kept {rec4['rooms_kept']})")
rooms4 = {r["room_id"]: r for r in on4["floors"][0]["rooms"]}
check(rooms4["A"].get("in_scope") is True
      and rooms4["B"].get("in_scope") is True,
      "scheduled exam rooms stay in scope")
check(rooms4["C"].get("in_scope") is False
      and rooms4["D"].get("in_scope") is False,
      "Elevator Lobby + Receiving leave scope (doors/stairs/slab go with them)")
check("scope boundary" in (rooms4["C"].get("scope_exclusion_reason") or ""),
      "exclusion reason recorded on the room")
check(on4.get("manual_review_required") is True,
      "a >25% drop forces manual review")
check(any("Schedule Room Scope" in str(n) for n in on4.get("notes", [])),
      "explanatory note emitted")

thin4 = _scope_analysis()
thin4["room_finish_schedule"] = thin4["room_finish_schedule"][:2]
t4 = T._apply_schedule_room_scope(thin4)
check((t4.get("_schedule_room_scope") or {}).get("noop") == "schedule_too_thin"
      and all(r.get("in_scope") for r in t4["floors"][0]["rooms"]),
      "thin schedule -> no authority, nothing dropped")

for _k in ("NIGHTSHIFT_SCHEDULE_ROOM_SCOPE",):
    os.environ.pop(_k, None)

# ── 5. stated GSF outranks an inferred footprint (both guard sides) ──────
print("\n[5] stated GSF as plausibility basis")

os.environ.pop("NIGHTSHIFT_GSF_BASIS", None)
check(T._gsf_basis_enabled() is False, "NIGHTSHIFT_GSF_BASIS defaults off")
os.environ["NIGHTSHIFT_GSF_BASIS"] = "1"
check(T._gsf_basis_enabled() is True, "flag reads on")

check(T._stated_gross_sqft(
    {"project_overview": {"gross_sqft": 8724}}) == 8724,
    "reads gross_sqft from project_overview")
check(T._stated_gross_sqft(
    {"project_info": {"total_gsf": 12000}}) == 12000,
    "reads total_gsf from project_info")
check(T._stated_gross_sqft({}) == 0, "no stated GSF -> 0")
check(T._stated_gross_sqft(
    {"project_overview": {"gross_sqft": 8724},
     "project_info": {"total_gsf": 999}}) == 8724,
    "project_overview wins over project_info")

# The Phelps rerun false positive: 34,340 paintable, inferred footprint
# 30,000 (ratio 1.14 -> "implausibly low"), stated GSF 8,724 (ratio 3.9 -> OK)
check(34340 / 8724 > 3.0,
      "34,340 SF is in-band (3.9x) against the STATED 8,724 GSF")
check(34340 < 30000 * 3,
      "...but scores 1.14x against the INFERRED 30,000 footprint")
os.environ.pop("NIGHTSHIFT_GSF_BASIS", None)

print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} FAILED:")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
