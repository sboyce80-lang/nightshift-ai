#!/usr/bin/env python3
"""Offline tests for Phase 2.2: per-sheet anchored extraction.

Covers the pure machinery — canonical room identity, merge-on-collision,
the deterministic union merge across sheets, verification-pass apply
semantics (additive-with-evidence), sheet checkpoints, text-layer anchor
matching, and the verification output schema's API constraints. No API
calls, no PDFs.

Run: python3 test_per_sheet_extraction.py
"""
import importlib.util as iu
import copy
import json
import os
import sys
import tempfile

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


def room(name="Corridor", rid="", floor_area=0, ceiling_area=0, wall_area=0,
         sheet="", mult=1, **extra):
    r = {
        "room_id": rid,
        "room_name": name,
        "source_sheet": sheet,
        "source_page": 1,
        "unit_multiplier": mult,
        "in_scope": True,
        "dimensions": {
            "length_feet": 0, "width_feet": 0, "ceiling_height_feet": 0,
            "floor_area_sqft": floor_area, "perimeter_lf": 0,
            "wall_area_sqft": wall_area, "ceiling_area_sqft": ceiling_area,
        },
        "materials": {"walls": "", "ceiling": "", "ceiling_painted": False,
                      "base": ""},
        "elements": {"doors_full_paint": 0, "base_trim_lf": 0},
        "notes": "",
    }
    r.update(extra)
    return r


