#!/usr/bin/env python3
"""Paint schedule gate (M3, NIGHTSHIFT_PAINT_SCHEDULE_GATE): rooms whose
finish-schedule row designates a NON-painted wall system (panel, tile,
stainless, FRP) stop carrying whole-room wall paint. Only-reduce; RFI
ships the excluded SF. Honey 2026-08-24: KS 15,686 SF walls vs Rider's
4,580 — cooler/freezer panels and tiled restrooms are not paint scope."""
import os
import sys
import copy

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


print("— finish classification —")
check(T._wall_finish_class("PT01 (SW7562 Roman Column)") == "painted",
      "PT row misclassed")
check(T._wall_finish_class("Cooler Panel with Stainless Steel Closure")
      == "nonpaint", "panel row misclassed")
check(T._wall_finish_class("TL04 (Florida Tile Songbird 3x12)")
      == "nonpaint", "tile row misclassed")
check(T._wall_finish_class("PT02 + TL04 wainscot") == "painted",
      "mixed row must keep paint")
check(T._wall_finish_class("") == "unknown", "empty misclassed")

print("— gate (Honey shape) —")


def honey():
    return {
        "room_finish_schedule": [
            {"room_name": "Sales Floor / Main Retail Area",
             "room_number": None,
             "wall_finish": "PT01 (Sherwin Williams SW7562)"},
            {"room_name": "Cooler", "room_number": "105",
             "wall_finish": "Cooler Panel with Stainless Steel Closure"},
            {"room_name": "Freezer", "room_number": None,
             "wall_finish": "Cooler/Freezer Panel with Stainless Steel"},
            {"room_name": "Restroom (RR)", "room_number": "106/107",
             "wall_finish": "TL04 (Florida Tile Songbird 3x12)"},
            {"room_name": "Atrium / Entry", "room_number": None,
             "wall_finish": "PT02 (SW9176 Dresses Blue)"},
        ],
        "has_finish_schedule": True,
        "floors": [{"floor_name": "1st Floor", "rooms": [
            {"room_name": "Sales Floor", "in_scope": True,
             "dimensions": {"wall_area_sqft": 2871.0}},
            {"room_name": "Cooler", "in_scope": True,
             "dimensions": {"wall_area_sqft": 800.0}},
            {"room_name": "Freezer", "in_scope": True,
             "dimensions": {"wall_area_sqft": 400.0}},
            {"room_name": "Restroom (RR)", "in_scope": True,
             "dimensions": {"wall_area_sqft": 300.0}},
            {"room_name": "Office", "in_scope": True,
             "dimensions": {"wall_area_sqft": 500.0}},
        ]}],
        "aggregated_totals": {"total_paintable_wall_sqft": 4871.0},
    }


os.environ.pop("NIGHTSHIFT_PAINT_SCHEDULE_GATE", None)
a = T._enforce_paint_schedule_gate(honey())
check(a["aggregated_totals"]["total_paintable_wall_sqft"] == 4871.0,
      "gate ran with flag off")

os.environ["NIGHTSHIFT_PAINT_SCHEDULE_GATE"] = "1"
a = T._enforce_paint_schedule_gate(honey())
rooms = {r["room_name"]: r for fl in a["floors"] for r in fl["rooms"]}
check(rooms["Cooler"]["dimensions"]["wall_area_sqft"] == 0,
      "cooler panel walls kept")
check(rooms["Freezer"]["dimensions"]["wall_area_sqft"] == 0,
      "freezer panel walls kept")
check(rooms["Restroom (RR)"]["dimensions"]["wall_area_sqft"] == 0,
      "tiled restroom walls kept")
check(rooms["Sales Floor"]["dimensions"]["wall_area_sqft"] == 2871.0,
      "painted sales floor zeroed")
check(rooms["Office"]["dimensions"]["wall_area_sqft"] == 500.0,
      "unmatched room zeroed")
agg = a["aggregated_totals"]["total_paintable_wall_sqft"]
check(agg == 4871.0 - 1500.0, f"aggregate mirror wrong: {agg}")
check(a["_paint_schedule_gate"]["zeroed_rooms"] == 3,
      f"record wrong: {a['_paint_schedule_gate']}")

# All-painted schedule -> inert (no exclusion information).
allpt = honey()
for r in allpt["room_finish_schedule"]:
    r["wall_finish"] = "PT01"
b = T._enforce_paint_schedule_gate(copy.deepcopy(allpt))
check(b["_paint_schedule_gate"].get("noop") == "no_nonpaint_rows",
      f"all-painted schedule not inert: {b['_paint_schedule_gate']}")

# VME owns walls -> stand down.
v = honey()
v["_vme_authoritative"] = {"applied": True}
c = T._enforce_paint_schedule_gate(v)
check(c["_paint_schedule_gate"].get("noop") == "vme_owns_walls",
      f"gate fought VME: {c['_paint_schedule_gate']}")
check(c["aggregated_totals"]["total_paintable_wall_sqft"] == 4871.0,
      "VME-owned aggregate mutated")
os.environ.pop("NIGHTSHIFT_PAINT_SCHEDULE_GATE", None)

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
