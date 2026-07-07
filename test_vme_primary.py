#!/usr/bin/env python3
"""Tests for VME primary (NIGHTSHIFT_VME_PRIMARY) — Release 2.

The measured wall total replaces vision ONLY on a full reliability verdict,
within the sanity ratio band, on non-CMU-heavy jobs. Everything else keeps
vision quantities (and the too-far-apart case files an RFI).

Run: python3 test_vme_primary.py
"""
import copy
import importlib.util as iu
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["NIGHTSHIFT_VME_PRIMARY"] = "1"
spec = iu.spec_from_file_location("T", os.path.join(HERE, "Takeoff_DIRECT.py"))
T = iu.module_from_spec(spec)
spec.loader.exec_module(T)

import vme_attribution as _vme  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def mk(vision_walls=10000, cmu=0):
    return {
        "_vme_pdf_paths": ["/tmp/fake.pdf"],
        "aggregated_totals": {"total_paintable_wall_sqft": vision_walls,
                              "total_cmu_wall_sqft": cmu},
        "floors": [{"floor_name": "First Floor", "rooms": [
            {"room_name": "Office", "in_scope": True,
             "materials": {"walls": "GYP"},
             "dimensions": {"wall_area_sqft": 6000}},
            {"room_name": "Storage", "in_scope": True,
             "materials": {"walls": "GYP"},
             "dimensions": {"wall_area_sqft": 4000}},
            {"room_name": "Elec", "in_scope": False,
             "materials": {"walls": "GYP"},
             "dimensions": {"wall_area_sqft": 500}},
        ]}],
    }


def fake_primary(result):
    def _fn(pdf_paths, analysis, default_height_ft=9.0):
        return result
    return _fn


orig = _vme.compute_vme_primary

# 1) reliable + in-band -> applied, rooms scaled, provenance recorded
_vme.compute_vme_primary = fake_primary(
    {"reliable": True, "reasons": [], "measured_wall_sf": 12000,
     "measured_wall_run_lf": 1300, "raw_lf": 1400, "basis": "run",
     "by_page": [{"page": 1}]})
a = T._apply_vme_primary(mk())
agg = a["aggregated_totals"]
r0 = a["floors"][0]["rooms"][0]["dimensions"]["wall_area_sqft"]
check("applied: total pinned to measured",
      agg["total_paintable_wall_sqft"] == 12000)
check("applied: rooms scaled x1.2", abs(r0 - 7200) < 1)
check("applied: out-of-scope room untouched",
      a["floors"][0]["rooms"][2]["dimensions"]["wall_area_sqft"] == 500)
check("applied: provenance measured",
      a.get("_wall_provenance") == "measured" and a.get("_vme_primary_applied"))

# 2) unreliable -> no-op
a = T._apply_vme_primary_result = None
_vme.compute_vme_primary = fake_primary(
    {"reliable": False, "reasons": ["page 3: 1 usable room anchors"]})
a = T._apply_vme_primary(mk())
check("unreliable: vision kept",
      a["aggregated_totals"]["total_paintable_wall_sqft"] == 10000
      and not a.get("_vme_primary_applied"))
check("unreliable: verdict stored for transparency",
      a.get("_vme_primary", {}).get("reliable") is False)

# 3) out-of-band ratio -> RFI, vision kept
_vme.compute_vme_primary = fake_primary(
    {"reliable": True, "reasons": [], "measured_wall_sf": 30000,
     "basis": "run", "by_page": [{"page": 1}]})
a = T._apply_vme_primary(mk())
check("out-of-band: vision kept",
      a["aggregated_totals"]["total_paintable_wall_sqft"] == 10000)
check("out-of-band: RFI filed",
      any(r.get("category") == "Wall quantity"
          for r in a.get("_pre_pricing_rfis", [])))

# 4) CMU-heavy -> no-op
_vme.compute_vme_primary = fake_primary(
    {"reliable": True, "reasons": [], "measured_wall_sf": 12000,
     "basis": "run", "by_page": [{"page": 1}]})
a = T._apply_vme_primary(mk(cmu=5000))
check("CMU-heavy: vision kept",
      a["aggregated_totals"]["total_paintable_wall_sqft"] == 10000)

# 5) flag off -> complete no-op
os.environ["NIGHTSHIFT_VME_PRIMARY"] = "0"
a = T._apply_vme_primary(mk())
check("flag off: no-op", "_vme_primary" not in a)
os.environ["NIGHTSHIFT_VME_PRIMARY"] = "1"

# 6) real reliability path on a real job (batch livestock): the basement
# page has no anchors, so the engine must refuse to take over.
_vme.compute_vme_primary = orig
BATCH = "/Users/stevenboyce/Desktop/_Code/NSAI/rider_batch_durable"
ls_pdf = os.path.join(BATCH, "livestock", "plans.pdf")
ls_res = os.path.join(BATCH, "livestock", "result_frp_on.json")
if os.path.exists(ls_pdf) and os.path.exists(ls_res):
    import json
    analysis = json.load(open(ls_res))["analysis"]
    p = _vme.compute_vme_primary([ls_pdf], analysis)
    check("livestock: verdict computed", isinstance(p, dict))
    check("livestock: not blindly reliable (unanchored basement page)",
          p.get("reliable") is False and p.get("reasons"))
else:
    print("  SKIP  livestock real-path checks (batch files not present)")

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
