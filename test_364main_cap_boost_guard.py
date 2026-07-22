"""Regression test for the 364 Main under-bid.

364 Main (mixed-use, 20 units, 5 of 17 sheets extracted) priced Gyp. Walls at
31,416 SF and Gyp. Ceilings at 10,472 SF, even though aggregation had boosted the
paintable-wall total to 113,560 SF. Root cause: the apartment FOOTPRINT cap
(footprint x stories x 3.0) fired AFTER the perimeter boost. The footprint was
read from a thin/partial sheet set (corrected 2,618 -> 5,236 SF), so the cap
clamped a legitimately-boosted total back down to ~28% of itself.

NIGHTSHIFT_APT_CAP_BOOST_GUARD suppresses BOTH multi-family caps when a wall boost
fired this run, pricing the boosted totals directly. Rider's manual takeoff
(364 Mainstreet Beacon Take Offs.xlsx) measured 113,615.72 wall SF and priced the
job at $162,456 — the pipeline's boosted 113,560 SF is essentially exact, proving
the caps (which cut it to 31,416) are the bug. Flag OFF must reproduce the buggy
numbers exactly (byte-identical legacy behavior); flag ON must price the boost.
"""
import os
import importlib


# 364 Main inputs, taken from construction_analysis_20260701_003551.json.
# footprint_sqft is the *corrected* plate (2,618 -> 5,236 via _dedup_floor_plate
# check) that the cap actually used: 5,236 x 2 x 3.0 = 31,416 == priced walls.
FOOTPRINT = 5236
STORIES = 2
UNITS = 20
BOOSTED_WALLS = 113560
BOOSTED_CEIL = 24949

AGG = {
    "total_paintable_wall_sqft": BOOSTED_WALLS,
    "total_paintable_ceiling_sqft": BOOSTED_CEIL,
    "total_cmu_wall_sqft": 0,
    "total_base_trim_lf": 12448,
}
PROJECT_INFO = {
    "building_type": "mixed-use",
    "footprint_sqft": FOOTPRINT,
    "total_stories": STORIES,
    "total_units": UNITS,
    "_building_inventory_units": UNITS,
}
# The boost note the guard keys off of — present on every real 364 Main run.
ANALYSIS = {
    "notes": [
        "[Perimeter Wall Boost] Aggregated walls (96,526.0 sqft) < "
        "perimeter-derived (113,560 sqft). Boosted to 113,560 sqft (1.18x)."
    ],
    "floors": [],
}


def _run(flag_value):
    os.environ["NIGHTSHIFT_APT_CAP_BOOST_GUARD"] = flag_value
    import Takeoff_DIRECT
    importlib.reload(Takeoff_DIRECT)  # re-evaluate env-gated helper
    import copy
    analysis = copy.deepcopy(ANALYSIS)
    ce = Takeoff_DIRECT.calculate_costs(
        dict(AGG), building_type="mixed-use",
        project_info=dict(PROJECT_INFO), analysis=analysis,
    )
    qty = {}
    for li in ce.get("line_items", []):
        item = str(li.get("item", "")).lower()
        if item.startswith("gyp. walls"):
            qty["walls"] = round(_num(li.get("qty", 0)))
        elif item.startswith("gyp. ceilings"):
            qty["ceilings"] = round(_num(li.get("qty", 0)))
    return qty, analysis


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def test_flag_off_reproduces_the_underbid():
    """Legacy behavior is unchanged: footprint cap still clamps to 31,416/10,472."""
    qty, _ = _run("0")
    assert qty["walls"] == 31416, qty
    assert qty["ceilings"] == 10472, qty


def test_flag_on_prices_the_boost():
    """Guard suppresses both caps; boosted totals price directly (matches Rider)."""
    qty, analysis = _run("1")
    # Boost is trusted: walls == the boosted agg total, near Rider's 113,616 SF.
    assert qty["walls"] == BOOSTED_WALLS, qty       # 113,560, was 31,416
    assert qty["ceilings"] == round(BOOSTED_CEIL), qty  # 24,949, was 10,472
    assert qty["walls"] > 31416 and qty["ceilings"] > 10472
    # Within 1% of Rider's manual wall takeoff (113,615.72 SF).
    assert abs(qty["walls"] - 113616) / 113616 < 0.01, qty
    # Suppression is traceable in the notes.
    assert any("Apt Cap Boost Guard" in str(n) for n in analysis["notes"])


