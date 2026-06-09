"""Offline regression tests for the confidence & room-count recovery changes.

Exercises the DETERMINISTIC code paths the fixes touch, with flags both on
and off, so we can prove behavior without a live extraction. Run:

    .venv/bin/python test_confidence_room_recovery.py
"""
import copy
import os

import Takeoff_DIRECT as T


def _room(name, sheet="A1", floor_area=300, wall=700, mult=1):
    return {
        "room_name": name, "source_sheet": sheet, "unit_multiplier": mult,
        "dimensions": {"length_feet": 20, "width_feet": 15,
                       "ceiling_height_feet": 10, "floor_area_sqft": floor_area,
                       "wall_area_sqft": wall, "ceiling_area_sqft": floor_area,
                       "perimeter_lf": 70},
        "elements": {}, "materials": {},
    }


def _pass(n_rooms, footprint=1500, failed_chunks=None, name_prefix="R"):
    rooms = [_room(f"{name_prefix}{i}") for i in range(n_rooms)]
    a = {"project_info": {"total_rooms_found": n_rooms, "footprint_sqft": footprint},
         "floors": [{"floor_name": "L1", "rooms": rooms}]}
    if failed_chunks is not None:
        a["_chunk_tracking"] = {"total_chunks": 3, "chunks_failed": failed_chunks}
    return a


def _count_rooms(a):
    return sum(len(f.get("rooms", []) or []) for f in a.get("floors", []) or [])


PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


# ---------------------------------------------------------------------------
print("\n[1] _recover_area_fields — rebuild nulls, preserve positives")
d = {"length_feet": 20, "width_feet": 15, "ceiling_height_feet": 10,
     "wall_area_sqft": None, "floor_area_sqft": None,
     "ceiling_area_sqft": None, "perimeter_lf": None}
T._recover_area_fields(d)
check("null wall recovered to perimeter*height (70*10=700)", d["wall_area_sqft"] == 700)
check("null floor recovered to l*w (300)", d["floor_area_sqft"] == 300)
check("null perimeter recovered to 2(l+w) (70)", d["perimeter_lf"] == 70)
d2 = {"length_feet": 20, "width_feet": 15, "ceiling_height_feet": 10,
      "wall_area_sqft": 999, "floor_area_sqft": 300,
      "ceiling_area_sqft": 300, "perimeter_lf": 70}
T._recover_area_fields(d2)
check("positive wall area NOT overwritten", d2["wall_area_sqft"] == 999)
d3 = {"wall_area_sqft": None}  # no geometry
T._recover_area_fields(d3)
check("unrecoverable null stays 0", T._num(d3.get("wall_area_sqft")) == 0)

# ---------------------------------------------------------------------------
print("\n[2] _normalize_analysis — recovered nulls don't trip the degraded gate")
an = {"project_info": {}, "floors": [{"floor_name": "L1", "rooms": [
    {"room_name": f"R{i}", "dimensions": {
        "length_feet": 20, "width_feet": 15, "ceiling_height_feet": 10,
        "wall_area_sqft": None,  # null but recoverable
        "floor_area_sqft": 300, "ceiling_area_sqft": 300, "perimeter_lf": 70},
     "elements": {}, "materials": {}}
    for i in range(10)]}]}
out = T._normalize_analysis(copy.deepcopy(an))
recovered = out["floors"][0]["rooms"][0]["dimensions"]["wall_area_sqft"]
check("recoverable null wall rebuilt during normalize (700)", recovered == 700)
check("recoverable nulls do NOT force manual review",
      not out.get("manual_review_required"))

# Unrecoverable nulls (no geometry) on many rooms SHOULD still flag.
an_bad = {"project_info": {}, "floors": [{"floor_name": "L1", "rooms": [
    {"room_name": f"R{i}", "dimensions": {"wall_area_sqft": None},
     "elements": {}, "materials": {}} for i in range(10)]}]}
out_bad = T._normalize_analysis(copy.deepcopy(an_bad))
check("unrecoverable nulls across many rooms still flag manual review",
      bool(out_bad.get("manual_review_required")))

# ---------------------------------------------------------------------------
print("\n[3] Multi-pass merge fallback — Wingstop [52,11,12] scenario")
# Distinct room names per pass so the per-room merge can't reconcile them,
# forcing the fallback (the real-world enhanced-vs-vector incompatibility).
p52 = _pass(52, footprint=1500, failed_chunks=[], name_prefix="A")
p11 = _pass(11, footprint=1500, failed_chunks=[], name_prefix="B")
p12 = _pass(12, footprint=1500, failed_chunks=[], name_prefix="C")

os.environ.pop("NIGHTSHIFT_MERGE_PREFER_COMPLETE", None)
merged_default = T._merge_passes_with_median(
    [copy.deepcopy(p52), copy.deepcopy(p11), copy.deepcopy(p12)])
