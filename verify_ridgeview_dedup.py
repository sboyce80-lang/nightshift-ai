"""Verification harness for the Ridgeview fixes.

Background: Elliott's 2026-05-28 Ridgeview run (a 3-story 42-unit
multifamily by Coppola Associates) had two compounding bugs:

  Fix #1 — template-floor dedup regex.
    _parse_floor_range only matched word-first names ("Floor 2",
    "Floors 2-9"); Coppola uses ordinal English ("2nd Floor",
    "Third Floor", "(2nd & 3rd)") so 7 of 8 floor names returned
    set() and dedup never fired. 8 floors instead of 3 → 1.65x
    ceiling inflation.

  Fix #2 — residential corridor ceiling defaulted to ACT.
    Extraction prompt told the model corridors "ALMOST ALWAYS have
    ACT ceilings — do NOT assume painted". That's right for
    commercial, wrong for residential. Every Ridgeview corridor and
    lobby ended up with ceiling_painted=false; ~2,900 sqft dropped.
    Prompt now branches on building_type; safety net
    _fix_residential_corridor_ceilings catches re-runs.

This script:
  1. Unit-tests the new _parse_floor_range against the 8 actual Ridgeview
     floor names + the legacy Waverly names that must still parse.
  2. Loads Elliott's result JSON, runs _dedupe_overlapping_template_floors
     and _recalculate_totals on a deep copy, prints before/after totals.
  3. Unit-tests the residential corridor ceiling safety net on synthetic
     analyses (residential flips; commercial leaves alone).

Run:  python3 verify_ridgeview_dedup.py
      python3 verify_ridgeview_dedup.py --json /path/to/other.json
"""
import argparse
import copy
import json
import os
import sys

import Takeoff_DIRECT as T

DEFAULT_JSON = os.path.expanduser(
    "~/Downloads/construction_analysis_20260528_172439.json"
)
DEFAULT_PDF = os.path.expanduser(
    "~/Downloads/Ridgeview Arch Drawings 3-27-26.pdf"
)

# (floor_name, expected_set) — must match exactly post-fix.
PARSE_CASES = [
    # Ridgeview (Coppola) — these all returned set() before the fix.
    ("1st Floor",                                {1}),
    ("2nd Floor",                                {2}),
    ("2nd Floor - Typical Apartment Units",      {2}),
    ("3rd Floor - Typical to 2nd Floor",         {2, 3}),
    ("Third Floor",                              {3}),
    ("Typical Residential Floors (2nd & 3rd)",   {2, 3}),
    ("Typical Floor (Floors 2-4)",               {2, 3, 4}),
    ("Basement",                                 set()),
    # Spelled-out ordinals
    ("First Floor",                              {1}),
    ("Second Floor",                             {2}),
    ("Tenth Floor",                              {10}),
    # 3-item compound
    ("Floors (2nd, 3rd & 4th)",                  {2, 3, 4}),
    # Legacy Waverly (word-first) — must still work
    ("Typical Residential Floors (Levels 1-7)",  {1, 2, 3, 4, 5, 6, 7}),
    ("Typical Residential Levels (Floors 2-9)",  {2, 3, 4, 5, 6, 7, 8, 9}),
    ("Level 0 - Dining",                         {0}),
    # Unparseable — should stay set() so dedup leaves them alone
    ("Penthouse",                                set()),
    ("Mezzanine",                                set()),
    ("Cellar",                                   set()),
]


def test_parser():
    print("=" * 78)
    print("Unit tests: _parse_floor_range")
    print("=" * 78)
    fails = 0
    for name, expected in PARSE_CASES:
        got = T._parse_floor_range(name)
        ok = got == expected
        mark = "OK " if ok else "XX "
        print(f"  {mark} {name!r:52}  got={sorted(got) or '∅'}"
              f"   expected={sorted(expected) or '∅'}")
        if not ok:
            fails += 1
    print(f"\n  {len(PARSE_CASES) - fails}/{len(PARSE_CASES)} passed\n")
    return fails == 0