def test_flag_on_is_failsafe_without_a_boost():
    """No boost note -> guard is inert, footprint cap still applies (no over-price)."""
    os.environ["NIGHTSHIFT_APT_CAP_BOOST_GUARD"] = "1"
    import Takeoff_DIRECT
    importlib.reload(Takeoff_DIRECT)
    import copy
    analysis = copy.deepcopy(ANALYSIS)
    analysis["notes"] = []  # no boost fired
    ce = Takeoff_DIRECT.calculate_costs(
        dict(AGG), building_type="mixed-use",
        project_info=dict(PROJECT_INFO), analysis=analysis,
    )
    walls = next((round(_num(li.get("qty", 0))) for li in ce["line_items"]
                  if str(li.get("item", "")).lower().startswith("gyp. walls")), None)
    assert walls == 31416, walls  # footprint cap unchanged when no boost


# --- VME authoritative-walls cap guard (364 Main run 2026-07-21) ---
# construction_analysis_20260721_052032.json: the VME authoritative gate priced
# walls from vector geometry (112,727 SF, within 0.8% of Rider's measured
# 113,616), but the multi-family UNIT cap then clamped the priced quantity to
# 20 x 3,000 + max(0, 4-2) x 17,556 x 0.5 = 77,556 — the boost guard never fired
# because VME application writes a "[VME]" note, not "[Wall Boost]". Wall caps
# must be suppressed whenever _vme_authoritative.applied is True, regardless of
# NIGHTSHIFT_APT_CAP_BOOST_GUARD; ceiling caps stay active (ceilings are still
# LLM-derived).
VME_FOOTPRINT = 17556
VME_STORIES = 4
VME_WALLS = 112727
VME_CEIL = 13946.75

VME_AGG = {
    "total_paintable_wall_sqft": VME_WALLS,
    "total_paintable_ceiling_sqft": VME_CEIL,
    "total_cmu_wall_sqft": 528,
    "total_base_trim_lf": 5163.5,
}
VME_PROJECT_INFO = {
    "building_type": "mixed-use",
    "footprint_sqft": VME_FOOTPRINT,
    "total_stories": VME_STORIES,
    "total_units": UNITS,
    "_building_inventory_units": UNITS,
}
VME_ANALYSIS = {
    "notes": [
        "[VME] Walls priced from deterministic vector measurement: 11,627 LF "
        "of scope-clipped wall runs = 112,727 sqft (extraction read 66,248; "
        "kept for comparison)."
    ],
    "floors": [],
    "_vme_authoritative": {"applied": True, "basis": "scoped"},
}


def _run_vme(agg, analysis, flag_value="0"):
    os.environ["NIGHTSHIFT_APT_CAP_BOOST_GUARD"] = flag_value
    import Takeoff_DIRECT
    importlib.reload(Takeoff_DIRECT)
    import copy
    analysis = copy.deepcopy(analysis)
    ce = Takeoff_DIRECT.calculate_costs(
        dict(agg), building_type="mixed-use",
        project_info=dict(VME_PROJECT_INFO), analysis=analysis,
    )
    qty = {}
    for li in ce.get("line_items", []):
        item = str(li.get("item", "")).lower()
        if item.startswith("gyp. walls"):
            qty["walls"] = round(_num(li.get("qty", 0)))
        elif item.startswith("gyp. ceilings"):
            qty["ceilings"] = round(_num(li.get("qty", 0)))
    return qty, analysis


def test_vme_applied_suppresses_wall_caps_even_with_guard_flag_off():
    """VME-priced walls must survive the unit cap (112,727 stays, not 77,556)."""
    qty, analysis = _run_vme(VME_AGG, VME_ANALYSIS, flag_value="0")
    assert qty["walls"] == VME_WALLS, qty  # was clamped to 77,556 on 7/21
    assert any("Apt Cap VME Guard" in str(n) for n in analysis["notes"])


def test_vme_applied_keeps_ceiling_caps_active():
    """Ceilings are still LLM-derived — the unit ceiling cap (20 x 1,100 =
    22,000) must still clamp an inflated ceiling total with VME applied."""
    agg = dict(VME_AGG)
    agg["total_paintable_ceiling_sqft"] = 30000  # phantom-floor style inflation
    qty, _ = _run_vme(agg, VME_ANALYSIS, flag_value="0")
    assert qty["walls"] == VME_WALLS, qty
    assert qty["ceilings"] == 22000, qty


def test_no_vme_no_boost_reproduces_the_unit_cap():
    """Without VME application the unit cap still clamps to 77,556 (legacy)."""
    import copy
    analysis = copy.deepcopy(VME_ANALYSIS)
    analysis["_vme_authoritative"] = {"applied": False, "reason": "test"}
    analysis["notes"] = []
    qty, analysis = _run_vme(VME_AGG, analysis, flag_value="0")
    # 20 x 3,000 + max(0, 4-2) x 17,556 x 0.5 = 77,556
    assert qty["walls"] == 77556, qty
    assert not any("Apt Cap VME Guard" in str(n) for n in analysis["notes"])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    os.environ.pop("NIGHTSHIFT_APT_CAP_BOOST_GUARD", None)
    print("\nAll 364 Main cap-boost-guard tests passed.")
