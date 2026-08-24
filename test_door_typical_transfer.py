#!/usr/bin/env python3
"""Door typical transfer (NIGHTSHIFT_DOOR_TYPICAL_TRANSFER): unit
instances inherit door counts from their unit typical when they carry
none. Homewood 2026-08-24: typicals carry 1-9 doors/type, instances
carry zero -> 116 doors priced vs JW's 393."""
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


def homewood():
    return {
        "floors": [
            {"floor_name": "Typical King Studio Suite (A4.02)", "rooms": [
                {"room_name": "King Studio Suite - Living/Sleeping",
                 "elements": {"doors_full_paint": 2, "doors_hm_panel": 0}},
                {"room_name": "King Studio Suite - Bathroom",
                 "elements": {"doors_full_paint": 1}},
            ]},
            {"floor_name": "Typical King One Bedroom Suite (A4.03)",
             "rooms": [
                {"room_name": "King One Bedroom Suite - Sleeping/Living",
                 "elements": {"doors_full_paint": 3}},
             ]},
            {"floor_name": "2nd Floor", "rooms": [
                {"room_name": "King Studio Suite (2nd Floor North)",
                 "elements": {"wallcovering_sqft": 300}},
                {"room_name": "King Studio Suite (2nd Floor South)",
                 "elements": {}},
                {"room_name": "King Studio Suite Bathroom (2nd Floor)",
                 "elements": {}},
                {"room_name": "King One Bedroom Suite (2nd Floor)",
                 "elements": {}},
                {"room_name": "Corridor (2nd Floor)",
                 "elements": {"doors_full_paint": 4}},
                {"room_name": "King Studio Suite (2nd Floor East)",
                 "elements": {"doors_full_paint": 2}},
            ]},
        ],
        "aggregated_totals": {"total_doors_full_paint": 12.0,
                              "total_doors_hm_panel": 0.0},
    }


os.environ.pop("NIGHTSHIFT_DOOR_TYPICAL_TRANSFER", None)
a = T._transfer_typical_doors(homewood())
check(a["aggregated_totals"]["total_doors_full_paint"] == 12.0,
      "transfer ran with flag off")

os.environ["NIGHTSHIFT_DOOR_TYPICAL_TRANSFER"] = "1"
a = T._transfer_typical_doors(homewood())
# studio typical = 2 fp living + 1 fp bath = 3; one-bdr typical = 3.
# Bare instances: studio North (3), studio South (3), one-bdr (3) = +9.
# Bath instance skipped; corridor untouched; East already has 2 -> kept.
agg = a["aggregated_totals"]["total_doors_full_paint"]
check(agg == 21.0, f"aggregate wrong: {agg} (want 12+9)")
rooms = {r["room_name"]: r for fl in a["floors"] for r in fl["rooms"]}
check(rooms["King Studio Suite (2nd Floor North)"]["elements"]
      ["doors_full_paint"] == 3, "north studio not filled")
check(rooms["King One Bedroom Suite (2nd Floor)"]["elements"]
      ["doors_full_paint"] == 3, "one-bdr not filled")
check(rooms["King Studio Suite Bathroom (2nd Floor)"]["elements"]
      .get("doors_full_paint") is None, "bath instance filled (would "
      "double the unit's doors)")
check(rooms["King Studio Suite (2nd Floor East)"]["elements"]
      ["doors_full_paint"] == 2, "instance with own count overwritten")
check(rooms["Corridor (2nd Floor)"]["elements"]["doors_full_paint"] == 4,
      "non-unit room touched")
check(a["_door_typical_transfer"]["filled_instances"] == 3,
      f"record wrong: {a['_door_typical_transfer']}")

# No typicals -> noop.
plain = homewood()
plain["floors"] = plain["floors"][2:]
b = T._transfer_typical_doors(copy.deepcopy(plain))
check(b["_door_typical_transfer"].get("noop") == "no_typicals",
      f"noop missing: {b['_door_typical_transfer']}")
os.environ.pop("NIGHTSHIFT_DOOR_TYPICAL_TRANSFER", None)

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