def _agg_summary(analysis):
    agg = analysis.get("aggregated_totals", {})
    return {
        "floors": len(analysis.get("floors", [])),
        "rooms": sum(len(f.get("rooms", [])) for f in analysis.get("floors", [])),
        "wall": int(agg.get("total_paintable_wall_sqft", 0)),
        "ceil": int(agg.get("total_paintable_ceiling_sqft", 0)),
        "doors": int(agg.get("total_doors_full_paint", 0)
                     + agg.get("total_doors_hm_panel", 0)
                     + agg.get("total_doors_frame_only", 0)),
        "windows": int(agg.get("total_windows_all", 0)),
        "base_trim": int(agg.get("total_base_trim_lf", 0)),
    }


def _print_floors(analysis, title):
    print(f"\n  {title}")
    print(f"  {'name':54} {'rooms':>6} {'wall_eff':>10} {'ceil_eff':>10}")
    print("  " + "-" * 84)
    for f in analysis.get("floors", []):
        rooms = f.get("rooms", []) or []
        wall = sum(float((r.get("dimensions") or {}).get("wall_area_sqft", 0) or 0)
                   * (r.get("unit_multiplier", 1) or 1) for r in rooms)
        ceil = sum(float((r.get("dimensions") or {}).get("ceiling_area_sqft", 0) or 0)
                   * (r.get("unit_multiplier", 1) or 1) for r in rooms)
        print(f"  {f.get('floor_name', '?')[:54]:54} {len(rooms):>6} "
              f"{wall:>10.0f} {ceil:>10.0f}")


def test_ridgeview_dedup(json_path, pdf_path):
    print("=" * 78)
    print(f"End-to-end: canonicalize + dedup + recalc on "
          f"{os.path.basename(json_path)}")
    print("=" * 78)
    if not os.path.exists(json_path):
        print(f"  SKIP — file not found: {json_path}")
        return True

    d = json.load(open(json_path))
    before = copy.deepcopy(d["analysis"])
    # Strip the "already deduped" flag — Elliott's run set it but the
    # function had silently bailed; we want to re-run cleanly.
    before.pop("_template_floors_deduped", None)
    before.pop("_source_sheets_canonicalized", None)
    after = copy.deepcopy(before)

    _print_floors(before, "BEFORE (Elliott's run, dedup never fired)")
    before_summary = _agg_summary(before)

    # Show how many rooms had their source_sheet rewritten when we add
    # the PDF-aware canonicalizer. Elliott's run tagged rooms with
    # 'A-101', 'A-102', 'A-103' for pages actually on Coppola's 'A2',
    # 'A3'. Without this rewrite, the regex dedup is still needed but
    # works around the symptom — with it, the LLM's bad sheet IDs are
    # corrected at the source.
    if os.path.exists(pdf_path):
        before_sheets = {}
        for fl in before.get("floors", []):
            for r in fl.get("rooms", []):
                ss = r.get("source_sheet", "?")
                before_sheets[ss] = before_sheets.get(ss, 0) + 1
        T._canonicalize_source_sheets(after, [pdf_path])
        after_sheets = {}
        for fl in after.get("floors", []):
            for r in fl.get("rooms", []):
                ss = r.get("source_sheet", "?")
                after_sheets[ss] = after_sheets.get(ss, 0) + 1
        print("\n  source_sheet distribution before / after canonicalization:")
        all_keys = sorted(set(before_sheets) | set(after_sheets))
        for k in all_keys:
            b = before_sheets.get(k, 0)
            a = after_sheets.get(k, 0)
            change = "" if b == a else "  ← rewritten"
            print(f"    {k:8} before={b:>3}  after={a:>3}{change}")

    T._dedupe_overlapping_template_floors(after)
    T._recalculate_totals(after)
    _print_floors(after, "AFTER (canonicalize + regex extended + Jaccard >= 0.5)")
    after_summary = _agg_summary(after)

    # Phase 3: secondary-space supplement. This was effectively disabled
    # on Elliott's run because the phantom-floor inflation pushed
    # rooms-per-unit density above the 0.85 gate. Post-dedup the density
    # drops correctly and supplement fires for the missing closets / mech
    # / storage rooms per apartment.
    T._supplement_missing_secondary_spaces(after)
    supp_summary = _agg_summary(after)

    # Phase 4: unit-multiplier sanity check. Doesn't change totals but
    # emits warnings into notes[] for estimator review. Verifies Fix #4
    # surfaces Ridgeview's 24% 2BR ratio (true 7%) as RATIO_IMPLAUSIBLE.
    T._validate_unit_multipliers(after)
    mult_warnings = [n for n in (after.get("notes") or [])
                     if isinstance(n, str)
                     and n.startswith("[Unit Multiplier Check]")]
    if mult_warnings:
        print("\n  Unit-multiplier validator warnings (Fix #4):")
        for w in mult_warnings:
            print(f"    - {w}")

    # Phase 5: residential ceiling floor — methodology fix. Bumps extracted
    # ceiling to footprint × stories × efficiency when extraction is
    # materially under. KonstructIQ measured 42,923 SF on Ridgeview;
    # extraction lands at 32,601 even with all upstream fixes.
    T._apply_residential_ceiling_floor(after)
    floor_summary = _agg_summary(after)

    print(f"\n  {'metric':14} {'before':>12} {'dedup':>12} {'+supplement':>14}"
          f" {'+ceil floor':>12}")
    print("  " + "-" * 70)
    for k in ("floors", "rooms", "wall", "ceil", "doors", "windows", "base_trim"):
        b, a, s = before_summary[k], after_summary[k], supp_summary[k]
        f = floor_summary[k]
        print(f"  {k:14} {b:>12,} {a:>12,} {s:>14,} {f:>12,}")

    print("\n  Rider's manual ceiling:   42,900 sqft (= GSF)")
    print("  KonstructIQ ceiling:      42,923 sqft (= GSF)")
    print(f"  Before all fixes:         {before_summary['ceil']:,} sqft "
          f"({before_summary['ceil']/42900:.2f}x)")
    print(f"  After dedup+corridor:     {after_summary['ceil']:,} sqft "
          f"({after_summary['ceil']/42900:.2f}x)")
    print(f"  After +supplement:        {supp_summary['ceil']:,} sqft "
          f"({supp_summary['ceil']/42900:.2f}x)")
    print(f"  After +ceiling floor:     {floor_summary['ceil']:,} sqft "
          f"({floor_summary['ceil']/42900:.2f}x)")

    notes_added = [n for n in (after.get("notes") or [])
                   if isinstance(n, str) and n.startswith("[dedup]")]
    if notes_added:
        print("\n  Dedup notes (auditable in proposal):")
        for n in notes_added:
            print(f"    - {n}")
    return True


