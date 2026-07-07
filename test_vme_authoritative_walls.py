"""Tests for VME Release 2 — authoritative geometric walls (2026-07-07).

N=4 on PNC `4b5ef77b`: LLM extraction walls swung 29,085 / 12,213 / 12,021 /
30,637 SF on identical input while the VME shadow measured 2,422.5 LF
bit-identically every run (+2.7% vs the customer's verified takeoff at the
job's real ceiling height). _apply_vme_authoritative_walls promotes the
geometric measurement to the priced wall quantity behind
NIGHTSHIFT_VME_AUTHORITATIVE_WALLS (default off), abstaining loudly instead
of guessing: full page coverage required, 3+ measured room heights required
(never the 9-ft default), x0.4-x2.5 sanity band vs the LLM read.

Offline, no API, no geometry engine (shadow is stubbed via _vme_shadow_v2).
"""
import os

import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


def _analysis(llm_walls=12021, wc=103, heights=(10.83,) * 5, lf=2422.5,
              unmeasured=()):
    rooms = [{"room_name": f"Office {i}", "in_scope": True,
              "dimensions": {"ceiling_height_feet": h, "wall_area_sqft": 400}}
             for i, h in enumerate(heights)]
    return {
        "floors": [{"floor_name": "15", "rooms": rooms}],
        "aggregated_totals": {"total_paintable_wall_sqft": llm_walls,
                              "total_wallcovering_sqft": wc},
        "_vme_shadow_v2": {"total_wall_run_lf": lf, "est_wall_sf": lf * 9,
                           "n_floor_pages": 1,
                           "unmeasured": list(unmeasured),
                           "engine": "tier2-geometric+title-attribution"},
        "notes": [],
    }


os.environ["NIGHTSHIFT_VME_AUTHORITATIVE_WALLS"] = "1"

# ── Happy path: PNC numbers ─────────────────────────────────────────────────
print("\nPromotion (PNC shape)")
a = T._apply_vme_authoritative_walls(_analysis())
rec = a["_vme_authoritative"]
expected = round(2422.5 * 10.83 - 103, 2)
check(rec["applied"] is True, "promoted")
check(a["aggregated_totals"]["total_paintable_wall_sqft"] == expected,
      f"walls = LF x measured height - WC ({expected})")
check(rec["height_ft"] == 10.83 and rec["n_room_heights"] == 5,
      "height is measured, not the 9-ft default")

# Mixed grid/deck heights: the deck band (p90) wins, not the grid median —
# walls are painted past the ACT grid.
a_mix = T._apply_vme_authoritative_walls(
    _analysis(heights=(8.58,) * 8 + (10.83,) * 2))
check(a_mix["_vme_authoritative"]["height_ft"] == 10.83,
      "p90 height = deck band, not the 8.58 ft grid median")
check(rec["llm_wall_sqft"] == 12021, "LLM read preserved for comparison")
check(any(n.startswith("[VME] Walls priced from deterministic")
          for n in a["notes"]), "audit note added")
walls_after = a["aggregated_totals"]["total_paintable_wall_sqft"]
a2 = T._apply_vme_authoritative_walls(a)
check(a2["aggregated_totals"]["total_paintable_wall_sqft"] == walls_after,
      "idempotent")

# ── Abstentions ─────────────────────────────────────────────────────────────
print("\nAbstentions never guess")
a = T._apply_vme_authoritative_walls(_analysis(heights=(10.83, 10.83)))
check(a["_vme_authoritative"]["applied"] is False
      and a["aggregated_totals"]["total_paintable_wall_sqft"] == 12021,
      "fewer than 3 measured heights -> abstain (no 9-ft default)")
check("9-ft default" in a["_vme_authoritative"]["reason"],
      "abstention reason names the refused default")

a = T._apply_vme_authoritative_walls(
    _analysis(unmeasured=[{"page": 7, "reason": "no scale"}]))
check(a["_vme_authoritative"]["applied"] is False,
      "any unmeasured floor page -> abstain (partial geometry)")

a = T._apply_vme_authoritative_walls(_analysis(llm_walls=5000))
check(a["_vme_authoritative"]["applied"] is False
      and a["aggregated_totals"]["total_paintable_wall_sqft"] == 5000,
      "x5.2 vs LLM read is outside the sanity band -> abstain")

a = _analysis()
a["_vme_shadow_v2"] = None
a.pop("_vme_pdf_paths", None)
orig = T.vme_attribution.compute_vme_shadow_v2 if hasattr(T, "vme_attribution") else None
import vme_attribution
_orig_compute = vme_attribution.compute_vme_shadow_v2
vme_attribution.compute_vme_shadow_v2 = lambda paths: None
try:
    a = T._apply_vme_authoritative_walls(a)
finally:
    vme_attribution.compute_vme_shadow_v2 = _orig_compute
check(a["_vme_authoritative"]["applied"] is False,
      "engine returns nothing -> abstain gracefully")

# ── Wallcovering deduction floors at zero ───────────────────────────────────
print("\nEdge cases")
a = T._apply_vme_authoritative_walls(
    _analysis(lf=10, wc=99999, llm_walls=50))
check(a["aggregated_totals"]["total_paintable_wall_sqft"] in (0, 50),
      "huge WC deduction can never go negative")

# ── Flag off ────────────────────────────────────────────────────────────────
os.environ["NIGHTSHIFT_VME_AUTHORITATIVE_WALLS"] = "0"
a = T._apply_vme_authoritative_walls(_analysis())
check("_vme_authoritative" not in a
      and a["aggregated_totals"]["total_paintable_wall_sqft"] == 12021,
      "flag off -> completely inert")
os.environ.pop("NIGHTSHIFT_VME_AUTHORITATIVE_WALLS", None)


print("\n=== ALL PASS ===" if not fails else f"\n=== {len(fails)} FAIL ===")
import sys
sys.exit(1 if fails else 0)
