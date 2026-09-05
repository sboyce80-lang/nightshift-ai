#!/usr/bin/env python3
"""room_geometry v2 (NIGHTSHIFT_ROOM_GEOMETRY_V2): adaptive enclosure +
merged-component splitting.

Locks in: v1 behavior is untouched when the options are off; a merged
two-room component splits by nearest anchor into plausible halves;
IDENTICAL anchor coordinates (template-instance rosters — homewood's 19
anchors on one point) are never fake-partitioned; the shadow builder
switches engines on the flag and keeps v1 output with it off.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fitz  # noqa: E402
import room_geometry as rg  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


PTS_PER_FT = 9.0  # 1/8" = 1'-0" -> 9 pt per ft


def two_room_plan(path, gap_ft=3.0):
    """Outer box 40ft x 20ft, center wall at x=20ft with a door OPENING at
    the wall's top end (a single short segment leaves the gap — nothing
    collinear for _bridge_gaps to bridge, unlike a mid-wall gap). Rooms:
    left 20x20, right 20x20 (nominal 400 SF each). Carries a real scale
    note so detect_scale_robust reads 9 pt/ft."""
    doc = fitz.open()
    page = doc.new_page(width=600, height=400)
    page.insert_text((110, 60), 'SCALE: 1/8" = 1\'-0"', fontsize=10)
    x0, y0, x1, y1 = 100, 100, 460, 280  # 40ft x 20ft at 9 pt/ft
    for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
                 ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        page.draw_line(fitz.Point(*a), fitz.Point(*b), width=2)
    xm = 280.0
    gap = gap_ft * PTS_PER_FT
    page.draw_line(fitz.Point(xm, y0 + gap), fitz.Point(xm, y1), width=2)
    doc.save(path)
    doc.close()


print("room geometry v2 checks")
plan = os.path.join(HERE, ".cache_test_rg_plan.pdf")
two_room_plan(plan)

left = ("LEFT", 190.0, 190.0)
right = ("RIGHT", 370.0, 190.0)

# v1: the 3ft door gap defeats the 0.5ft closing -> rooms merge
m1 = rg.measure_room_areas(plan, 0, [left, right], PTS_PER_FT)
s1 = {n: v["status"].split(":")[0] for n, v in m1["rooms"].items()}
check(s1 == {"LEFT": "measured", "RIGHT": "merged"},
      f"v1 baseline: door gap merges the pair: {s1}")

# v2 split: both rooms measured at plausible halves
m2 = rg.measure_room_areas(plan, 0, [left, right], PTS_PER_FT,
                           adaptive_close=True, split_merged=True)
areas = {n: v.get("area_sqft") for n, v in m2["rooms"].items()}
check(all(v["status"] == "measured" for v in m2["rooms"].values()),
      f"v2 splits the merged pair: {m2['rooms']}")
check(areas["LEFT"] and 300 <= areas["LEFT"] <= 500 and
      areas["RIGHT"] and 300 <= areas["RIGHT"] <= 500,
      f"split halves are plausible (~400 SF each): {areas}")
check(m2["rooms"]["RIGHT"].get("basis") == "split" or
      m2["rooms"]["LEFT"].get("basis") == "split",
      "split rooms record their basis")

# duplicate anchors are never fake-partitioned
dup_a = ("UNIT-1", 370.0, 190.0)
dup_b = ("UNIT-2", 370.0, 190.0)   # same point — template siblings
m3 = rg.measure_room_areas(plan, 0, [left, dup_a, dup_b], PTS_PER_FT,
                           adaptive_close=True, split_merged=True)
st3 = {n: v["status"].split(":")[0] for n, v in m3["rooms"].items()}
check(st3.get("UNIT-2") == "merged",
      f"identical-coordinate sibling stays merged: {st3}")
check(st3.get("LEFT") == "measured" and st3.get("UNIT-1") == "measured",
      "distinct anchors still split around the duplicates")

# v1 params untouched: same call without options reproduces v1 exactly
m1b = rg.measure_room_areas(plan, 0, [left, right], PTS_PER_FT)
check({n: v["status"] for n, v in m1b["rooms"].items()} ==
      {n: v["status"] for n, v in m1["rooms"].items()},
      "options-off call reproduces v1 statuses")

# shadow builder: flag routing
os.environ.pop("NIGHTSHIFT_ROOM_GEOMETRY_V2", None)
sh1 = rg.compute_room_geometry_shadow(
    [plan], anchors_by_page={(plan, 0): [left, right]})
check(sh1["engine"] == "room-geometry-shadow-v1",
      f"flag off: engine v1: {sh1['engine']}")
os.environ["NIGHTSHIFT_ROOM_GEOMETRY_V2"] = "1"
sh2 = rg.compute_room_geometry_shadow(
    [plan], anchors_by_page={(plan, 0): [left, right]})
check(sh2["engine"] == "room-geometry-shadow-v2",
      f"flag on: engine v2: {sh2['engine']}")
rooms2 = next((p.get("rooms") for p in sh2["pages"] if p.get("rooms")), {})
check(rooms2 and all(v["status"] == "measured" for v in rooms2.values()),
      f"v2 shadow measures both rooms: {rooms2}")
os.environ.pop("NIGHTSHIFT_ROOM_GEOMETRY_V2", None)

try:
    os.remove(plan)
except OSError:
    pass

print()
if fails:
    print(f"❌ {len(fails)} room geometry v2 check(s) failed")
    sys.exit(1)
print("✅ all room geometry v2 checks passed")
