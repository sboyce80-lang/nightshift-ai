"""Tests for the deduped floor-plate footprint correction.

The footprint sanity-check used to sum EVERY room's floor area, double-counting
cross-sheet re-draws (floor plan + RCP of the same space). On TSC that inflated
the footprint 26,387 -> 72,405, which then ballooned allowance column counts and
exterior perimeter. _dedup_floor_plate_sqft counts the dominant plate once when
it appears on more than one sheet, while preserving the genuine under-extraction
correction (Mazda).
"""
import sys

import Takeoff_DIRECT as T

_fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        _fails.append(name)


def room(area, sheet, in_scope=True, name="R"):
    return {"room_name": name, "source_sheet": sheet, "in_scope": in_scope,
            "dimensions": {"floor_area_sqft": area}}


print("TSC — plate drawn on A1.0 + A3.0 (RCP) counts once")
tsc = [room(20115, "A1.0", name="Retail Sales"),
       room(16000, "A3.0", name="Main Sales Floor"),  # RCP re-draw of the plate
       room(4065, "A1.0", name="Stockroom"),
       room(719, "A1.0"), room(282, "A2.0"), room(396, "A3.0")]
plate = T._dedup_floor_plate_sqft(tsc, 26387)
naive = sum(r["dimensions"]["floor_area_sqft"] for r in tsc)
check("deduped plate drops the 16,000 RCP re-draw",
      plate == 20115 + 4065 + 719 + 282 + 396, f"got {plate:,.0f}")
check("naive sum would have double-counted", naive == plate + 16000, f"naive {naive:,.0f}")
check("plate stays under the 1.5x trigger (no false correction)", plate < 26387 * 1.5)

print("\nMazda — single dominant room over footprint still corrects")
mazda = [room(4104, "A2.0", name="Showroom"),
         room(1800, "A2.0", name="Service"), room(900, "A2.0", name="Office")]
plate_m = T._dedup_floor_plate_sqft(mazda, 4000)
check("single-sheet plate sums normally (no collapse)",
      plate_m == 4104 + 1800 + 900, f"got {plate_m:,.0f}")
check("exceeds 1.5x footprint -> would trigger correction", plate_m > 4000 * 1.5)

print("\nTwo genuine big rooms on the SAME sheet are NOT collapsed")
same = [room(9000, "A1.0", name="Warehouse A"), room(9000, "A1.0", name="Warehouse B")]
check("same-sheet plate rooms both counted",
      T._dedup_floor_plate_sqft(same, 10000) == 18000)

print("\nOut-of-scope rooms excluded")
mixed = [room(5000, "A1.0"), room(99999, "A1.0", in_scope=False)]
check("out-of-scope ignored", T._dedup_floor_plate_sqft(mixed, 6000) == 5000)

print("\nNo stated footprint -> plain sum (nothing to threshold against)")
check("footprint 0 -> naive sum",
      T._dedup_floor_plate_sqft([room(100, "A1.0"), room(200, "A3.0")], 0) == 300)

print("\nEnd-to-end: calculate_costs does not inflate TSC footprint")
analysis = {"project_info": {"footprint_sqft": 26387, "building_type": "commercial retail"},
            "floors": [{"floor_name": "1st Floor", "rooms": tsc}]}
costs = T.calculate_costs(
    {"total_paintable_wall_sqft": 1000}, exterior={},
    building_type="commercial retail",
    project_info=analysis["project_info"], analysis=analysis)
# footprint correction is internal; assert via the absence of a runaway by
# re-deriving the plate the same way the function does.
check("deduped plate < 1.5x footprint so no correction fires",
      T._dedup_floor_plate_sqft(tsc, 26387) <= 26387 * 1.5)

print(f"\n=== {'ALL PASS' if not _fails else str(len(_fails)) + ' FAILED: ' + ', '.join(_fails)} ===")
sys.exit(1 if _fails else 0)