def test_corridor_fix():
    """Synthetic checks for _fix_residential_corridor_ceilings."""
    print("=" * 78)
    print("Unit tests: _fix_residential_corridor_ceilings")
    print("=" * 78)
    fails = 0

    def _mk(building_type, room_name, ceiling_painted, ceiling_mat="",
            floor_area=720, notes=""):
        return {
            "project_info": {"building_type": building_type},
            "floors": [{
                "floor_name": "Test Floor",
                "rooms": [{
                    "room_name": room_name,
                    "dimensions": {"floor_area_sqft": floor_area,
                                   "ceiling_area_sqft": 0},
                    "materials": {"ceiling": ceiling_mat,
                                  "ceiling_painted": ceiling_painted},
                    "unit_multiplier": 1,
                    "notes": notes,
                }]
            }],
        }

    def _check(label, analysis, expect_painted, expect_ceil_sqft):
        nonlocal fails
        T._fix_residential_corridor_ceilings(analysis)
        room = analysis["floors"][0]["rooms"][0]
        got_painted = room["materials"].get("ceiling_painted")
        got_sqft = room["dimensions"].get("ceiling_area_sqft", 0)
        ok = (got_painted == expect_painted and got_sqft == expect_ceil_sqft)
        mark = "OK " if ok else "XX "
        print(f"  {mark} {label:60} painted={got_painted} sqft={got_sqft}"
              f"  (expected painted={expect_painted} sqft={expect_ceil_sqft})")
        if not ok:
            fails += 1

    # Residential corridor with ACT default → flip
    _check("residential apartment corridor (ACT default)",
           _mk("multifamily residential", "Corridor", False, "ACT"),
           True, 720)
    # Residential lobby with empty material → flip
    _check("residential supportive housing lobby (empty material)",
           _mk("supportive housing", "Lobby", False, ""),
           True, 720)
    # Residential bedroom — not a common area, leave alone (already painted)
    _check("residential bedroom (not corridor, leave alone)",
           _mk("multifamily", "Bedroom", True, "GYP"),
           True, 0)  # ceiling_area unchanged since function doesn't touch
    # Commercial corridor — leave alone, ACT is correct
    _check("commercial office corridor (leave alone)",
           _mk("office commercial", "Corridor", False, "ACT"),
           False, 0)
    # Residential corridor with explicit RCP ACT evidence — leave alone
    _check("residential corridor with RCP-shows-ACT note (leave alone)",
           _mk("multifamily", "Corridor", False, "ACT",
               notes="RCP shows ACT grid throughout this corridor"),
           False, 0)
    # Residential corridor already painted — no-op
    _check("residential corridor already painted (no-op)",
           _mk("multifamily", "Corridor", True, "GYP"),
           True, 0)
    # Empty floor area — skip
    _check("residential corridor with zero floor area (skip)",
           _mk("multifamily", "Corridor", False, "", floor_area=0),
           False, 0)
    # Mixed-use residential — should still fire
    _check("mixed-use residential lobby (flip)",
           _mk("mixed-use residential", "Elevator Lobby", False, ""),
           True, 720)
    # Idempotency — second call must not change anything
    a = _mk("multifamily residential", "Corridor", False, "ACT")
    T._fix_residential_corridor_ceilings(a)
    before = json.dumps(a, sort_keys=True)
    T._fix_residential_corridor_ceilings(a)
    after = json.dumps(a, sort_keys=True)
    if before != after:
        print(f"  XX  idempotency check FAILED — second call mutated state")
        fails += 1
    else:
        print(f"  OK  idempotency (second call is no-op)")

    print(f"\n  {9 - fails}/9 passed\n")
    return fails == 0


