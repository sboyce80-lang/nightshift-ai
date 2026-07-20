"""Tests for the field-paintable wood-window breakdown + wood default.

364 Main golden (2026-07-08): window schedule A-502 lists 92 windows — 6
fire-rated FYRE-TEC (factory-finished) + 16 "Provided by Owner (Pella)" (red
revision annotations, incl. a bracket arrow spanning marks 219-230) + 70
field-paintable wood double-hungs. The old pipeline priced 0 painted windows
(no interior paint spec on the schedule) with a generic note; Rider's bid
painted 26 (a finish-spec-guide subset outside the bid set).

New behavior in _apply_schedule_overrides:
  - Always: stash total_windows_field_paintable and either price it (flag ON,
    residential context) or emit a quantified "RFI REQUIRED" note.
  - NIGHTSHIFT_WINDOW_WOOD_DEFAULT=1 + residential/mixed-use: the wood count
    is priced as painted interior with an "(assumed)" marker.
  - Pure commercial jobs never default, flag or no flag.
"""
import os
import importlib

import Takeoff_DIRECT as TD


def _combined(building_type="mixed-use", units=20, window_schedule=None):
    ws = {
        "total_windows": 92,
        "windows_painted_interior": 0,
        "windows_owner_provided": 16,
        "windows_factory_finished": 6,
        "windows_field_paintable_wood": 70,
        "window_paint_spec": "",
        "notes": "",
    }
    if window_schedule is not None:
        ws = window_schedule
    return {
        "project_info": {
            "building_type": building_type,
            "total_units": units,
        },
        "floors": [],
        "notes": [],
        "has_window_schedule": True,
        "aggregated_totals": {
            "total_windows_painted_interior": 0,
            "total_windows_all": 98,
        },
        "schedule_data": {
            "door_schedule": {},
            "window_schedule": ws,
            "stair_info": {},
        },
    }


def _run(combined, flag):
    old = os.environ.get("NIGHTSHIFT_WINDOW_WOOD_DEFAULT")
    os.environ["NIGHTSHIFT_WINDOW_WOOD_DEFAULT"] = flag
    try:
        return TD._apply_schedule_overrides(combined)
    finally:
        if old is None:
            os.environ.pop("NIGHTSHIFT_WINDOW_WOOD_DEFAULT", None)
        else:
            os.environ["NIGHTSHIFT_WINDOW_WOOD_DEFAULT"] = old


def test_flag_off_emits_quantified_rfi_not_priced():
    out = _run(_combined(), "0")
    agg = out["aggregated_totals"]
    assert agg["total_windows_painted_interior"] == 0
    assert agg["total_windows_field_paintable"] == 70
    notes = " | ".join(str(n) for n in out.get("notes", []))
    assert "RFI REQUIRED" in notes
    assert "70 field-paintable wood window(s)" in notes
    assert "16 owner-provided" in notes
    assert "6 factory-finished" in notes
    # RFI generator must pick the note up
    items = TD.generate_rfi_items(out)
    assert any("70 wood windows" in str(i.get("question", "")) for i in items)


def test_flag_on_residential_prices_wood_count_as_assumed():
    out = _run(_combined(), "1")
    agg = out["aggregated_totals"]
    assert agg["total_windows_painted_interior"] == 70
    assert agg["total_windows_field_paintable"] == 70
    # persists through re-aggregation via the authoritative stash
    assert out["_schedule_authoritative_counts"][
        "total_windows_painted_interior"] == 70
    notes = " | ".join(str(n) for n in out.get("notes", []))
    assert "(assumed)" in notes


def test_flag_on_commercial_does_not_default():
    out = _run(_combined(building_type="office", units=0), "1")
    agg = out["aggregated_totals"]
    assert agg["total_windows_painted_interior"] == 0
    notes = " | ".join(str(n) for n in out.get("notes", []))
    assert "RFI REQUIRED" in notes


def test_explicit_schedule_paint_spec_untouched():
    ws = {
        "total_windows": 92,
        "windows_painted_interior": 26,
        "windows_owner_provided": 16,
        "windows_factory_finished": 6,
        "windows_field_paintable_wood": 70,
    }
    out = _run(_combined(window_schedule=ws), "1")
    # Schedule-confirmed painted count stays authoritative; no default fires.
    assert out["aggregated_totals"]["total_windows_painted_interior"] == 26
    notes = " | ".join(str(n) for n in out.get("notes", []))
    assert "(assumed)" not in notes


def test_wood_count_derived_from_type_rows_when_summary_missing():
    ws = {
        "total_windows": 92,
        "windows_painted_interior": 0,
        "window_types": [
            {"mark": "100", "qty": 1, "frame": "FYRE-TEC Series 925",
             "factory_finished": True},
            {"mark": "200-218", "qty": 19,
             "frame": "wood (WD framed double hung per type detail)"},
            {"mark": "219-230", "qty": 12, "frame": "wood",
             "owner_provided": True},
            {"mark": "300-339", "qty": 40,
             "frame": "wood (WD framed double hung)"},
            {"mark": "REST", "qty": 20, "frame": ""},  # unknown frame: excluded
        ],
    }
    out = _run(_combined(window_schedule=ws), "0")
    # 19 + 40 wood, not owner/factory; blank-frame rows conservatively skipped
    assert out["aggregated_totals"]["total_windows_field_paintable"] == 59


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
    print("ALL PASS")
