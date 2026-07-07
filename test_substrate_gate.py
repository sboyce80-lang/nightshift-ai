#!/usr/bin/env python3
"""Tests for the substrate provenance gate (NIGHTSHIFT_SUBSTRATE_GATE).

Release 3 of the 2026-07 accuracy plan: fail-safe review flags for
(1) large rooms whose wall substrate is "(assumed)" and
(2) service/industrial rooms billing large painted-GYP ceilings.
The gate must NEVER change quantities — RFI + manual-review flags only.

Run: python3 test_substrate_gate.py
"""
import copy
import importlib.util as iu
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["NIGHTSHIFT_SUBSTRATE_GATE"] = "1"
spec = iu.spec_from_file_location("T", os.path.join(HERE, "Takeoff_DIRECT.py"))
T = iu.module_from_spec(spec)
spec.loader.exec_module(T)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def mk(rooms):
    return {"floors": [{"floor_name": "First Floor", "rooms": rooms}]}


def room(name, walls="GYP", ceiling="ACT", wall_sf=800, clg_sf=400,
         in_scope=True):
    return {"room_name": name, "in_scope": in_scope,
            "materials": {"walls": walls, "ceiling": ceiling},
            "dimensions": {"wall_area_sqft": wall_sf,
                           "ceiling_area_sqft": clg_sf}}


# (1) assumed wall substrate, large room -> RFI, no quantity change
a = mk([room("Sales Floor", walls="GYP (assumed)", wall_sf=9000),
        room("Office", walls="GYP", wall_sf=400)])
before = json.dumps(a["floors"])
out = T._substrate_provenance_gate(copy.deepcopy(a))
rfis = out.get("_pre_pricing_rfis", [])
check("assumed large wall -> 1 RFI", len(rfis) == 1 and
      rfis[0]["category"] == "Wall substrate")
check("small/confirmed rooms not flagged",
      "Office" not in rfis[0]["question"])
out2 = T._substrate_provenance_gate(copy.deepcopy(a))
check("quantities untouched", json.dumps(out2["floors"]) == before)

# small assumed room does NOT fire
a = mk([room("Closet", walls="GYP (assumed)", wall_sf=200)])
out = T._substrate_provenance_gate(copy.deepcopy(a))
check("small assumed wall ignored", not out.get("_pre_pricing_rfis"))

# (2) service-area painted GYP ceiling -> RFI + manual review
a = mk([room("Service Department", ceiling="GYP", clg_sf=19488),
        room("Break Room", ceiling="GYP", clg_sf=300)])
out = T._substrate_provenance_gate(copy.deepcopy(a))
rfis = out.get("_pre_pricing_rfis", [])
check("service GYP ceiling -> RFI", len(rfis) == 1 and
      rfis[0]["category"] == "Ceiling type")
check("service GYP ceiling -> manual review",
      out.get("manual_review_required") is True)
check("break room not flagged", "Break Room" not in rfis[0]["question"])

# service room with ACT ceiling does NOT fire
a = mk([room("Service Department", ceiling="ACT", clg_sf=19488)])
out = T._substrate_provenance_gate(copy.deepcopy(a))
check("service ACT ceiling ignored", not out.get("_pre_pricing_rfis"))

# out-of-scope rooms ignored
a = mk([room("Service Department", ceiling="GYP", clg_sf=19488,
             in_scope=False)])
out = T._substrate_provenance_gate(copy.deepcopy(a))
check("out-of-scope ignored", not out.get("_pre_pricing_rfis"))

# flag off -> complete no-op
os.environ["NIGHTSHIFT_SUBSTRATE_GATE"] = "0"
a = mk([room("Service Department", ceiling="GYP", clg_sf=19488),
        room("Sales Floor", walls="CMU (assumed)", wall_sf=9000)])
out = T._substrate_provenance_gate(copy.deepcopy(a))
check("flag off -> no-op", not out.get("_pre_pricing_rfis") and
      not out.get("manual_review_required"))
os.environ["NIGHTSHIFT_SUBSTRATE_GATE"] = "1"

# malformed input safe
check("malformed input -> unchanged",
      T._substrate_provenance_gate(None) is None)

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