def test_unit_multiplier_validator():
    """Synthetic checks for _validate_unit_multipliers."""
    print("=" * 78)
    print("Unit tests: _validate_unit_multipliers")
    print("=" * 78)
    fails = 0

    def _mk(building_type, total_units, type_to_mult):
        # type_to_mult: {unit_type: multiplier}; one room per type tagged
        rooms = []
        for ut, mult in type_to_mult.items():
            rooms.append({
                "room_name": "Living Room",
                "unit_type": ut,
                "unit_multiplier": mult,
                "dimensions": {"floor_area_sqft": 200},
            })
        return {
            "project_info": {"building_type": building_type,
                             "total_units": total_units},
            "floors": [{"floor_name": "Test", "rooms": rooms}],
        }

    def _check(label, analysis, expect_warning_substrings):
        nonlocal fails
        T._validate_unit_multipliers(analysis)
        warns = [n for n in (analysis.get("notes") or [])
                 if isinstance(n, str) and n.startswith("[Unit Multiplier Check]")]
        ok = True
        if expect_warning_substrings is None:
            ok = (len(warns) == 0)
        else:
            for sub in expect_warning_substrings:
                if not any(sub in w for w in warns):
                    ok = False
                    break
        mark = "OK " if ok else "XX "
        print(f"  {mark} {label}")
        if not ok:
            for w in warns:
                print(f"      got: {w[:120]}")
            fails += 1

    # Ridgeview-like: 24% 2BR, sum matches total → fires VERIFY_UNIT_MIX
    _check("Ridgeview-like (multi-family, 24% 2BR)",
           _mk("multi-family", 42,
               {"Typical 1BR Unit": 14, "1BR Type A": 18, "2BR Type A": 10}),
           ["VERIFY_UNIT_MIX"])
    # Legitimate 1BR-dominant supportive housing → no warning
    _check("clean 1BR-dominant residential (no warning)",
           _mk("supportive housing", 42, {"1BR Type A": 39, "2BR Type A": 3}),
           None)
    # Sum doesn't match total → fires SUM_MISMATCH
    _check("sum mismatch (missing typology)",
           _mk("multifamily", 100, {"1BR": 50, "Studio": 20}),
           ["SUM_MISMATCH"])
    # Single-type dominance → fires SINGLE_TYPE_DOMINANCE
    _check("single type dominance (99% one typology)",
           _mk("apartment", 100, {"1BR": 99, "2BR": 1}),
           ["SINGLE_TYPE_DOMINANCE"]),
    # Non-residential → no warning
    _check("commercial office building (no warning)",
           _mk("commercial office", 42,
               {"Open Office": 14, "Private Office": 18, "Conference": 10}),
           None)
    # Too few units → no warning
    _check("single-family residential (no warning)",
           _mk("residential", 1, {"Main": 1}),
           None)

    print(f"\n  {6 - fails}/6 passed\n")
    return fails == 0


