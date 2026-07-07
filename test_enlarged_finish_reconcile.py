"""_reconcile_enlarged_wall_finish: propagate a confirmed non-paint wet-wall
finish (FRP/Trusscore/tile) from an enlarged detail sheet to the same room's
generic-GYP composite instance, dropping only its wall paint scope.

Livestock 2026-07-06: A-102 composite reads 1st-floor wet + storage rooms as
GYP/in-scope; A-110/A-111 enlarged plans show them as Trusscore. ~4,200 SF of
wall was painted that Rider excludes.
"""
import os
import copy
import Takeoff_DIRECT as T

_fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


def _room(name, sheet, walls, in_scope, wall_sf, floor_h=9.0):
    return {"room_name": name, "source_sheet": sheet, "in_scope": in_scope,
            "materials": {"walls": walls}, "dimensions": {"wall_area_sqft": wall_sf}}


def _analysis(rooms_by_floor):
    return {"floors": [{"floor_name": fn, "rooms": rs}
                       for fn, rs in rooms_by_floor],
            # billed quantities come from the aggregate, not the room sums —
            # the pass must only-reduce this by exactly the SF it removes
            "aggregated_totals": {"total_paintable_wall_sqft": 2200.0}}


def _in_scope_wall(a):
    return sum(T._num((r.get("dimensions") or {}).get("wall_area_sqft", 0))
               for fl in a["floors"] for r in fl["rooms"] if r.get("in_scope", True))


# A composite (A102) GYP/in-scope wet room + its enlarged (A111) Trusscore
# sibling on the SAME floor; plus a genuinely-painted GYP room on ANOTHER floor
# that shares the "bath" token and must stay painted.
BASE = _analysis([
    ("First Floor", [
        _room("Women's 104", "A102", "GYP", True, 1000),
        _room("Women's", "A111", "Trusscore", False, 0),
        _room("Family/ADA Bathroom 102", "A102", "GYP", True, 400),
        _room("Family / ADA Bathroom", "A111", "Trusscore", False, 0),
        _room("Office 113", "A102", "GYP", True, 500),          # painted, no FRP sibling
    ]),
    ("Second Floor", [
        _room("Bathroom 202", "A102", "GYP", True, 300),        # painted; token 'bath'
    ]),
])

print("enlarged-finish reconcile")

# --- flag OFF -> no-op ---
os.environ["NIGHTSHIFT_ENLARGED_FINISH_RECONCILE"] = "0"
a = copy.deepcopy(BASE)
T._reconcile_enlarged_wall_finish(a)
check("flag OFF is a no-op", _in_scope_wall(a) == 2200 and
      a.get("_enlarged_finish_reconcile") is None)

# --- flag ON ---
os.environ["NIGHTSHIFT_ENLARGED_FINISH_RECONCILE"] = "1"
a = copy.deepcopy(BASE)
T._reconcile_enlarged_wall_finish(a)
rec = a.get("_enlarged_finish_reconcile") or {}
check("demotes exactly the 2 FRP-sibling rooms", rec.get("rooms_demoted") == 2)
check("removes 1400 SF wall", rec.get("wall_sqft_removed") == 1400)

fl1 = {r["room_name"]: r for r in a["floors"][0]["rooms"]}
check("Women's 104 walls zeroed", fl1["Women's 104"]["dimensions"]["wall_area_sqft"] == 0)
check("Women's 104 finish -> Trusscore", fl1["Women's 104"]["materials"]["walls"] == "Trusscore")
check("Office 113 (no FRP sibling) untouched",
      fl1["Office 113"]["dimensions"]["wall_area_sqft"] == 500)

fl2 = {r["room_name"]: r for r in a["floors"][1]["rooms"]}
check("Bathroom 202 on other floor stays painted",
      fl2["Bathroom 202"]["dimensions"]["wall_area_sqft"] == 300)
check("RFI raised", any("wet-wall" in x.get("question", "")
                        for x in a.get("rfi_items", [])))
check("aggregate reduced by removed SF (2200-1400=800)",
      a["aggregated_totals"]["total_paintable_wall_sqft"] == 800.0)
check("record notes aggregate_reduced",
      (a.get("_enlarged_finish_reconcile") or {}).get("aggregate_reduced") is True)

# --- idempotent ---
w = _in_scope_wall(a)
T._reconcile_enlarged_wall_finish(a)
check("idempotent (second run no change)", _in_scope_wall(a) == w)

# --- fail-safe: no unpaintable sibling -> no-op even with flag on ---
safe = _analysis([("First Floor", [
    _room("Storage 101", "A102", "GYP", True, 300),
    _room("Storage", "A111", "GYP", False, 0)])])
T._reconcile_enlarged_wall_finish(safe)
check("no unpaintable sibling -> no demotion",
      (safe.get("_enlarged_finish_reconcile") or {}).get("rooms_demoted") == 0)

# --- _is_paintable_wall recognizes FRP/Trusscore (finish-schedule path) ---
# reach the nested helper via a tiny finish-schedule estimate is overkill; assert
# the keyword set directly through the module-level unpaintable list instead.
check("FRP in unpaintable finish keywords", "frp" in T._UNPAINTABLE_WALL_FINISH_KW)
check("trusscore in unpaintable finish keywords",
      "trusscore" in T._UNPAINTABLE_WALL_FINISH_KW)

print(f"\n=== {'PASS' if not _fails else str(len(_fails)) + ' FAILED: ' + ', '.join(_fails)} ===")
import sys
sys.exit(1 if _fails else 0)
