"""Tests for the closet-recovery pass (NIGHTSHIFT_CLOSET_RECOVERY).

364 Main golden: ceilings read -26% vs Rider with ZERO closet/pantry rooms
extracted across 9 floors of a 20-unit building; the hard-numbers Ceiling
Check RFI'd ~2,979 sqft instead of pricing it. The recovery pass re-reads the
unit plans (measured, not ratio-derived) and adds ceiling-only rooms; walls
stay untouched (VME owns them).
"""
import os

import Takeoff_DIRECT as TD


def _analysis(wall=92000, ceiling=19000):
    # wall/3.3 ≈ 27,879 expected → gap ≈ 8,879 (>8%): recovery fires
    return {
        "project_info": {"building_type": "mixed-use", "total_units": 20},
        "notes": [],
        "aggregated_totals": {
            "total_paintable_wall_sqft": wall,
            "total_paintable_ceiling_sqft": ceiling,
        },
        "floors": [
            {"floor_name": "2nd Floor", "rooms": [
                {"room_id": "F2-LR", "room_name": "Living Room (Unit 2A)",
                 "source_page": 10, "source_sheet": "A103",
                 "unit_multiplier": 1, "in_scope": True,
                 "dimensions": {"floor_area_sqft": 250, "wall_area_sqft": 800,
                                "ceiling_area_sqft": 250, "perimeter_lf": 66},
                 "materials": {"ceiling_painted": True, "ceiling": "GYP"},
                 "elements": {"base_trim_lf": 66}},
            ]},
        ],
    }


def _run(analysis, recovered, flag="1", n_pdfs=1):
    old_flag = os.environ.get("NIGHTSHIFT_CLOSET_RECOVERY")
    os.environ["NIGHTSHIFT_CLOSET_RECOVERY"] = flag
    old_fn = TD._recover_missed_small_rooms
    calls = []

    def fake(client, pdf_path, pages, known):
        calls.append((pdf_path, tuple(pages)))
        return recovered

    TD._recover_missed_small_rooms = fake
    try:
        out = TD._apply_closet_recovery(
            analysis, client=None, pdf_paths=["/x/plans.pdf"] * n_pdfs)
        return out, calls
    finally:
        TD._recover_missed_small_rooms = old_fn
        if old_flag is None:
            os.environ.pop("NIGHTSHIFT_CLOSET_RECOVERY", None)
        else:
            os.environ["NIGHTSHIFT_CLOSET_RECOVERY"] = old_flag


def test_flag_off_noop():
    a = _analysis()
    out, calls = _run(a, [{"room_name": "Coat Closet", "sheet": "A103",
                           "floor_area_sqft": 12, "count": 5}], flag="0")
    assert not calls
    assert out["aggregated_totals"]["total_paintable_ceiling_sqft"] == 19000


def test_no_gap_noop():
    a = _analysis(wall=60000, ceiling=19000)  # 60000/3.3=18,181 < ceiling
    out, calls = _run(a, [{"room_name": "Coat Closet", "sheet": "A103",
                           "floor_area_sqft": 12, "count": 5}])
    assert not calls


def test_recovers_measured_ceiling_only():
    a = _analysis()
    out, calls = _run(a, [
        {"sheet": "A103", "page": 11, "room_name": "Coat Closet (typ)",
         "length_feet": 3, "width_feet": 4, "floor_area_sqft": 12,
         "count": 20, "how_measured": "labeled dims"},
        {"sheet": "A103", "page": 11, "room_name": "Pantry (typ)",
         "floor_area_sqft": 20, "count": 20, "how_measured": "scaled"},
    ])
    assert calls, "recovery read should have run"
    agg = out["aggregated_totals"]
    # 12*20 + 20*20 = 640 sqft added directly to the aggregate (no recalc —
    # the VME gates inside _recalculate_totals are not re-entrant)
    assert agg["total_paintable_ceiling_sqft"] == 19000 + 640
    # walls aggregate untouched
    assert agg["total_paintable_wall_sqft"] == 92000
    # walls untouched by the synthetic rooms
    rooms = [r for fl in out["floors"] for r in fl["rooms"]
             if r.get("source") == "closet_recovery"]
    assert rooms and all(
        r["dimensions"]["wall_area_sqft"] == 0 and
        r["elements"]["base_trim_lf"] == 0 for r in rooms)
    # attached to the floor that owns sheet A103
    assert any(r.get("source") == "closet_recovery"
               for r in out["floors"][0]["rooms"])
    assert out["_closet_recovery"]["rooms"] == 40
    notes = " | ".join(str(n) for n in out.get("notes", []))
    assert "[Closet Recovery]" in notes


def test_budget_clamp_drops_excess_and_rfis():
    a = _analysis()  # gap ≈ 8,879; budget ≈ 13,319
    out, _ = _run(a, [
        {"sheet": "A103", "room_name": "Closet A", "floor_area_sqft": 100,
         "count": 100},   # 10,000 sqft — fits
        {"sheet": "A103", "room_name": "Closet B", "floor_area_sqft": 150,
         "count": 100},   # 15,000 sqft — over budget, dropped
    ])
    cr = out["_closet_recovery"]
    assert cr["dropped_over_budget"] == 100
    notes = " | ".join(str(n) for n in out.get("notes", []))
    assert "RFI REQUIRED" in notes


def test_dedup_and_sanitation():
    a = _analysis()
    out, _ = _run(a, [
        # duplicate of an extracted room on the same sheet → skipped
        {"sheet": "A103", "room_name": "Living Room (Unit 2A)",
         "floor_area_sqft": 12, "count": 1},
        # oversized "closet" → skipped
        {"sheet": "A103", "room_name": "Great Closet", "floor_area_sqft": 400,
         "count": 1},
        # zero-dims → skipped
        {"sheet": "A103", "room_name": "Mystery", "floor_area_sqft": 0,
         "count": 1},
    ])
    assert "_closet_recovery" not in out


def test_multi_pdf_skips():
    a = _analysis()
    out, calls = _run(a, [{"sheet": "A103", "room_name": "Coat Closet",
                           "floor_area_sqft": 12, "count": 5}], n_pdfs=2)
    assert not calls


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
    print("ALL PASS")