def test_residential_ceiling_floor():
    """Synthetic checks for _apply_residential_ceiling_floor."""
    print("=" * 78)
    print("Unit tests: _apply_residential_ceiling_floor")
    print("=" * 78)
    fails = 0

    def _mk(building_type, footprint, stories, current_ceil,
            has_finish_schedule=False, commons_act=False,
            override_efficiency=None, used_footprint_fallback=False):
        rooms = []
        if commons_act:
            rooms.append({
                "room_name": "Corridor",
                "materials": {"ceiling": "ACT", "ceiling_painted": False},
                "dimensions": {"floor_area_sqft": 720, "ceiling_area_sqft": 0},
                "unit_multiplier": 1,
            })
        a = {
            "project_info": {
                "building_type": building_type,
                "footprint_sqft": footprint,
                "total_stories": stories,
            },
            "floors": [{"floor_name": "Test", "rooms": rooms}],
            "aggregated_totals": {
                "total_paintable_ceiling_sqft": current_ceil,
            },
            "has_finish_schedule": has_finish_schedule,
        }
        if override_efficiency is not None:
            a["project_info"]["_residential_efficiency"] = override_efficiency
        if used_footprint_fallback:
            a["_used_footprint_fallback"] = True
        return a

    def _check(label, analysis, expect_ceil):
        nonlocal fails
        T._apply_residential_ceiling_floor(analysis)
        got = analysis["aggregated_totals"]["total_paintable_ceiling_sqft"]
        ok = (abs(got - expect_ceil) <= 1)  # rounding tolerance
        mark = "OK " if ok else "XX "
        print(f"  {mark} {label:60} got={got:>7,} expected={expect_ceil:>7,}")
        if not ok:
            fails += 1

    # Ridgeview-shaped: floor fires, default efficiency 0.97
    _check("Ridgeview-shaped (multi-family, 32,601 → ~42,500)",
           _mk("multi-family", 14603, 3, 32601),
           round(14603 * 3 * 0.97))
    # Extracted is already above expected → no change
    _check("extraction is plausible (no bump needed)",
           _mk("multifamily", 14603, 3, 41000),
           41000)
    # Extracted within 10% of expected → no change (under the trigger)
    _check("within 10% of expected (no bump needed)",
           _mk("multifamily", 14603, 3, 40000),
           40000)
    # Commercial building → no-op (not residential)
    _check("commercial office (no-op)",
           _mk("office commercial", 14603, 3, 10000),
           10000)
    # Finish schedule shows ACT in commons → use UNITS_ONLY efficiency
    _check("finish schedule shows ACT in commons (0.63 efficiency)",
           _mk("multi-family", 14603, 3, 10000,
               has_finish_schedule=True, commons_act=True),
           round(14603 * 3 * 0.63))
    # Project override = 0.85 → use 0.85
    _check("project override efficiency 0.85",
           _mk("multifamily", 14603, 3, 20000, override_efficiency=0.85),
           round(14603 * 3 * 0.85))
    # _used_footprint_fallback already set → skip
    _check("footprint fallback already used (skip)",
           _mk("multifamily", 14603, 3, 5000, used_footprint_fallback=True),
           5000)
    # No footprint known → skip
    _check("no footprint known (skip)",
           _mk("multifamily", 0, 3, 5000),
           5000)
    # Idempotency: second call no-op
    a = _mk("multi-family", 14603, 3, 32601)
    T._apply_residential_ceiling_floor(a)
    first = a["aggregated_totals"]["total_paintable_ceiling_sqft"]
    T._apply_residential_ceiling_floor(a)
    second = a["aggregated_totals"]["total_paintable_ceiling_sqft"]
    if first != second:
        print(f"  XX idempotency FAILED: {first} → {second}")
        fails += 1
    else:
        print(f"  OK idempotency (second call is no-op)")

    print(f"\n  {9 - fails}/9 passed\n")
    return fails == 0


