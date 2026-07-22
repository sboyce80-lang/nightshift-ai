"""Regression tests for the BofA Vails Gate exterior fixes (2026-07-21).

Job bea0ba66 (Bank of America - Vails Gate, exterior-only Gensler set) priced
$7,776 while the elevation pass had extracted far more scope. Three defects,
all behind flags:

  NIGHTSHIFT_EXT_PRICING_FIX (calculate_costs):
    (a) 'vinyl siding' in the factory-finish keyword list zeroed Azek (60 LF)
        and corner boards (40 LF) on a job whose notes explicitly said the
        vinyl siding is prepped and PAINTED (EXPT-15). Explicit paint
        language now overrides the factory-finish keyword hit.
    (b) The Azek-covers-window-casings dedup zeroed 320 LF of window trim
        because 60 LF of Azek existed — and ran BEFORE the suppression that
        then zeroed the Azek too, so both priced $0. The dedup now runs
        after suppression and only when Azek LF >= window-trim LF.
    (c) exterior soffit_sqft (480 SF) was extracted but never priced — the
        exterior_soffit_fascia config key existed unwired.
    (d) exterior_door_count (2 painted HM service doors) was extracted by
        the elevation pass but dropped by the merge key list and never
        priced. Now merged and priced at the doors_hm_panel rate.
    (e) The blanket "single-story buildings never need exterior lifts" rule
        zeroed an EXPLICIT lift_required=true on a job with real exterior
        scope. The extractor's lift criterion includes painted surfaces
        above ~14 ft (soffits / roof peaks), which ladders don't reach even
        on 1-story buildings. Explicit flag + real scope now keeps the lift.

  NIGHTSHIFT_ELEV_RECONCILE (_drop_stale_elevation_claims):
    The tiled room-extraction call only sees plan-sheet tiles, so its notes
    claimed elevation sheets A01.01/A01.02 were "not included" — false at
    the set level (they were pages 4-5). Those claims became two
    Missing-Drawings RFIs for sheets the customer already submitted, and
    Will downgraded the elevation-derived takeoff to "unanchored"
    (confidence 32%). After a successful elevation pass, stale
    missing-elevation notes are now dropped and a corrective note appended.

Offline, no API.
"""
import os
import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


def _set(flag, val):
    if val is None:
        os.environ.pop(flag, None)
    else:
        os.environ[flag] = val


def _li_total(costs, prefix):
    for li in costs["line_items"]:
        if li["item"].startswith(prefix):
            return li["total"]
    return None


def _li_qty(costs, prefix):
    for li in costs["line_items"]:
        if li["item"].startswith(prefix):
            return li["qty"]
    return None


# BofA Vails Gate shape: exterior-only 1-story commercial, interior all zero.
_AGG_ZERO = {}
_PI_BOFA = {"building_type": "commercial", "total_stories": 1}
_EXT_BOFA = {
    "cornice_lf": 0, "window_trim_lf": 320, "soffit_sqft": 480,
    "railing_lf": 80, "lift_required": True, "interior_lift_required": False,
    "exterior_paint_sqft": 3200, "hardie_siding_sqft": 0,
    "azek_trim_lf": 60, "corner_board_lf": 40, "steel_lintel_lf": 0,
    "exterior_door_count": 2, "bollard_count": 6, "pipe_handrail_lf": 80,
    "exterior_siding_type": "Existing masonry/facade — type unconfirmed",
    "notes": ("All four elevations show a mix of painted vinyl siding "
              "(EXPT-15 Super White), painted decorative trim, painted "
              "soffits and columns; vinyl siding is prepped and painted "
              "per notes 08/09 and finish schedule EXPT-15."),
}


# ── Legacy (flag off) reproduces the BofA bug ───────────────────────────────
print("\nFlag OFF — legacy behavior reproduces the $7,776 under-bid")
_set("NIGHTSHIFT_EXT_PRICING_FIX", None)
c = T.calculate_costs(dict(_AGG_ZERO), exterior=dict(_EXT_BOFA),
                      building_type="commercial", project_info=dict(_PI_BOFA))
check(_li_qty(c, "Ext. Azek Trim") == 0, "legacy: Azek zeroed by 'vinyl siding' keyword")
check(_li_qty(c, "Ext. Corner Boards") == 0, "legacy: corner boards zeroed")
check(_li_qty(c, "Exterior Window Trim") == 0, "legacy: window trim zeroed by Azek dedup")
check(_li_total(c, "Ext. Soffit/Fascia") in (None, 0.0), "legacy: no exterior soffit line priced")
check(_li_total(c, "Ext. HM Doors") in (None, 0.0), "legacy: no exterior HM door line priced")
check(_li_total(c, "Exterior Lift") == 0.0, "legacy: single-story rule drops explicit lift")
check(_li_qty(c, "Exterior Painting") == 3200, "legacy: facade still priced")


# ── Fix a+b: paint-override + deferred conditional dedup ────────────────────
print("\nFlag ON — painted vinyl overrides factory-finish; trim/Azek both price")
_set("NIGHTSHIFT_EXT_PRICING_FIX", "1")
c = T.calculate_costs(dict(_AGG_ZERO), exterior=dict(_EXT_BOFA),
                      building_type="commercial", project_info=dict(_PI_BOFA))
check(_li_qty(c, "Ext. Azek Trim") == 60, "Azek survives (notes say siding is painted)")
check(_li_qty(c, "Ext. Corner Boards") == 40, "corner boards survive")
check(_li_qty(c, "Exterior Window Trim") == 320,
      "window trim kept: 60 LF Azek cannot cover 320 LF of casings")
check(_li_qty(c, "Exterior Painting") == 3200, "facade unchanged")