check("default (median rule) ships the sparse pass (~12 rooms)",
      _count_rooms(merged_default) in (11, 12))

os.environ["NIGHTSHIFT_MERGE_PREFER_COMPLETE"] = "1"
merged_complete = T._merge_passes_with_median(
    [copy.deepcopy(p52), copy.deepcopy(p11), copy.deepcopy(p12)])
check("PREFER_COMPLETE ships the complete pass (52 rooms)",
      _count_rooms(merged_complete) == 52)

# Overshoot guard: a pass with an outlier-high footprint must NOT be chosen
# even under PREFER_COMPLETE (Ridgeview protection).
p_over = _pass(60, footprint=60000, failed_chunks=[], name_prefix="D")  # runaway
p_a = _pass(40, footprint=1500, failed_chunks=[], name_prefix="E")
p_b = _pass(38, footprint=1500, failed_chunks=[], name_prefix="F")
merged_guard = T._merge_passes_with_median(
    [copy.deepcopy(p_over), copy.deepcopy(p_a), copy.deepcopy(p_b)])
check("footprint-outlier overshoot pass rejected under PREFER_COMPLETE",
      _count_rooms(merged_guard) == 40)
os.environ.pop("NIGHTSHIFT_MERGE_PREFER_COMPLETE", None)

# ---------------------------------------------------------------------------
print("\n[4] Union merge — keep single-pass rooms instead of dropping them")
# Same room set repeated in 1 pass only; under majority it would drop.
shared = _room("Lobby")
solo = _room("Storage")
pa = {"project_info": {}, "floors": [{"floor_name": "L1",
      "rooms": [copy.deepcopy(shared), copy.deepcopy(solo)]}]}
pb = {"project_info": {}, "floors": [{"floor_name": "L1",
      "rooms": [copy.deepcopy(shared)]}]}
pc = {"project_info": {}, "floors": [{"floor_name": "L1",
      "rooms": [copy.deepcopy(shared)]}]}
os.environ.pop("NIGHTSHIFT_MERGE_UNION", None)
m_majority = T._merge_passes_with_median(
    [copy.deepcopy(pa), copy.deepcopy(pb), copy.deepcopy(pc)])
check("majority (default) drops the 1-of-3 room (keeps 1)",
      _count_rooms(m_majority) == 1)
os.environ["NIGHTSHIFT_MERGE_UNION"] = "1"
m_union = T._merge_passes_with_median(
    [copy.deepcopy(pa), copy.deepcopy(pb), copy.deepcopy(pc)])
check("union keeps the 1-of-3 room (keeps 2)", _count_rooms(m_union) == 2)
os.environ.pop("NIGHTSHIFT_MERGE_UNION", None)

# ---------------------------------------------------------------------------
print("\n[5] Confidence decouple — policy zeros excluded, real failures kept")
analysis_wc = {
    "project_info": {"building_type": "commercial", "footprint_sqft": 5000},
    "aggregated_totals": {"total_paintable_wall_sqft": 9000,
                          "total_wallcovering_sqft": 0,
                          "total_cmu_wall_sqft": 0,
                          "total_dryfall_ceiling_sqft": 0},
    "notes": ["Wallcovering extent unconfirmed — no finish schedule; RFI required."],
    "floors": [{"floor_name": "L1", "rooms": [_room("Office")]}],
}
costs = {"subtotal": 20000, "line_items": []}

os.environ["NIGHTSHIFT_CONFIDENCE_DECOUPLE"] = "1"
v_on = T._validate_cost_estimate(copy.deepcopy(analysis_wc), costs)
os.environ["NIGHTSHIFT_CONFIDENCE_DECOUPLE"] = "0"
v_off = T._validate_cost_estimate(copy.deepcopy(analysis_wc), costs)
os.environ["NIGHTSHIFT_CONFIDENCE_DECOUPLE"] = "1"
check("decouple ON raises score above decouple OFF",
      v_on["data_quality_score"] > v_off["data_quality_score"])
check("wallcovering + cmu warnings still listed (visibility kept)",
      v_on["warning_count"] >= 2)
check("policy_excluded counter reflects the suppressed penalties",
      v_on.get("policy_excluded_warnings", 0) >= 2)

# Genuine failure (zero walls) keeps its penalty even with decouple ON.
analysis_zerowall = {
    "project_info": {"building_type": "commercial"},
    "aggregated_totals": {"total_paintable_wall_sqft": 0,
                          "total_cmu_wall_sqft": 0,
                          "total_wallcovering_sqft": 100},
    "notes": [], "floors": [],
}
v_zw = T._validate_cost_estimate(copy.deepcopy(analysis_zerowall), costs)
check("genuine zero-walls failure still deducts (score < 100)",
      v_zw["data_quality_score"] < 100)

# ---------------------------------------------------------------------------
print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
    raise SystemExit(1)
print("ALL TESTS PASSED")