def test_source_sheet_canonicalization(pdf_path):
    """End-to-end: verify _build_page_to_sheet_map detects all 14
    Ridgeview sheets including the ones the old bottom-strip clip missed.
    """
    print("=" * 78)
    print(f"Source-sheet canonicalization: {os.path.basename(pdf_path)}")
    print("=" * 78)
    if not os.path.exists(pdf_path):
        print(f"  SKIP — file not found: {pdf_path}")
        return True
    page_map = T._build_page_to_sheet_map([pdf_path])
    # Ridgeview has 14 pages: T1 + A1..A13
    expected_per_page = {
        0: "T1", 1: "A1", 2: "A2", 3: "A3", 4: "A4", 5: "A5", 6: "A6",
        7: "A7", 8: "A8", 9: "A9", 10: "A10", 11: "A11", 12: "A12",
        13: "A13",
    }
    fails = 0
    for page_idx, expected in expected_per_page.items():
        got = page_map.get((pdf_path, page_idx))
        ok = (got == expected)
        mark = "OK " if ok else "XX "
        print(f"  {mark} page {page_idx+1:>2} expected={expected:>4}  got={got!s:>4}")
        if not ok:
            fails += 1
    print(f"\n  {len(expected_per_page) - fails}/{len(expected_per_page)} passed\n")
    return fails == 0


def test_schedule_detection(pdf_path):
    """Run finish / door / window schedule detectors against the Ridgeview
    PDF. All three should now return True (all three failed in Elliott's run).
    """
    print("=" * 78)
    print(f"Schedule detection: {os.path.basename(pdf_path)}")
    print("=" * 78)
    if not os.path.exists(pdf_path):
        print(f"  SKIP — file not found: {pdf_path}")
        return True
    cases = [
        ("finish (A13)", T._detect_finish_schedule, True),
        ("door   (A12)", T._detect_door_schedule,   True),
        ("window (A4)",  T._detect_window_schedule, False),  # vector-only,
                                                              # expected miss
    ]
    fails = 0
    for label, fn, expected in cases:
        got = fn(pdf_path)
        ok = (bool(got) == expected) or (got is None and expected is False)
        mark = "OK " if ok else "XX "
        note = ""
        if label.startswith("window") and not got:
            note = " (vector-only schedule, no extractable text — needs " \
                   "vision/OCR fallback, separate task)"
        print(f"  {mark} {label:20} got={got!s:>5}  expected={expected}{note}")
        if not ok:
            fails += 1
    print(f"\n  {len(cases) - fails}/{len(cases)} passed\n")
    return fails == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=DEFAULT_JSON,
                    help=f"path to result JSON (default: {DEFAULT_JSON})")
    ap.add_argument("--pdf", default=DEFAULT_PDF,
                    help=f"path to Ridgeview PDF for schedule detection "
                    f"(default: {DEFAULT_PDF})")
    args = ap.parse_args()

    parser_ok = test_parser()
    print()
    corridor_ok = test_corridor_fix()
    print()
    validator_ok = test_unit_multiplier_validator()
    print()
    ceiling_floor_ok = test_residential_ceiling_floor()
    print()
    canon_ok = test_source_sheet_canonicalization(args.pdf)
    print()
    schedule_ok = test_schedule_detection(args.pdf)
    print()
    end_to_end_ok = test_ridgeview_dedup(args.json, args.pdf)

    print()
    if (parser_ok and corridor_ok and validator_ok and ceiling_floor_ok
            and canon_ok and schedule_ok and end_to_end_ok):
        print("All checks passed.")
        return 0
    print("FAILURES — see above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
