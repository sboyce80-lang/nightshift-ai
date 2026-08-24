#!/usr/bin/env python3
"""WC typical-match (NIGHTSHIFT_WC_TYPICAL_MATCH): unit instances inherit
their typical schedule row's WC designation by type signature, arming the
existing mixed-share promotion the typicals-keyed schedule never reached.

Homewood 2026-08-24: 166 rooms / 56.5k SF unmatched, WC stuck at 77.6k SF
of ~50%-of-wall guesses vs JW 136.6k; at the 0.8 mixed share the same
rooms land within ~9% of JW.
"""
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
    for k in ("NIGHTSHIFT_WC_TYPICAL_MATCH", "NIGHTSHIFT_WC_SCHEDULE_GATE",
              "NIGHTSHIFT_WC_SCHEDULE_AUTHORITATIVE",
              "NIGHTSHIFT_WC_MIXED_SHARE"):
        os.environ.pop(k, None)


print("— type signatures —")
sig = T._typical_signature
check(sig("Living/Sleeping - King Connector Suite")
      == ("king", "connector", "", False), f"row sig: "
      f"{sig('Living/Sleeping - King Connector Suite')}")
check(sig("King Connector Suite (Third Floor)")
      == ("king", "connector", "", False), "instance sig")
check(sig("King Studio B Suite - Main Living/Sleeping Area")[2] == "b",
      f"variant letter: "
      f"{sig('King Studio B Suite - Main Living/Sleeping Area')}")
check(sig("Bathroom - King Connector Suite")[3] is True, "bath part")
check(sig("Corridor (West Wing)") is None, "non-unit name matched")

print("— row matching —")
rows = [
    {"room_name": "Living/Sleeping - King Connector Suite",
     "room_number": "211/217 (typical)",
     "wall_finish": "WC 01 (Wallcovering), PT 03 (Paint)"},
    {"room_name": "Living/Sleeping - King Studio B Suite",
     "room_number": "305 (typical)",
     "wall_finish": "WC 01 (Wallcovering), PT 03 (Paint)"},
    {"room_name": "Bathroom - Queen Studio Suite",
     "room_number": "typ", "wall_finish": "WC 02"},
]
m = T._match_typical_row
check(m({"room_name": "King Connector Suite (Third Floor)"}, rows)
      is rows[0], "connector instance mismatched")
check(m({"room_name": "King Studio B (Second Floor)"}, rows)
      is rows[1], "studio-B instance mismatched")
check(m({"room_name": "Queen Studio Suite Bathroom"}, rows)
      is rows[2], "queen bath mismatched")
check(m({"room_name": "Queen Studio Suite (Living)"}, rows) is None,
      "queen living matched a king/bath row")
check(m({"room_name": "Meeting Room"}, rows) is None,
      "non-unit room matched")

print("— gate integration (Homewood shape) —")


def analysis():
    return {
        "room_finish_schedule": [
            {"room_name": "Living/Sleeping - King Connector Suite",
             "room_number": "211/217 (typical)",
             "wall_finish": "WC 01 (Wallcovering), PT 03 (Paint)"},
            {"room_name": "Living/Sleeping - King Studio Suite",
             "room_number": "220 (typical)",
             "wall_finish": "WC 01 (Wallcovering), PT 03 (Paint)"},
            {"room_name": "Corridor", "room_number": "106",
             "wall_finish": "WC 01"},
            {"room_name": "Office", "room_number": "108",
             "wall_finish": "PT 03"},
            {"room_name": "Storage", "room_number": "109",
             "wall_finish": "PT 03"},
            {"room_name": "Fitness", "room_number": "111",
             "wall_finish": "WC 01, PT 03"},
        ],
        "has_finish_schedule": True,
        "floors": [{"floor_name": "2nd Floor", "rooms": [
            {"room_name": "King Connector Suite (2nd Floor)",
             "in_scope": True,
             "dimensions": {"wall_area_sqft": 700.0},
             "elements": {"wallcovering_sqft": 350.0}},
            {"room_name": "King Studio Suite (2nd Floor North)",
             "in_scope": True,
             "dimensions": {"wall_area_sqft": 600.0},
             "elements": {"wallcovering_sqft": 300.0}},
            {"room_name": "Meeting Room", "in_scope": True,
             "dimensions": {"wall_area_sqft": 900.0},
             "elements": {"wallcovering_sqft": 400.0}},
        ]}],
        "aggregated_totals": {"total_wallcovering_sqft": 1050.0},
    }


def run(a):
    return T._enforce_wallcovering_schedule_gate(a)


_clear()
os.environ["NIGHTSHIFT_WC_SCHEDULE_GATE"] = "1"
os.environ["NIGHTSHIFT_WC_SCHEDULE_AUTHORITATIVE"] = "1"
os.environ["NIGHTSHIFT_WC_MIXED_SHARE"] = "0.8"
a = run(analysis())
r0 = a["floors"][0]["rooms"][0]["elements"]["wallcovering_sqft"]
check(r0 == 350.0,
      f"typical promotion ran with match flag OFF: {r0}")

_clear()
os.environ["NIGHTSHIFT_WC_SCHEDULE_GATE"] = "1"
os.environ["NIGHTSHIFT_WC_SCHEDULE_AUTHORITATIVE"] = "1"
os.environ["NIGHTSHIFT_WC_MIXED_SHARE"] = "0.8"
os.environ["NIGHTSHIFT_WC_TYPICAL_MATCH"] = "1"
a = run(analysis())
r0 = a["floors"][0]["rooms"][0]["elements"]["wallcovering_sqft"]
r1 = a["floors"][0]["rooms"][1]["elements"]["wallcovering_sqft"]
r2 = a["floors"][0]["rooms"][2]["elements"]["wallcovering_sqft"]
check(r0 == 560.0, f"connector not promoted to 0.8x700: {r0}")
check(r1 == 480.0, f"studio not promoted to 0.8x600: {r1}")
check(r2 == 400.0, f"non-unit room WC changed: {r2}")
agg = a["aggregated_totals"]["total_wallcovering_sqft"]
check(abs(agg - (1050.0 + 210.0 + 180.0)) < 0.01,
      f"aggregate not mirrored: {agg}")
_clear()

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