def main():
    print("\n── Flag gating ──")
    for var in ("NIGHTSHIFT_PER_SHEET_EXTRACTION", "NIGHTSHIFT_SHEET_VERIFY",
                "NIGHTSHIFT_SHEET_CHECKPOINT"):
        os.environ.pop(var, None)
    check("per-sheet extraction defaults OFF",
          T._per_sheet_extraction_enabled() is False)
    os.environ["NIGHTSHIFT_PER_SHEET_EXTRACTION"] = "1"
    check("per-sheet extraction enables with env=1",
          T._per_sheet_extraction_enabled() is True)
    os.environ.pop("NIGHTSHIFT_PER_SHEET_EXTRACTION", None)
    check("verification defaults ON within per-sheet mode",
          T._sheet_verification_enabled() is True)
    os.environ["NIGHTSHIFT_SHEET_VERIFY"] = "0"
    check("verification disables with env=0",
          T._sheet_verification_enabled() is False)
    os.environ.pop("NIGHTSHIFT_SHEET_VERIFY", None)
    check("checkpointing defaults ON within per-sheet mode",
          T._sheet_checkpoint_enabled() is True)

    print("\n── Plan-sheet title classification ──")
    check("'first floor plan' is a plan sheet",
          T._title_text_is_plan_sheet("A-101 FIRST FLOOR PLAN scale 1/8"))
    check("'reflected ceiling' is a plan sheet",
          T._title_text_is_plan_sheet("REFLECTED CEILING PLAN LEVEL 2"))
    check("elevations-only sheet is not",
          not T._title_text_is_plan_sheet("EXTERIOR ELEVATIONS north south"))
    check("door schedule sheet is not",
          not T._title_text_is_plan_sheet("DOOR SCHEDULE and frame types"))
    check("bare 'plan' without non-plan content counts",
          T._title_text_is_plan_sheet("KEY PLAN building orientation"))
    check("empty text is not a plan sheet",
          not T._title_text_is_plan_sheet(""))

    print("\n── Section/elevation/detail exclusion (per-sheet over-extraction fix) ──")
    check("'Building Section A-300' excluded",
          T._title_text_is_section_or_detail("A-300 BUILDING SECTION scale 1/4"))
    check("'Exterior Elevations' excluded",
          T._title_text_is_section_or_detail("EXTERIOR ELEVATIONS north south"))
    check("'Wall Details' excluded",
          T._title_text_is_section_or_detail("A-500 WALL DETAILS typ"))
    check("'Stairwell Plans & Sections' KEPT (has 'plan')",
          not T._title_text_is_section_or_detail("STAIRWELL PLANS & SECTIONS A303"))
    check("'Enlarged Plan' KEPT (strong plan, even if a section is drawn)",
          not T._title_text_is_section_or_detail("ENLARGED PLAN & SECTION A-105"))
    check("'2nd Floor Plan' not a section",
          not T._title_text_is_section_or_detail("A-102 SECOND FLOOR PLAN"))
    check("empty title is not a section", not T._title_text_is_section_or_detail(""))

    print("\n── Enlarged-plan floor naming (broadened dedup match) ──")
    for nm in ["Typical Unit x01 (2BR/2BA Template)", "Typical Unit x05 Floor",
               "Unit Types x01-x06 (Typical Residential Unit Plans — A-105/A-106)",
               "Special Unit Plans — Units x01 through x06"]:
        check(f"enlarged match: {nm[:32]!r}",
              bool(T._ENLARGED_PLAN_FLOOR_RE.search(nm)))
    for nm in ["1st Floor", "2nd Floor", "3rd Floor (Sheet A103)",
               "Roof / Bulkhead Level"]:
        check(f"real floor NOT matched: {nm!r}",
              not T._ENLARGED_PLAN_FLOOR_RE.search(nm))

    print("\n── Residential ceiling floor skipped in per-sheet mode ──")
    an_ps = {"_per_sheet_extraction": True,
             "project_info": {"building_type": "residential",
                              "footprint_sqft": 10200, "total_stories": 5},
             "aggregated_totals": {"total_paintable_ceiling_sqft": 39412}}
    T._apply_residential_ceiling_floor(an_ps)
    check("per-sheet ceiling NOT boosted by the GSF compensator",
          an_ps["aggregated_totals"]["total_paintable_ceiling_sqft"] == 39412)
    check("skip is recorded + idempotency flag set",
          an_ps.get("_residential_ceiling_floor_applied") is True
          and any("per-sheet" in str(n) for n in an_ps.get("notes", [])))

    print("\n── Sheet-role classification (P2-B) ──")
    check("floor plan → geometry", T._sheet_role("A-102 SECOND FLOOR PLAN") == "geometry")
    check("dimension plan → geometry", T._sheet_role("DIMENSION PLAN A-103") == "geometry")
    check("enlarged plan → geometry (unit detail)",
          T._sheet_role("ENLARGED UNIT PLANS A-105") == "geometry")
    check("reflected ceiling plan → ceiling",
          T._sheet_role("REFLECTED CEILING PLAN LEVEL 2") == "ceiling")
    check("RCP → ceiling", T._sheet_role("A-201 RCP") == "ceiling")
    check("finish plan → finish", T._sheet_role("FIRST FLOOR FINISH PLAN") == "finish")
    check("empty → geometry (safe default)", T._sheet_role("") == "geometry")

    print("\n── Role-aware merge: secondary sheets add attributes, not geometry ──")
    # Geometry sheet (floor plan) establishes the Corridor with walls;
    # RCP sheet has the same Corridor (ceiling only) + a NEW room not on the plan.
    geo = {"page_idx0": 1, "sheet_id": "A-102", "role": "geometry",
           "analysis": {"floors": [{"floor_name": "2nd Floor", "rooms": [
               room("Corridor", floor_area=800, wall_area=1200, sheet="A-102")]}]}}
    rcp_corr = room("Corridor", ceiling_area=780, sheet="A-201")
    rcp_corr["materials"]["ceiling_painted"] = True
    rcp = {"page_idx0": 9, "sheet_id": "A-201", "role": "ceiling",
           "analysis": {"floors": [{"floor_name": "2nd Floor", "rooms": [
               rcp_corr,
               room("Plenum Void", ceiling_area=300, sheet="A-201")]}]}}
    merged = T._merge_sheet_analyses([geo, rcp])
    rooms = merged["floors"][0]["rooms"]
    check("RCP did NOT add its non-matching 'Plenum Void' as new geometry",
          len(rooms) == 1, f"got {len(rooms)} rooms")
    corr = rooms[0]
    check("matching RCP ceiling DID merge onto the floor-plan corridor",
          corr["dimensions"]["ceiling_area_sqft"] == 780
          and corr["dimensions"]["wall_area_sqft"] == 1200)
    check("dropped secondary geometry counted", merged.get("_secondary_geometry_dropped") == 1)
    # Fallback: if ALL sheets are secondary (no geometry), don't drop everything
    only_rcp = {"page_idx0": 9, "sheet_id": "A-201", "role": "ceiling",
                "analysis": {"floors": [{"floor_name": "2nd Floor", "rooms": [
                    room("Corridor", ceiling_area=780, sheet="A-201")]}]}}
    m2 = T._merge_sheet_analyses([only_rcp])
    check("all-secondary set falls back to keeping rooms (nothing lost)",
          m2["project_info"]["total_rooms_found"] == 1)

    print("\n── Geometry bucketing ──")
    check("re-read noise lands in the same bucket (800 vs 840 SF)",
          T._geom_bucket(room(floor_area=800)) ==
          T._geom_bucket(room(floor_area=840)))
    check("closet vs corridor split buckets (50 vs 800 SF)",
          T._geom_bucket(room(floor_area=50)) !=
          T._geom_bucket(room(floor_area=800)))
    check("floor-plan and RCP instances collide (floor 800 / ceiling 780)",
          T._geom_bucket(room(floor_area=800)) ==
          T._geom_bucket(room(ceiling_area=780)))
    check("zero-dim room buckets to empty string",
          T._geom_bucket(room()) == "")
    check("wall-only geometry uses its own namespace",
          T._geom_bucket(room(wall_area=800)).startswith("w"))

    print("\n── Canonical room identity ──")
    a = room("Corridor", floor_area=800, sheet="A-102")
    b = room("Corridor", floor_area=820, sheet="A1.02")
    check("sheet ID is provenance, NOT identity (A-102 vs A1.02 collide)",
          T._canonical_room_key("2nd Floor", a) ==
          T._canonical_room_key("2nd Floor", b))
    check("ordinal floor names normalize ('2nd Floor' == 'Second Floor')",
          T._canonical_room_key("2nd Floor", a) ==
          T._canonical_room_key("Second Floor", b))
    u1 = room("Bedroom 1", rid="F2-APT201-BED1", floor_area=140)
    u2 = room("Bedroom 1", rid="F2-APT201-BED1", floor_area=210)
    check("numbered/unit rooms ignore geometry (re-read can't split them)",
          T._canonical_room_key("2nd Floor", u1) ==
          T._canonical_room_key("2nd Floor", u2))
    s1 = room("Storage", floor_area=60)
    s2 = room("Storage", floor_area=900)
    check("two generic 'Storage' rooms with different geometry stay distinct",
          T._canonical_room_key("1st Floor", s1) !=
          T._canonical_room_key("1st Floor", s2))
    fp = room("Corridor", floor_area=800, sheet="A-101")
    rcp = room("Corridor", ceiling_area=780, sheet="A-201")
    check("floor-plan + RCP instances collide by construction",
          T._canonical_room_key("1st Floor", fp) ==
          T._canonical_room_key("1st Floor", rcp))
    check("different floors never collide",
          T._canonical_room_key("1st Floor", a) !=
          T._canonical_room_key("2nd Floor", a))

    print("\n── Merge on collision ──")
    keeper = room("Corridor", floor_area=800, wall_area=1200, sheet="A-101")
    rcp_inst = room("Corridor", ceiling_area=780, sheet="A-201")
    rcp_inst["materials"]["ceiling_painted"] = True
    merged, log = T._merge_rooms_on_collision(keeper, rcp_inst)
    check("ceiling area backfilled from RCP instance (review 4.6 fix)",
          merged["dimensions"]["ceiling_area_sqft"] == 780)
    check("ceiling_painted OR'd from RCP instance",
          merged["materials"]["ceiling_painted"] is True)
    check("keeper's non-zero wall area not overwritten",
          merged["dimensions"]["wall_area_sqft"] == 1200)
    check("merge log records the backfill", len(log) >= 2)
    check("source sheets unioned as provenance",
          merged["_source_sheets"] == ["A-101", "A-201"])

    k2 = room("Bedroom", mult=1, sheet="A-101")
    o2 = room("Bedroom", mult=3, sheet="A-102")
    m2, _ = T._merge_rooms_on_collision(k2, o2)
    check("unit_multiplier conflict reconciles to max",
          m2["unit_multiplier"] == 3)
    check("multiplier conflict leaves an audit note",
          "merge audit" in m2.get("notes", ""))

    e1 = room("Lobby", sheet="A-101")
    e1["elements"] = {"doors_full_paint": 2, "base_trim_lf": 40}
    e2 = room("Lobby", sheet="A-102")
    e2["elements"] = {"doors_full_paint": 5, "base_trim_lf": 10}
    m3, _ = T._merge_rooms_on_collision(e1, e2)
    check("elements merge per-field max",
          m3["elements"]["doors_full_paint"] == 5
          and m3["elements"]["base_trim_lf"] == 40)
    check("merge does not mutate its inputs",
          e1["elements"]["doors_full_paint"] == 2)

    print("\n── Union merge across sheets ──")
    sheet1 = {
        "page_idx0": 4, "sheet_id": "A-101",
        "analysis": {
            "project_info": {"building_type": "residential",
                             "total_units": 12, "footprint_sqft": 9000},
            "floors": [{"floor_name": "1st Floor", "rooms": [
                room("Lobby", floor_area=400, sheet="A-101"),
                room("Corridor", floor_area=800, wall_area=1200,
                     sheet="A-101"),
            ]}],
            "notes": ["scale 1/8 in = 1 ft"],
            "has_door_schedule": False,
        },
    }
    rcp_room = room("Corridor", ceiling_area=780, sheet="A-201")
    rcp_room["materials"]["ceiling_painted"] = True
    sheet2 = {
        "page_idx0": 9, "sheet_id": "A-201",
        "analysis": {
            "project_info": {"building_type": "residential"},
            "floors": [{"floor_name": "First Floor", "rooms": [rcp_room]}],
            "notes": ["scale 1/8 in = 1 ft"],
            "has_door_schedule": True,
        },
    }
    merged = T._merge_sheet_analyses([sheet1, sheet2])
    check("floor names union via ordinal normalizer (one '1st Floor')",
          len(merged["floors"]) == 1)
    rooms = merged["floors"][0]["rooms"]
    check("RCP corridor merged into floor-plan corridor (2 rooms, not 3)",
          len(rooms) == 2, f"got {len(rooms)}")
    corr = [r for r in rooms if r["room_name"] == "Corridor"][0]
    check("merged corridor has walls from plan + ceiling from RCP",
          corr["dimensions"]["wall_area_sqft"] == 1200
          and corr["dimensions"]["ceiling_area_sqft"] == 780)
    check("canonical merge logged",
          len(merged.get("_canonical_merge_log") or []) == 1)
    check("room count reflects the union",
          merged["project_info"]["total_rooms_found"] == 2)
    check("has_door_schedule OR'd across sheets",
          merged["has_door_schedule"] is True)
    check("project_info maxima carried (units, footprint)",
          merged["project_info"]["total_units"] == 12
          and merged["project_info"]["footprint_sqft"] == 9000)
    check("notes tagged by sheet and deduped",
          merged["notes"] == ["[A-101] scale 1/8 in = 1 ft",
                              "[A-201] scale 1/8 in = 1 ft"])
    check("per-sheet marker set", merged.get("_per_sheet_extraction") is True)
    merged_again = T._merge_sheet_analyses(copy.deepcopy([sheet1, sheet2]))
    check("union merge is deterministic (same input → identical output)",
          json.dumps(merged, sort_keys=True, default=list) ==
          json.dumps(merged_again, sort_keys=True, default=list))

    print("\n── Verification apply (additive-with-evidence) ──")
    sheet_an = {
        "floors": [{"floor_name": "1st Floor", "rooms": [
            room("Lobby", floor_area=400, rid="F1-LOBBY", sheet="A-101"),
            room("Corridor", floor_area=800, rid="F1-CORR", sheet="A-101"),
        ]}],
    }
    verification = {
        "missing_rooms": [
            room("Storage", floor_area=80, rid="F1-STOR"),
            room("Lobby", floor_area=400, rid="F1-LOBBY"),  # echo — no-op
        ],
        "unanchored_rooms": [
            {"room_id": "F1-CORR", "room_name": "Corridor",
             "reason": "no label visible at that location"},
        ],
    }
    n_added, n_flagged = T._apply_sheet_verification(
        sheet_an, verification, "A-101", 5)
    check("missed room added", n_added == 1, f"added={n_added}")
    rooms = sheet_an["floors"][0]["rooms"]
    check("echoed existing room deduped by canonical key (no duplicate Lobby)",
          sum(1 for r in rooms if r["room_name"] == "Lobby") == 1)
    added = [r for r in rooms if r["room_name"] == "Storage"][0]
    check("added room stamped _added_by_verification + provenance",
          added.get("_added_by_verification") is True
          and added["source_sheet"] == "A-101" and added["source_page"] == 5)
    check("unanchored room flagged, NOT deleted", n_flagged == 1
          and any(r.get("_no_anchor") for r in rooms
                  if r["room_name"] == "Corridor"))
    corr = [r for r in rooms if r["room_name"] == "Corridor"][0]
    check("flagged room keeps its dimensions (no zeroing, no deletion)",
          corr["dimensions"]["floor_area_sqft"] == 800)
    empty_an = {"floors": []}
    n_added, _ = T._apply_sheet_verification(
        empty_an, {"missing_rooms": [room("Office", floor_area=150)]},
        "A-102", 7)
    check("missing room on floorless sheet creates a sheet-named floor",
          n_added == 1 and empty_an["floors"][0]["floor_name"] == "Sheet A-102")

    print("\n── Sheet checkpoints ──")
    with tempfile.TemporaryDirectory() as td:
        key = T._sheet_checkpoint_key("PROMPT", "CTX", True)
        check("checkpoint key is stable",
              key == T._sheet_checkpoint_key("PROMPT", "CTX", True))
        check("checkpoint key varies with prompt/context/verify flag",
              len({key, T._sheet_checkpoint_key("PROMPT2", "CTX", True),
                   T._sheet_checkpoint_key("PROMPT", "CTX2", True),
                   T._sheet_checkpoint_key("PROMPT", "CTX", False)}) == 4)
        analysis = {"floors": [{"floor_name": "1st Floor", "rooms": []}]}
        T._sheet_checkpoint_save(td, 4, key, "A-101", analysis)
        loaded = T._sheet_checkpoint_load(td, 4, key)
        check("checkpoint roundtrip", loaded == analysis)
        check("miss on different key",
              T._sheet_checkpoint_load(
                  td, 4, T._sheet_checkpoint_key("OTHER", "CTX", True)) is None)
        check("miss on different page",
              T._sheet_checkpoint_load(td, 5, key) is None)
        p = os.path.join(td, f"p0004_{key}.json")
        with open(p) as f:
            payload = json.load(f)
        payload["version"] = -1
        with open(p, "w") as f:
            json.dump(payload, f)
        check("version mismatch invalidates checkpoint",
              T._sheet_checkpoint_load(td, 4, key) is None)
        with open(p, "w") as f:
            f.write("{corrupt")
        check("corrupt checkpoint returns None (non-fatal)",
              T._sheet_checkpoint_load(td, 4, key) is None)
    check("no checkpoint dir is a no-op",
          T._sheet_checkpoint_load(None, 0, "x") is None)

    print("\n── Text-layer anchor matching ──")
    parsed = {
        "room_ids": [{"id": "V-101", "bbox": [10, 10, 50, 20]}],
        "room_labels": [{"label": "CORRIDOR", "bbox": [100, 100, 160, 112]}],
    }
    check("room_id matches across separator conventions (V101 vs V-101)",
          (T._match_room_anchor(room("Vest", rid="V101"), parsed) or {})
          .get("kind") == "room_id")
    check("label containment matches ('Corridor 1' vs CORRIDOR)",
          (T._match_room_anchor(room("Corridor 1"), parsed) or {})
          .get("kind") == "label")
    check("unrelated room gets no anchor",
          T._match_room_anchor(room("Penthouse Spa"), parsed) is None)
    check("no parsed text layer → no anchor",
          T._match_room_anchor(room("Corridor"), None) is None)

    print("\n── Provenance stamping ──")
    an = {"floors": [{"floor_name": "1st Floor", "rooms": [
        room("Corridor", sheet="A-1.02"),
        room("Lobby", sheet="B9"),
    ]}]}
    anchored, total = T._stamp_sheet_provenance(an, "A-102", 6, parsed)
    rooms = an["floors"][0]["rooms"]
    check("all rooms stamped with the detected sheet + page",
          all(r["source_sheet"] == "A-102" and r["source_page"] == 6
              for r in rooms) and total == 2)
    check("equivalent LLM sheet ('A-1.02') not stashed as a mismatch",
          "_source_sheet_llm" not in rooms[0])
    check("mismatched LLM sheet stashed for audit",
          rooms[1].get("_source_sheet_llm") == "B9")
    check("anchorable room got its anchor", anchored == 1
          and rooms[0].get("_anchor", {}).get("kind") == "label")

    print("\n── Verification schema API constraints ──")

    def walk(schema, out):
        if not isinstance(schema, dict):
            return
        if schema.get("properties") is not None:
            out.append(schema)
            for v in schema["properties"].values():
                walk(v, out)
        if isinstance(schema.get("items"), dict):
            walk(schema["items"], out)

    objs = []
    walk(T._VERIFICATION_OUTPUT_SCHEMA, objs)
    check("every object enumerates keys with additionalProperties: false",
          all(o.get("additionalProperties") is False for o in objs))
    check("every key is required (API caps optional params at ~24)",
          all(sorted(o.get("required", [])) == sorted(o["properties"].keys())
              for o in objs))
    n_props = sum(len(o["properties"]) for o in objs)
    check("total properties under the ~70 grammar cap",
          n_props <= 70, f"{n_props} properties")
    check("verification reuses the extraction room item verbatim",
          T._VERIFICATION_OUTPUT_SCHEMA["properties"]["missing_rooms"]["items"]
          is T._SO_ROOM_ITEM)
    check("extraction schema's room item is the same shared object",
          T._EXTRACTION_OUTPUT_SCHEMA["properties"]["floors"]["items"]
          ["properties"]["rooms"]["items"] is T._SO_ROOM_ITEM)
    kwargs = T._verification_output_kwargs()
    check("verification kwargs carry the schema when enabled",
          kwargs.get("output_config", {}).get("format", {}).get("schema")
          is T._VERIFICATION_OUTPUT_SCHEMA)
    T._STRUCTURED_OUTPUTS_BROKEN = True
    check("kill switch also disables verification schema",
          T._verification_output_kwargs() == {})
    T._STRUCTURED_OUTPUTS_BROKEN = False

    print("\n── Schedule-read consensus merge (determinism) ──")
    # Two reads of the same door schedule that each miss a different row:
    # read A has D1,D2,D3 (sees 3); read B has D1,D2,D4 (sees 3). The union
    # is D1-D4 (the real 4), recovering the row each read dropped.
    readA = {"door_schedule": {"total_doors_full_paint": 3,
                               "total_doors_hm_panel": 0,
                               "door_marks_counted": [{"mark": "D1"}, {"mark": "D2"},
                                                      {"mark": "D3"}]},
             "window_schedule": {"total_windows": 5, "window_types": [{"mark": "W1"}]},
             "stair_info": {"total_stair_sections": 6}}
    readB = {"door_schedule": {"total_doors_full_paint": 3,
                               "total_doors_hm_panel": 1,
                               "door_marks_counted": [{"mark": "D1"}, {"mark": "D2"},
                                                      {"mark": "D4"}]},
             "window_schedule": {"total_windows": 4, "window_types": [{"mark": "W2"}]},
             "stair_info": {"total_stair_sections": 8}}
    merged = T._merge_schedule_reads([readA, readB])
    ds = merged["door_schedule"]
    check("door marks unioned across reads (D1-D4 recovered)",
          {m["mark"] for m in ds["door_marks_counted"]} == {"D1", "D2", "D3", "D4"})
    check("door totals take the max across reads (HM 0 vs 1 → 1)",
          ds["total_doors_hm_panel"] == 1 and ds["total_doors_full_paint"] == 3)
    check("window types unioned (W1 + W2)",
          {w["mark"] for w in merged["window_schedule"]["window_types"]} == {"W1", "W2"})
    check("stair sections take the max (6 vs 8 → 8)",
          merged["stair_info"]["total_stair_sections"] == 8)
    check("single read passes through unchanged",
          T._merge_schedule_reads([readA]) is readA)
    check("empty reads → None", T._merge_schedule_reads([]) is None)
    # Determinism: order must not change the merged result
    m1 = T._merge_schedule_reads([readA, readB])
    m2 = T._merge_schedule_reads([readB, readA])
    check("merge is order-independent on counts",
          m1["door_schedule"]["total_doors_hm_panel"]
          == m2["door_schedule"]["total_doors_hm_panel"]
          and {m["mark"] for m in m1["door_schedule"]["door_marks_counted"]}
          == {m["mark"] for m in m2["door_schedule"]["door_marks_counted"]})

    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
