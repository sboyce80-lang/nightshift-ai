#!/usr/bin/env python3
"""Same-floor room dedup (NIGHTSHIFT_SAME_FLOOR_ROOM_DEDUP): one
physical room read from several sheets must not stack scope. Fixture is
the literal Honey 2026-08-24 fresh-run room list (21 rooms, ~12 real)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import Takeoff_DIRECT as T  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def rm(name, wall, src="A1.1", num=None):
    return {"room_name": name, "room_number": num, "in_scope": True,
            "source_sheet": src,
            "dimensions": {"wall_area_sqft": float(wall)},
            "elements": {}}


def honey():
    rooms = [
        rm("Sales Floor", 2382, "A1.1"),
        rm("Office", 350, "R1.0"),
        rm("Prep", 524, "A1.1"),
        rm("BOH Storage", 1442, "A1.1"),
        rm("Freezer", 794, "R1.0"),
        rm("Cooler", 549, "R1.0"),
        rm("Restroom (Male)", 288, "A1.1"),
        rm("Cooler (108)", 584, "A1.1"),
        rm("Sales Floor / Main Retail Area", 1562, "PG61"),
        rm("Atrium / Entry", 660, "A1.1"),
        rm("106 Restroom", 342, "A1.1"),
        rm("107 Restroom", 342, "A1.1"),
        rm("101 Office", 558, "A1.1"),
        rm("105 Cooler", 639, "A1.1"),
        rm("108 Cooler", 378, "PG63"),
        rm("Freezer - Room 109", 522, "A1.1"),
        rm("109 Freezer", 396, "PG63"),
        rm("Corridor / Circulation", 450, "A1.1"),
        rm("Freezer", 331, "PG63"),
        rm("BOH Storage", 450, "PG61"),
        rm("Mechanical / Equipment Room", 396, "A1.1"),
    ]
    total = sum(r["dimensions"]["wall_area_sqft"] for r in rooms)
    return {"floors": [{"floor_name": "1st Floor", "rooms": rooms}],
            "aggregated_totals": {"total_paintable_wall_sqft": total}}


os.environ.pop("NIGHTSHIFT_SAME_FLOOR_ROOM_DEDUP", None)
a = T._dedup_same_floor_rooms(honey())
check(len(a["floors"][0]["rooms"]) == 21, "dedup ran with flag off")

os.environ["NIGHTSHIFT_SAME_FLOOR_ROOM_DEDUP"] = "1"
a = T._dedup_same_floor_rooms(honey())
names = [r["room_name"] for r in a["floors"][0]["rooms"]]
rec = a["_same_floor_room_dedup"]
print("   survivors:", names)
print("   record:", rec)

# Numbered identity merges: 108 Cooler + Cooler (108) collapse; 105
# Cooler stays its own room (two real coolers).
check(sum(1 for n in names if "105" in n) == 1, "105 cooler lost")
check(sum(1 for n in names if "108" in n) == 1,
      f"108-cooler pair not merged: {names}")
# 109 freezer pair merges.
check(sum(1 for n in names if "109" in n) == 1,
      f"109-freezer pair not merged: {names}")
# Restrooms 106/107 both stay; Male stays (tokens not a subset of a
# single group).
check(sum(1 for n in names if "Restroom" in n) == 3,
      f"restrooms wrong: {names}")
# Office folds into 101 Office (subset of exactly one group).
check("Office" not in names and "101 Office" in names,
      f"generic office not folded: {names}")
# Sales Floor subset-folds into the Main Retail Area group (one way or
# the other, exactly one sales-floor room survives).
check(sum(1 for n in names if "Sales Floor" in n) == 1,
      f"sales floor pair not merged: {names}")
# BOH Storage identical-token pair merges.
check(sum(1 for n in names if "BOH" in n) == 1,
      f"BOH pair not merged: {names}")
# Unique rooms untouched.
for keep in ("Prep", "Atrium / Entry", "Corridor / Circulation",
             "Mechanical / Equipment Room"):
    check(keep in names, f"unique room lost: {keep}")
# Aggregate mirrored down by the dropped SF.
agg = a["aggregated_totals"]["total_paintable_wall_sqft"]
check(agg < 13939 - 3000, f"aggregate barely moved: {agg}")
check(abs((13939 - rec["wall_sqft"]) - agg) < 0.6,
      f"aggregate mirror inconsistent: {agg} vs {rec}")
os.environ.pop("NIGHTSHIFT_SAME_FLOOR_ROOM_DEDUP", None)


print("— same-sheet guard (364 Main multifamily) —")
os.environ["NIGHTSHIFT_SAME_FLOOR_ROOM_DEDUP"] = "1"
multi = {"floors": [{"floor_name": "2nd Floor", "rooms": [
    rm("Bedroom", 400, "A102"), rm("Bedroom", 410, "A102"),
    rm("Bedroom", 390, "A102"), rm("Bedroom", 405, "A102"),
    rm("Living Room", 500, "A102"), rm("Living Room", 505, "A102"),
    rm("Bedroom", 400, "A502"),  # true cross-sheet re-read
]}], "aggregated_totals": {"total_paintable_wall_sqft": 3010.0}}
a = T._dedup_same_floor_rooms(multi)
names = [r["room_name"] for r in a["floors"][0]["rooms"]]
check(names.count("Bedroom") == 4,
      f"real same-sheet bedrooms merged: {names}")
check(names.count("Living Room") == 2,
      f"real same-sheet living rooms merged: {names}")
check(a["_same_floor_room_dedup"]["dropped"] == 1,
      f"cross-sheet re-read not dropped: {a['_same_floor_room_dedup']}")
os.environ.pop("NIGHTSHIFT_SAME_FLOOR_ROOM_DEDUP", None)

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
