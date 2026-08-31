#!/usr/bin/env python3
"""Schedule scope clip: wainscot height bands + scheduled-rooms-only.

Honey K=3 round 1 (2026-08-27): walls 15,686 SF vs Rider's 4,580 —
Rider bids only the 6 scheduled areas, and the scheduled areas
themselves carry paint only ABOVE the 4' tile wainscot ('PT01 above
4'-0\" AFF; TL04 up to 4'-0\"'). Both are hard numbers off the finish
schedule. Locks in: band parsing, only-reduce band split, unscheduled
zeroing behind its own per-customer flag, match-coverage stand-down,
missing-height RFI, VME finish-clip record."""
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


def _clear():
    for k in ("NIGHTSHIFT_SCHEDULE_HEIGHT_SPLIT",
              "NIGHTSHIFT_SCHEDULE_SCOPE_AUTHORITATIVE"):
        os.environ.pop(k, None)


print("— band parsing —")
band = T._schedule_band_ft
check(band("PT01 (SW7562) above 4'-0\" AFF; TL04 (tile) up to 4'-0\" AFF")
      == 4.0, "classic wainscot row parses to 4.0")
check(band("TL04 tile up to 4'-0\"; PT01 above 4'-0\" AFF") == 4.0,
      "order-agnostic")
check(band("GYP BD PNT") == 0.0, "plain paint row has no band")
check(band("Cooler Panel with Stainless Steel to 6\" above ceiling")
      == 0.0, "non-paint row has no band")
check(band("PT01 above wainscot; WD01 wainscot up to 3'") == 3.0,
      "explicit wainscot band parses even with wood material")


def honey():
    return {
        "room_finish_schedule": [
            {"room_name": "Sales Floor / Main Retail Area",
             "room_number": None,
             "wall_finish": "PT01 above 4'-0\" AFF; TL04 (tile) up to "
                            "4'-0\" AFF"},
            {"room_name": "Restroom (RR)", "room_number": "106",
             "wall_finish": "TL04 tile up to 4'-0\"; PT01 above 4'-0\""},
            {"room_name": "Atrium / Entry", "room_number": None,
             "wall_finish": "PT02 accent, WD01 milled wood"},
            {"room_name": "Cooler", "room_number": "105",
             "wall_finish": "Cooler Panel with Stainless Steel"},
            {"room_name": "Freezer", "room_number": "109",
             "wall_finish": "Cooler Panel with Stainless Steel"},
            {"room_name": "Office", "room_number": "101",
             "wall_finish": "GYP BD PNT"},
        ],
        "has_finish_schedule": True,
        "floors": [{"floor_name": "1st Floor", "rooms": [
            {"room_name": "Sales Floor / Main Retail Area",
             "in_scope": True,
             "dimensions": {"wall_area_sqft": 1568.0,
                            "ceiling_height_feet": 10.0}, "elements": {}},
            {"room_name": "Sales Floor", "in_scope": True,
             "dimensions": {"wall_area_sqft": 2398.0,
                            "ceiling_height_feet": 10.0}, "elements": {}},
            {"room_name": "Atrium / Entry", "in_scope": True,
             "dimensions": {"wall_area_sqft": 638.0,
                            "ceiling_height_feet": 12.0}, "elements": {}},
            {"room_name": "BOH Storage", "in_scope": True,
             "dimensions": {"wall_area_sqft": 1408.0,
                            "ceiling_height_feet": 10.0}, "elements": {}},
            {"room_name": "Restroom (Men)", "in_scope": True,
             "dimensions": {"wall_area_sqft": 288.0}, "elements": {}},
        ]}],
        "aggregated_totals": {"total_paintable_wall_sqft": 6300.0},
        "notes": [],
    }


print("\n— height split only —")
_clear()
os.environ["NIGHTSHIFT_SCHEDULE_HEIGHT_SPLIT"] = "1"
a = T._apply_schedule_scope_clip(honey())
rec = a["_schedule_scope_clip"]
rooms = a["floors"][0]["rooms"]
# exact-name room 1568 @ 10' -> -627.2; token-subset 'Sales Floor' 2398
# -> -959.2; atrium matched but no band; BOH unmatched (kept — scope
# rule off); restroom matched by token subset but NO height -> RFI
check(rooms[0]["dimensions"]["wall_area_sqft"] == 940.8,
      f"exact-match band split: {rooms[0]['dimensions']}")
check(rooms[1]["dimensions"]["wall_area_sqft"] == 1438.8,
      f"token-subset band split: {rooms[1]['dimensions']}")
check(rooms[3]["dimensions"]["wall_area_sqft"] == 1408.0,
      "unmatched room untouched with scope rule off")
check(rec["band_sqft"] == 1586.4 and rec["unscheduled_sqft"] == 0,
      f"record wrong: {rec}")
check("Restroom (Men)" in rec["no_height_rooms"],
      f"missing-height room must RFI: {rec}")
check(a["aggregated_totals"]["total_paintable_wall_sqft"]
      == 6300.0 - 1586.4, f"agg not clipped: {a['aggregated_totals']}")

print("\n— scheduled-rooms-only added —")
_clear()
os.environ["NIGHTSHIFT_SCHEDULE_HEIGHT_SPLIT"] = "1"
os.environ["NIGHTSHIFT_SCHEDULE_SCOPE_AUTHORITATIVE"] = "1"
a = T._apply_schedule_scope_clip(honey())
rec = a["_schedule_scope_clip"]
check(rec["unscheduled_sqft"] == 1408.0
      and rec["unscheduled_rooms"] == ["BOH Storage"],
      f"unscheduled room not excluded: {rec}")
check(a["floors"][0]["rooms"][3]["dimensions"]["wall_area_sqft"] == 0,
      "unscheduled room walls must zero")
check(rec["total_clip_sqft"] == 1586.4 + 1408.0,
      f"total clip wrong: {rec}")
check(any("Paint Scope" == r.get("category")
          for r in (a.get("_pre_pricing_rfis") or [])),
      "scope RFI missing")

print("\n— match-coverage stand-down —")
_clear()
os.environ["NIGHTSHIFT_SCHEDULE_SCOPE_AUTHORITATIVE"] = "1"
bad = honey()
for r in bad["room_finish_schedule"]:
    r["room_name"] = "Zone " + str(r.get("room_number") or "X")
    r["room_number"] = None
a = T._apply_schedule_scope_clip(bad)
check("noop" in a["_schedule_scope_clip"],
      f"mismatched naming must stand down: {a['_schedule_scope_clip']}")
check(a["floors"][0]["rooms"][3]["dimensions"]["wall_area_sqft"]
      == 1408.0, "stand-down must leave rooms untouched")

print("\n— flags off: inert —")
_clear()
a = T._apply_schedule_scope_clip(honey())
check(a.get("_schedule_scope_clip") is None, "off must be inert")

_clear()
print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ all schedule scope clip checks passed")
