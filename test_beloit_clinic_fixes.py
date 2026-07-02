"""Regression tests for the Beloit Clinic 4th-Floor reno pricing fixes (2026-06-20).

Scott Redmond (Devine) ran the BHS Gold Family Care Center DD set — a single
4th-floor renovation inside a 5-story building — and the output came back low.
Two pricing defects fixed here in Takeoff_DIRECT.py:

  (1) DOOR $0 RATE: a blank/zero `door_rate` rate override coerced to 0 and
      `_apply_rate_overrides` applied it verbatim, zeroing all door tiers — 36
      doors priced at $0. A $0 paint rate is never a real price; scope is
      suppressed by zeroing the QUANTITY, not the rate.
  (2) PHANTOM EXTERIOR LIFT: the extractor sets lift_required=True on building
      height alone (3+ stories). On an interior-only reno of a tall building
      that fired an $8,000 exterior lift with zero exterior scope. An exterior
      lift is exterior-only; remove it when no exterior scope exists.

(The third Beloit symptom — the implausible-vs-footprint manual-review gate —
is handled separately on main by the work-area basis, _declared_work_area_sqft /
NIGHTSHIFT_WORK_AREA_BASIS, and is not retested here.)

Offline, no API.
"""
import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


# ── Fix 1: rate-override guard ──────────────────────────────────────────────
print("\nFix 1 — zero/blank rate override must not zero the catalog line")
_DEFAULT = [t["rate"] for t in T.PRICING_MODEL["doors_full_paint"]["tiers"]]
pm = T._apply_rate_overrides({"door_rate": 0})
check([t["rate"] for t in pm["doors_full_paint"]["tiers"]] == _DEFAULT,
      "door_rate=0 keeps catalog default")
check(not pm["doors_full_paint"].get("_rate_overridden"),
      "door_rate=0 does not mark line as overridden")
pm = T._apply_rate_overrides({"door_rate": ""})
check([t["rate"] for t in pm["doors_full_paint"]["tiers"]] == _DEFAULT,
      "blank door_rate keeps catalog default")
pm = T._apply_rate_overrides({"door_rate": None})
check([t["rate"] for t in pm["doors_full_paint"]["tiers"]] == _DEFAULT,
      "None door_rate keeps catalog default")
pm = T._apply_rate_overrides({"door_rate": 175})
check(all(t["rate"] == 175.0 for t in pm["doors_full_paint"]["tiers"]),
      "valid door_rate=175 still applies")
check(pm["doors_full_paint"].get("_rate_overridden") is True,
      "valid override marks line as overridden")
# Zero markup is legitimate and must still apply.
pm = T._apply_rate_overrides({"markup": 0})
check(all(v.get("markup") == 0 for v in pm.values() if isinstance(v, dict)),
      "markup=0 is a valid override and still applies")


# ── Fix 2: phantom exterior lift ────────────────────────────────────────────
print("\nFix 2 — exterior lift dropped when no exterior scope (kept when present)")
_AGG = {"total_paintable_wall_sqft": 18902, "total_paintable_ceiling_sqft": 3518,
        "total_doors_full_paint": 36, "total_stair_sections": 3,
        "total_gyp_between_stairs_sqft": 640, "total_painted_railing_lf": 50}
_PI = {"building_type": "commercial", "total_stories": 5}


def _lift_total(costs):
    for li in costs["line_items"]:
        if li["item"].startswith("Exterior Lift"):
            return li["total"]
    return None


def _door_fp_total(costs):
    for li in costs["line_items"]:
        if li["item"].startswith("Doors (Full Paint)"):
            return li["total"]
    return None


c = T.calculate_costs(_AGG, exterior={"lift_required": True},
                      building_type="commercial", project_info=_PI)
check(_lift_total(c) == 0.0,
      "5-story, no exterior scope -> exterior lift = $0")
check((_door_fp_total(c) or 0) > 0,
      "36 doors price at a real commercial rate (not $0)")

c = T.calculate_costs(_AGG, exterior={"lift_required": True,
                                      "exterior_paint_sqft": 5000},
                      building_type="commercial", project_info=_PI)
check((_lift_total(c) or 0) > 0,
      "5-story WITH exterior paint -> exterior lift kept")

# Interior-only exterior quantity (window trim) still justifies the lift.
c = T.calculate_costs(_AGG, exterior={"lift_required": True,
                                      "window_trim_lf": 200},
                      building_type="commercial", project_info=_PI)
check((_lift_total(c) or 0) > 0,
      "exterior window trim alone still keeps the lift")


print("\n=== ALL PASS ===" if not fails else f"\n=== {len(fails)} FAIL ===")
import sys
sys.exit(1 if fails else 0)
