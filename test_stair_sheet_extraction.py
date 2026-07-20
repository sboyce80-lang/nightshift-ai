"""Tests for targeted stair-flight extraction (NIGHTSHIFT_STAIR_SHEET_EXTRACTION).

364 Main golden: room extraction counted 25 stair "sections" (landings
counted as sections), the heuristic cap clamped to 16, Rider's takeoff has 11
actual flights. Flights drawn on stair/building sections are hard numbers —
when analyze_stair_sheets reads them, the count is authoritative BOTH
directions and the boost/cap heuristics stand down.
"""
import os

import Takeoff_DIRECT as TD

PDF_364MAIN = "/Users/stevenboyce/Desktop/_Projects/364Main/364Main.pdf"


def _combined(stair_info, room_stairs=25):
    return {
        "project_info": {"building_type": "mixed-use", "total_units": 20,
                         "total_stories": 4},
        "floors": [],
        "notes": [],
        "aggregated_totals": {"total_stair_sections": room_stairs},
        "schedule_data": {
            "door_schedule": {},
            "window_schedule": {},
            "stair_info": stair_info,
        },
    }


def test_stair_sheet_count_overrides_both_directions():
    out = TD._apply_schedule_overrides(_combined({
        "total_stair_sections": 11,
        "source": "stair_sheets",
        "confidence": "high",
        "notes": "counted from stair/section sheets",
    }))
    assert out["aggregated_totals"]["total_stair_sections"] == 11
    assert out["_stair_sheet_authoritative"] is True
    assert out["_schedule_authoritative_counts"]["total_stair_sections"] == 11
    notes = " | ".join(str(n) for n in out.get("notes", []))
    assert "Stairs SET from stair/section sheets" in notes


def test_legacy_stair_note_only_overrides_upward():
    # Without the stair_sheets source, a lower count must NOT reduce the
    # room-level number (legacy behavior preserved).
    out = TD._apply_schedule_overrides(_combined({
        "total_stair_sections": 8, "notes": "from schedule notes",
    }))
    assert out["aggregated_totals"]["total_stair_sections"] == 25
    assert not out.get("_stair_sheet_authoritative")

    out2 = TD._apply_schedule_overrides(_combined(
        {"total_stair_sections": 30, "notes": ""}, room_stairs=25))
    assert out2["aggregated_totals"]["total_stair_sections"] == 30


def test_identify_stair_pages_on_364main():
    if not os.path.exists(PDF_364MAIN):
        print("  (364Main.pdf not present — skipping page-scan test)")
        return
    pages = TD._identify_stair_pages(PDF_364MAIN)
    # Must find the building-section sheets (A-301/A-302 draw the stair runs)
    # and must NOT sweep in most of the 23-page set.
    assert pages, "expected at least one stair/section page"
    assert len(pages) <= 6, f"stair page scan too broad: {pages}"


def test_flag_helper_default_off():
    old = os.environ.pop("NIGHTSHIFT_STAIR_SHEET_EXTRACTION", None)
    try:
        assert TD._stair_sheet_extraction_enabled() is False
        os.environ["NIGHTSHIFT_STAIR_SHEET_EXTRACTION"] = "1"
        assert TD._stair_sheet_extraction_enabled() is True
    finally:
        if old is None:
            os.environ.pop("NIGHTSHIFT_STAIR_SHEET_EXTRACTION", None)
        else:
            os.environ["NIGHTSHIFT_STAIR_SHEET_EXTRACTION"] = old


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
    print("ALL PASS")