# Dedup still fires when Azek plausibly IS the casing stock
_ext = dict(_EXT_BOFA, azek_trim_lf=400,
            notes="Azek PVC trim at all window casings, painted.")
c = T.calculate_costs(dict(_AGG_ZERO), exterior=_ext,
                      building_type="commercial", project_info=dict(_PI_BOFA))
check(_li_qty(c, "Exterior Window Trim") == 0,
      "dedup still fires when Azek LF (400) >= window-trim LF (320)")
check(_li_qty(c, "Ext. Azek Trim") == 400, "Azek priced in dedup case")

# Factory finish with NO paint language still suppresses
_ext = dict(_EXT_BOFA, notes="Existing vinyl siding throughout; factory finish, "
                             "no painting required on siding.")
c = T.calculate_costs(dict(_AGG_ZERO), exterior=_ext,
                      building_type="commercial", project_info=dict(_PI_BOFA))
check(_li_qty(c, "Ext. Azek Trim") == 0,
      "true factory-finish (no paint language) still suppresses Azek")


# ── Fix c+d: exterior soffit + exterior HM doors priced ─────────────────────
print("\nFlag ON — extracted soffit and HM service doors reach the estimate")
c = T.calculate_costs(dict(_AGG_ZERO), exterior=dict(_EXT_BOFA),
                      building_type="commercial", project_info=dict(_PI_BOFA))
check(_li_qty(c, "Ext. Soffit/Fascia") == 480, "480 SF exterior soffit priced")
check((_li_total(c, "Ext. Soffit/Fascia") or 0) > 0, "soffit line has nonzero total")
check(_li_qty(c, "Ext. HM Doors") == 2, "2 exterior HM doors priced")
check((_li_total(c, "Ext. HM Doors") or 0) > 0, "HM door line has nonzero total")


# ── Fix e: explicit lift + real scope survives the single-story rule ────────
print("\nFlag ON — explicit lift_required with exterior scope kept on 1 story")
c = T.calculate_costs(dict(_AGG_ZERO), exterior=dict(_EXT_BOFA),
                      building_type="commercial", project_info=dict(_PI_BOFA))
check((_li_total(c, "Exterior Lift") or 0) > 0, "1-story explicit lift priced")

# Beloit guard unchanged: lift_required with NO exterior scope stays $0
c = T.calculate_costs({"total_paintable_wall_sqft": 18902},
                      exterior={"lift_required": True},
                      building_type="commercial",
                      project_info={"building_type": "commercial", "total_stories": 5})
check(_li_total(c, "Exterior Lift") == 0.0,
      "Beloit guard intact: lift_required with zero exterior scope -> $0")

# Height-only flag on 1-story with no scope also stays $0
c = T.calculate_costs(dict(_AGG_ZERO), exterior={"lift_required": True},
                      building_type="commercial", project_info=dict(_PI_BOFA))
check(_li_total(c, "Exterior Lift") == 0.0,
      "1-story lift_required with zero exterior scope -> $0")


# ── Elevation-note reconcile ────────────────────────────────────────────────
print("\nNIGHTSHIFT_ELEV_RECONCILE — stale missing-elevation claims dropped")
_STALE_SEG = ("Exterior elevation sheets A01.01 and A01.02 were NOT included "
              "in the pages provided to this analysis. RFI required: request "
              "sheets A01.01 and A01.02 to quantify exterior painting scope.")
_GOOD_SEG = ("All four elevations of this single-story branch show painted "
             "vinyl siding (EXPT-15), painted trim, soffits and columns.")
_DOOR_NOTE = "No door schedule was found in the provided documents."


def _mk_analysis():
    return {
        "notes": [
            "MISSING SHEETS: Exterior elevation sheets A01.01 and A01.02 "
            "were not included in the pages provided.",
            _DOOR_NOTE,
        ],
    }


def _mk_merged():
    return {
        "exterior_paint_sqft": 3200,
        "notes": _STALE_SEG + " | " + _GOOD_SEG,
        "source_pages": [2, 4, 5, 10],
    }


_set("NIGHTSHIFT_ELEV_RECONCILE", None)
a, m = _mk_analysis(), _mk_merged()
T._drop_stale_elevation_claims(a, m)
check(m["notes"] == _STALE_SEG + " | " + _GOOD_SEG,
      "flag off: exterior notes untouched")
check(len(a["notes"]) == 2, "flag off: analysis notes untouched")

_set("NIGHTSHIFT_ELEV_RECONCILE", "1")
a, m = _mk_analysis(), _mk_merged()
T._drop_stale_elevation_claims(a, m)
check(m["notes"] == _GOOD_SEG, "stale segment dropped from exterior notes")
check(_DOOR_NOTE in a["notes"],
      "door-schedule note (no 'elevation') is untouched")
check(not any("MISSING SHEETS" in n for n in a["notes"]),
      "stale MISSING SHEETS analysis note dropped")
check(any(n.startswith("[Elevation Reconcile]") for n in a["notes"]),
      "corrective note appended")

# All-zero elevation pass must NOT drop anything (claims may be true)
a, m = _mk_analysis(), _mk_merged()
m["exterior_paint_sqft"] = 0
T._drop_stale_elevation_claims(a, m)
check("MISSING SHEETS" in a["notes"][0],
      "zero-data pass leaves missing-sheet claims in place")

_set("NIGHTSHIFT_EXT_PRICING_FIX", None)
_set("NIGHTSHIFT_ELEV_RECONCILE", None)

print("\n=== ALL PASS ===" if not fails else f"\n=== {len(fails)} FAIL ===")
import sys
sys.exit(1 if fails else 0)
