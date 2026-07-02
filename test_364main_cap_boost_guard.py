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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    os.environ.pop("NIGHTSHIFT_APT_CAP_BOOST_GUARD", None)
    print("\nAll 364 Main cap-boost-guard tests passed.")
