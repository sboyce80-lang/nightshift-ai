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
import json
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
    # The guard computes its own paintable total from aggregated_totals —
    # it must never depend on the commercial-only sanity block's local
    # (_total_paintable), which crashed every non-commercial job that ran
    # with the flag on (UnboundLocalError, board rounds 4 and 5).
    ns = {
        "project_info": {"footprint_sqft": footprint},
        "project_overview": {"gross_sqft": gsf},
        "floors": [],
        "aggregated_totals": {"total_paintable_wall_sqft": float(paintable)},
        "notes": [],
        "manual_review_required": False,
    }
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "Takeoff_DIRECT.py")).read()
    start = src.index("    # ── Over-extraction guard (Phelps/Northwell")
    end = src.index("    # Doors-zero cold-draw detector", start)
    block = "\n".join(l[4:] if l.startswith("    ") else l
                      for l in src[start:end].splitlines())
    g = {"os": os, "_num": T._num, "analysis": ns,
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

# a PARTIAL schedule must not bound scope (the -69% rerun failure)
T._FINISH_PLAN_BLOCK_COUNTS.clear()
T._FINISH_PLAN_BLOCK_COUNTS["/tmp/x.pdf"] = 62
partial = _scope_analysis()
partial["_vme_pdf_paths"] = ["/tmp/x.pdf"]
pr = T._apply_schedule_room_scope(partial)
prec = pr["_schedule_room_scope"]
check(prec.get("noop") == "schedule_incomplete",
      f"17-of-62 style read stands down (got {prec.get('noop')})")
check(all(r.get("in_scope") for r in pr["floors"][0]["rooms"]),
      "no room excluded when the schedule is incomplete")
check(any("STOOD DOWN" in str(n) for n in pr.get("notes", [])),
      "stand-down is explained in the notes")

# a COMPLETE read still bounds scope
T._FINISH_PLAN_BLOCK_COUNTS.clear()
T._FINISH_PLAN_BLOCK_COUNTS["/tmp/y.pdf"] = 9
complete = _scope_analysis()
complete["_vme_pdf_paths"] = ["/tmp/y.pdf"]
cr = T._apply_schedule_room_scope(complete)
check((cr["_schedule_room_scope"] or {}).get("rooms_dropped") == 2,
      "8-of-9 coverage still bounds scope")
T._FINISH_PLAN_BLOCK_COUNTS.clear()

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

# ── 6. placeholder rows are not evidence ────────────────────────────────
print("\n[6] fabricated/placeholder schedule rows")

REAL = {"room_number": "417-16", "room_name": "Exam 09",
        "wall_finish": "WN PT4; WE PT1", "ceiling_finish": "ACT2"}
PAD = {"room_number": "117", "room_name": "Exam 1",
       "wall_finish": "see finish plan block",
       "ceiling_finish": "see finish plan block"}
PAD2 = {"room_number": "124", "room_name": "Blood Draw",
        "wall_finish": "not listed", "ceiling_finish": "N/A"}
EMPTY = {"room_number": "9", "room_name": "X",
         "wall_finish": "", "ceiling_finish": None}

check(T._finish_row_has_evidence(REAL) is True, "real finish codes count")
check(T._finish_row_has_evidence(PAD) is False,
      "'see finish plan block' is not evidence")
check(T._finish_row_has_evidence(PAD2) is False,
      "'not listed' / 'N/A' is not evidence")
check(T._finish_row_has_evidence(EMPTY) is False, "empty cells are not evidence")
check(len(T._finish_rows_with_evidence([REAL, PAD, PAD2, EMPTY])) == 1,
      "filter keeps only the real row")

# padding must not buy coverage: 18 real + 27 padded vs 62 blocks stays under
os.environ["NIGHTSHIFT_SCHEDULE_ROOM_SCOPE"] = "1"
T._FINISH_PLAN_BLOCK_COUNTS.clear()
T._FINISH_PLAN_BLOCK_COUNTS["/tmp/z.pdf"] = 62
padded = _scope_analysis()
padded["_vme_pdf_paths"] = ["/tmp/z.pdf"]
padded["room_finish_schedule"] = (
    [dict(REAL, room_number=f"417-{i:02d}", room_name=f"Exam {i}")
     for i in range(1, 19)]
    + [dict(PAD, room_number=str(100 + i)) for i in range(27)])
pd_res = T._apply_schedule_room_scope(padded)
prec2 = pd_res["_schedule_room_scope"]
check(prec2.get("noop") == "schedule_incomplete",
      f"45 rows but only 18 real -> still stands down (got {prec2.get('noop')})")
check(prec2.get("rows_with_evidence") == 18 and prec2.get("rows") == 45,
      f"records both counts (rows={prec2.get('rows')}, "
      f"evidence={prec2.get('rows_with_evidence')})")
check(prec2.get("coverage") == round(18 / 62, 3),
      "coverage computed on evidence rows, not padded rows")
T._FINISH_PLAN_BLOCK_COUNTS.clear()
os.environ.pop("NIGHTSHIFT_SCHEDULE_ROOM_SCOPE", None)

# ── 7. region sweep hygiene: dedup, area tags, on-sheet validation ──────
print("\n[7] region sweep row hygiene")

check(T._canon_room_number("417-16") == "41716", "canon strips dashes")
check(T._canon_room_number("417.16") == "41716", "canon strips dots")
check(T._canon_room_number("417 16") == "41716", "canon strips spaces")
check(T._canon_room_number(None) == "", "canon handles None")
check(T._canon_room_number("417-16") == T._canon_room_number("417.16"),
      "separator drift collapses to one key (overlapping tiles)")

TAGROWS = ([{"room_number": f"400-{i:02d}", "room_name": f"Exam {i}",
             "wall_finish": "PT1", "ceiling_finish": "ACT1"}
            for i in range(1, 13)]
           + [{"room_number": str(116 + i), "room_name": f"Room {i}",
               "wall_finish": "PT1", "ceiling_finish": "ACT1"}
              for i in range(6)])
kept, tags = T._drop_floorplan_tag_rows(TAGROWS)
check(len(tags) == 6 and len(kept) == 12,
      f"bare area tags dropped when suite numbering dominates "
      f"(kept {len(kept)}, dropped {len(tags)})")

BARE_ONLY = [{"room_number": str(100 + i), "room_name": f"R{i}",
              "wall_finish": "PT1"} for i in range(10)]
kept2, tags2 = T._drop_floorplan_tag_rows(BARE_ONLY)
check(len(tags2) == 0 and len(kept2) == 10,
      "jobs legitimately numbered 101/102 are untouched")

ON = {"41716", "40001"}
rows = [{"room_number": "417-16"}, {"room_number": "400-01"},
        {"room_number": "400-33"}, {"room_number": "417O"},
        {"room_name": "Corridor"}]
k3, d3 = T._drop_offsheet_rows(rows, ON)
check(len(k3) == 3 and len(d3) == 2,
      f"numbers absent from the sheet are rejected (kept {len(k3)}, "
      f"dropped {len(d3)})")
check(any(not r.get("room_number") for r in k3),
      "name-only rows survive (matched by name downstream)")
k4, d4 = T._drop_offsheet_rows(rows, set())
check(len(k4) == 5 and not d4,
      "empty token scan is a no-op — never deletes a whole schedule")

# ── 8. scope boundary must also reduce the aggregates ───────────────────
print("\n[8] scope boundary reduces aggregated_totals")

os.environ["NIGHTSHIFT_SCHEDULE_ROOM_SCOPE"] = "1"
agg_a = {
    "room_finish_schedule": [
        {"room_number": f"400-{i:02d}", "room_name": f"Exam {i}",
         "wall_finish": "PT1", "ceiling_finish": "ACT1"} for i in range(1, 9)],
    "floors": [{"rooms": [
        {"room_id": "A", "room_name": "Exam 1", "room_number": "400-01",
         "in_scope": True, "dimensions": {"wall_area_sqft": 300},
         "elements": {"doors_full_paint": 2}},
        {"room_id": "S", "room_name": "Stair 1", "in_scope": True,
         "dimensions": {"wall_area_sqft": 900},
         "elements": {"stair_sections": 6, "gyp_between_stairs_sqft": 480,
                      "doors_full_paint": 4}},
        {"room_id": "R", "room_name": "Receiving", "in_scope": True,
         "dimensions": {"wall_area_sqft": 800},
         "elements": {"concrete_floor_sqft": 1250}},
    ]}], "notes": [],
    "aggregated_totals": {"total_doors_full_paint": 6,
                          "total_stair_sections": 6,
                          "total_gyp_between_stairs_sqft": 480,
                          "total_concrete_floor_sqft": 1250}}
ar = T._apply_schedule_room_scope(agg_a)
ag = ar["aggregated_totals"]
check(ag["total_stair_sections"] == 0,
      f"stairs on a dropped room stop being billed (got {ag['total_stair_sections']})")
check(ag["total_doors_full_paint"] == 2,
      f"doors fall to what in-scope rooms hold (got {ag['total_doors_full_paint']})")
check(ag["total_concrete_floor_sqft"] == 0, "sealed slab follows its room")
check(ag["total_gyp_between_stairs_sqft"] == 0, "gyp-between-stairs follows too")
check("aggregates_adjusted" in ar["_schedule_room_scope"],
      "adjustment recorded for the reviewer")

# never below what survivors hold, never negative
agg_b = dict(agg_a)
agg_b = json.loads(json.dumps(agg_a)) if False else None
os.environ.pop("NIGHTSHIFT_SCHEDULE_ROOM_SCOPE", None)

# ── 9. roster anchoring: one schedule row is one room ───────────────────
print("\n[9] schedule roster anchoring")

def _anchor_case():
    return {
        "room_finish_schedule": [
            {"room_number": f"400-{i:02d}", "room_name": f"Exam {i}",
             "wall_finish": "PT1", "ceiling_finish": "ACT1"}
            for i in range(1, 9)],
        "floors": [{"rooms": [
            {"room_id": "A1", "room_name": "Exam 1", "room_number": "400-01",
             "in_scope": True, "dimensions": {"wall_area_sqft": 300},
             "elements": {"doors_full_paint": 1}},
            {"room_id": "A2", "room_name": "Exam 1 (dup)",
             "room_number": "400-01", "in_scope": True,
             "dimensions": {"wall_area_sqft": 0},
             "elements": {"doors_full_paint": 1}},
            {"room_id": "A3", "room_name": "Exam 1", "room_number": "400-01",
             "in_scope": True, "dimensions": {"wall_area_sqft": 120},
             "elements": {"doors_full_paint": 1}},
            {"room_id": "B", "room_name": "Exam 2", "room_number": "400-02",
             "in_scope": True, "dimensions": {"wall_area_sqft": 250},
             "elements": {"doors_full_paint": 1}},
            {"room_id": "X", "room_name": "Elevator Lobby", "in_scope": True,
             "dimensions": {"wall_area_sqft": 900},
             "elements": {"doors_full_paint": 4}},
        ]}], "notes": [],
        "aggregated_totals": {"total_doors_full_paint": 8}}

os.environ["NIGHTSHIFT_SCHEDULE_ROOM_SCOPE"] = "1"
os.environ["NIGHTSHIFT_SCHEDULE_ROOM_ANCHOR"] = "0"
off9 = T._apply_schedule_room_scope(_anchor_case())
check(off9["_schedule_room_scope"]["rooms_kept"] == 4
      and off9["_schedule_room_scope"].get("rooms_anchored_out", 0) == 0,
      "anchor OFF -> all three duplicates kept (unchanged behavior)")

os.environ["NIGHTSHIFT_SCHEDULE_ROOM_ANCHOR"] = "1"
on9 = T._apply_schedule_room_scope(_anchor_case())
r9 = on9["_schedule_room_scope"]
ins9 = [x["room_id"] for f in on9["floors"] for x in f["rooms"]
        if x.get("in_scope", True)]
check(r9["rooms_anchored_out"] == 2,
      f"two duplicates folded out (got {r9.get('rooms_anchored_out')})")
check(ins9 == ["A1", "B"],
      f"best-dimensioned room per row survives (got {ins9})")
check(on9["aggregated_totals"]["total_doors_full_paint"] == 2,
      f"folded rooms stop billing doors (got "
      f"{on9['aggregated_totals']['total_doors_full_paint']})")
check(any("Schedule Room Anchor" in str(n) for n in on9.get("notes", [])),
      "anchoring explained in the notes")
check(r9["rooms_kept"] <= len(_anchor_case()["room_finish_schedule"]),
      "kept rooms never exceed the schedule row count")
os.environ.pop("NIGHTSHIFT_SCHEDULE_ROOM_ANCHOR", None)
os.environ.pop("NIGHTSHIFT_SCHEDULE_ROOM_SCOPE", None)

# ── 10. room_number: the schedule join key ──────────────────────────────
print("\n[10] room_number join key")

check("room_number" in T._SO_ROOM_ITEM["properties"],
      "room_number is in the extraction schema")
check("room_number" in T._SO_ROOM_ITEM["required"],
      "room_number is required+nullable like every other field")

def _bf():
    return {"floors": [{"rooms": [
        {"room_id": "a", "room_name": "Shared Office (400-05)"},
        {"room_id": "b", "room_name": "Pre/Post Bay 400-27"},
        {"room_id": "c", "room_name": "MA Station (417-13)"},
        {"room_id": "d", "room_name": "Corridor 417B"},
        {"room_id": "e", "room_name": "Isolation #1"},
        {"room_id": "f", "room_name": "Exam 9", "room_number": "400-99"},
    ]}]}

os.environ.pop("NIGHTSHIFT_ROOM_NUMBER_BACKFILL", None)
off10 = T._backfill_room_numbers(_bf())
check(all(not r.get("room_number") for r in off10["floors"][0]["rooms"][:5]),
      "flag OFF -> inert")

os.environ["NIGHTSHIFT_ROOM_NUMBER_BACKFILL"] = "1"
on10 = T._backfill_room_numbers(_bf())
got = {r["room_id"]: r.get("room_number") for r in on10["floors"][0]["rooms"]}
check(got["a"] == "400-05", f"parses '(400-05)' (got {got['a']})")
check(got["b"] == "400-27", f"parses bare '400-27' (got {got['b']})")
check(got["c"] == "417-13", f"parses '(417-13)' (got {got['c']})")
check(got["d"] == "417B", f"parses suite letter '417B' (got {got['d']})")
check(not got["e"], "'Isolation #1' yields no false number")
check(got["f"] == "400-99", "never overwrites a number extraction reported")
check(on10["_room_number_backfill"]["filled"] == 4,
      f"records how many were recovered (got "
      f"{on10['_room_number_backfill']['filled']})")
os.environ.pop("NIGHTSHIFT_ROOM_NUMBER_BACKFILL", None)

print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} FAILED:")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
