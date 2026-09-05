#!/usr/bin/env python3
"""Pre-delivery reconciliation suite (delivery_verification.py).

One fixture per check, exercising the pass AND flag paths — each fixture
models its historical miss in miniature:

  totals_reconcile      the $11,904 ceiling phantom (unledgered write)
  schedule_vs_instance  the Northwell 78-door / 31%-of-bid schedule gap
  cross_sheet_dedup     the 88 Academy stairwell priced 6x
  read_then_discarded   the Toyota deck / Profeta A-207 inert authority
  white_label           the 11-reference Profeta "Rider" leak
  page_coverage         the Toyota 9-failed-pages class
  confidence_floor      a held/low-confidence job must never read clean

Plus the chain hook contract: flag OFF = byte identity; flag ON stores
the record WITHOUT touching quantities (byte-compare minus the record);
HOLD escalates flags into manual review.
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("CLAUDE_API_KEY", "x")
os.environ.pop("NIGHTSHIFT_DELIVERY_VERIFICATION", None)
os.environ.pop("NIGHTSHIFT_DELIVERY_VERIFICATION_HOLD", None)

import delivery_verification as dv  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  X {msg}")
    else:
        print("  ok " + msg.split(":")[0])


def by_id(rec, cid):
    return next(c for c in rec["checks"] if c["id"] == cid)


def room(num, name="Room", floor_area=100, ceiling_painted=False,
         in_scope=True, wall=300):
    return {"room_number": num, "room_name": name, "in_scope": in_scope,
            "dimensions": {"floor_area_sqft": floor_area,
                           "wall_area_sqft": wall,
                           "ceiling_height_feet": 9},
            "materials": {"ceiling_painted": ceiling_painted}}


def base_analysis():
    return {
        "aggregated_totals": {"total_paintable_wall_sqft": 5000,
                              "total_paintable_ceiling_sqft": 1200,
                              "total_doors_full_paint": 20,
                              "total_doors_hm_panel": 4},
        "floors": [{"floor_name": "Level 1",
                    "rooms": [room("101"), room("102")]},
                   {"floor_name": "Level 2",
                    "rooms": [room("201"), room("202")]}],
        "notes": ["clean fixture"],
        "_ledger_reconcile": {"unledgered": {}, "n_keys": 0,
                              "max_gap_pct": 0.0},
        "calibrated_confidence": {"confidence_level": 62},
    }


print("delivery verification checks")

# ---------------------------------------------------------------------
# 1) totals_reconcile — the $11,904 ceiling phantom in miniature: a key
#    moved outside the adjustment ledger.
a = base_analysis()
rec = dv.run_delivery_checks(a)
check(by_id(rec, "totals_reconcile")["status"] == "pass",
      "totals_reconcile pass: contiguous ledger reads clean")

a = base_analysis()
a["_ledger_reconcile"] = {
    "unledgered": {"total_paintable_ceiling_sqft": {
        "gaps": [{"where": "after final ledgered stage",
                  "expected": 0.0, "saw": 11904.0, "gap": 11904.0}],
        "worst_gap_pct": 100.0}},
    "n_keys": 1, "max_gap_pct": 100.0}
c = by_id(dv.run_delivery_checks(a), "totals_reconcile")
check(c["status"] == "flag",
      "totals_reconcile flag: unledgered ceiling write is caught")
check("total_paintable_ceiling_sqft" in c["detail"],
      "totals_reconcile flag detail names the phantom key")

a = base_analysis()
del a["_ledger_reconcile"]
check(by_id(dv.run_delivery_checks(a),
            "totals_reconcile")["status"] == "skip",
      "totals_reconcile skip: no ledger record does not fake a pass")

# ---------------------------------------------------------------------
# 2) schedule_vs_instance — Northwell in miniature: the door schedule
#    lists 78, the priced aggregate carries far fewer.
a = base_analysis()
a["_door_ledger"] = {"mode": "schedule", "count": 24, "full_paint": 20,
                     "hm_panel": 4}
check(by_id(dv.run_delivery_checks(a),
            "schedule_vs_instance")["status"] == "pass",
      "schedule_vs_instance pass: schedule and priced doors agree")

a = base_analysis()
a["_door_ledger"] = {"mode": "schedule", "count": 78, "full_paint": 60,
                     "hm_panel": 18}
c = by_id(dv.run_delivery_checks(a), "schedule_vs_instance")
check(c["status"] == "flag",
      "schedule_vs_instance flag: 78 scheduled vs 24 priced diverges")
check("78" in c["detail"] and "24" in c["detail"],
      "schedule_vs_instance flag detail carries both counts")

a = base_analysis()
check(by_id(dv.run_delivery_checks(a),
            "schedule_vs_instance")["status"] == "skip",
      "schedule_vs_instance skip: no parsed schedule, nothing to check")

a = base_analysis()
a["_door_ledger"] = {"mode": "symbols", "count": 78}
check(by_id(dv.run_delivery_checks(a),
            "schedule_vs_instance")["status"] == "skip",
      "schedule_vs_instance skip: diagnostic symbol mode never rules")

# ---------------------------------------------------------------------
# 3) cross_sheet_dedup — 88 Academy in miniature: one stairwell number
#    in scope on 6 floors.
a = base_analysis()
check(by_id(dv.run_delivery_checks(a),
            "cross_sheet_dedup")["status"] == "pass",
      "cross_sheet_dedup pass: unique room numbers read clean")

a = base_analysis()
a["floors"] = [{"floor_name": f"Level {i}",
                "rooms": [room("ST-1", "Stairwell 1")]}
               for i in range(1, 7)]
c = by_id(dv.run_delivery_checks(a), "cross_sheet_dedup")
check(c["status"] == "flag",
      "cross_sheet_dedup flag: stairwell priced 6x across floors")
check("ST-1" in c["detail"] and "x6" in c["detail"],
      "cross_sheet_dedup flag detail names the room and the count")

a = base_analysis()
a["floors"] = [{"floor_name": f"L{i}",
                "rooms": [room("ST-1", in_scope=False)]}
               for i in range(1, 7)]
check(by_id(dv.run_delivery_checks(a),
            "cross_sheet_dedup")["status"] == "skip",
      "cross_sheet_dedup: out-of-scope duplicates are not offenders")

# ---------------------------------------------------------------------
# 4) read_then_discarded — Toyota / A-207 in miniature: an extracted
#    authority contributes zero, silently.
a = base_analysis()
a["room_finish_schedule"] = [{"room_number": str(100 + i),
                              "room_name": f"Room {i}"}
                             for i in range(8)]
c = by_id(dv.run_delivery_checks(a), "read_then_discarded")
check(c["status"] == "flag",
      "read_then_discarded flag: 8-row schedule with no consumer record")
check("no schedule-consumer record" in c["detail"],
      "read_then_discarded flag detail says what went inert")

a["_wc_schedule_gate"] = {"numbered_rows": 8, "promoted_sqft": 120.0}
check(by_id(dv.run_delivery_checks(a),
            "read_then_discarded")["status"] == "pass",
      "read_then_discarded pass: a real consumer record clears it")

a["_wc_schedule_gate"] = {"noop": "schedule_too_thin"}
check(by_id(dv.run_delivery_checks(a),
            "read_then_discarded")["status"] == "flag",
      "read_then_discarded: a noop stand-down record is NOT consumption")

# ceiling variant: rooms carry painted-ceiling area, priced ceilings 0.
a = base_analysis()
a["aggregated_totals"]["total_paintable_ceiling_sqft"] = 0
a["floors"] = [{"floor_name": "Roof Deck", "rooms": [
    room("D1", "Deck Lounge", floor_area=400, ceiling_painted=True),
    room("D2", "Deck Bar", floor_area=350, ceiling_painted=True)]}]
c = by_id(dv.run_delivery_checks(a), "read_then_discarded")
check(c["status"] == "flag",
      "read_then_discarded flag: 750 SF painted-ceiling rooms, 0 priced")

a["aggregated_totals"]["total_paintable_ceiling_sqft"] = 750
check(by_id(dv.run_delivery_checks(a),
            "read_then_discarded")["status"] != "flag",
      "read_then_discarded cleared: priced ceilings drop the ceiling flag")

# door-ledger inert variant: parsed, never applied, never demoted.
a = base_analysis()
a["_door_ledger"] = {"mode": "schedule", "count": 24, "full_paint": 20,
                     "hm_panel": 4}
c = by_id(dv.run_delivery_checks(a), "read_then_discarded")
check(c["status"] == "flag" and "neither applied nor demoted"
      in c["detail"],
      "read_then_discarded flag: mode-A parse that went inert")
a["notes"].append("[Door Ledger] 24 doors from parsed door schedule "
                  "(A-601); extraction had 24. source: ledger")
check(by_id(dv.run_delivery_checks(a),
            "read_then_discarded")["status"] == "pass",
      "read_then_discarded pass: an applied ledger is not inert")

# ---------------------------------------------------------------------
# 5) white_label — Profeta leak in miniature: a competitor name in a
#    customer-facing note.
a = base_analysis()
check(by_id(dv.run_delivery_checks(a),
            "white_label")["status"] == "pass",
      "white_label pass: clean result JSON")

a = base_analysis()
a["notes"].append("Touch-up of Rider Painting's own work is included.")
c = by_id(dv.run_delivery_checks(a), "white_label")
check(c["status"] == "flag",
      "white_label flag: 'Rider Painting' in a note is caught")
check("notes/" in c["detail"],
      "white_label flag detail carries the offending JSON path")

a = base_analysis()
a["golden_source"] = {"takeoff_by": "Rider Painting golden set"}
check(by_id(dv.run_delivery_checks(a),
            "white_label")["status"] == "pass",
      "white_label: golden/test paths are exempt")

a = base_analysis()
costs = {"gc_scope_of_work": "— Will, Senior Estimator, JW Painting"}
c = by_id(dv.run_delivery_checks(a, costs=costs), "white_label")
check(c["status"] == "flag" and "jw painting" in c["detail"],
      "white_label flag: the costs dict is grepped too")

# ---------------------------------------------------------------------
# 6) page_coverage — Toyota in miniature: failed pages, worst when they
#    look like finish plans.
a = base_analysis()
a["coverage"] = {"total_pages": 40,
                 "totals": {"measured": 38, "excluded": 2, "failed": 0,
                            "degraded": 0, "unaccounted": 0},
                 "files": [{"file": "plans.pdf", "failed_pages": []}]}
check(by_id(dv.run_delivery_checks(a),
            "page_coverage")["status"] == "pass",
      "page_coverage pass: healthy coverage reads clean")

a["coverage"] = {"total_pages": 40,
                 "totals": {"measured": 31, "excluded": 0, "failed": 9,
                            "degraded": 0, "unaccounted": 0},
                 "files": [{"file": "plans.pdf",
                            "failed_pages": [3, 4, 5, 6, 7, 8, 9, 10,
                                             11]}]}
check(by_id(dv.run_delivery_checks(a),
            "page_coverage")["status"] == "flag",
      "page_coverage flag: 9/40 failed pages (22%) is missing scope")

a["coverage"] = {"total_pages": 40,
                 "totals": {"measured": 38, "excluded": 0, "failed": 2,
                            "degraded": 0, "unaccounted": 0},
                 "files": [{"file": "A601_finish_plans.pdf",
                            "failed_pages": [1, 2]}]}
c = by_id(dv.run_delivery_checks(a), "page_coverage")
check(c["status"] == "flag" and "A601_finish_plans.pdf" in c["detail"],
      "page_coverage flag: failed finish-plan sheets flag below 20%")

del a["coverage"]
check(by_id(dv.run_delivery_checks(a),
            "page_coverage")["status"] == "skip",
      "page_coverage skip: no ledger record, nothing to certify")

# ---------------------------------------------------------------------
# 7) confidence_floor — a held job must never read clean.
a = base_analysis()
check(by_id(dv.run_delivery_checks(a),
            "confidence_floor")["status"] == "pass",
      "confidence_floor pass: confident, unheld job reads clean")

a = base_analysis()
a["calibrated_confidence"] = {"confidence_level": 24}
c = by_id(dv.run_delivery_checks(a), "confidence_floor")
check(c["status"] == "flag" and "24" in c["detail"],
      "confidence_floor flag: confidence 24 < 30 carries through")

a = base_analysis()
a["manual_review_required"] = True
a["manual_review_reason"] = "3 page(s) could not be analyzed"
c = by_id(dv.run_delivery_checks(a), "confidence_floor")
check(c["status"] == "flag" and "could not be analyzed" in c["detail"],
      "confidence_floor flag: an existing hold carries through")

# ---------------------------------------------------------------------
# Read-only contract: the suite never mutates the analysis.
a = base_analysis()
a["notes"].append("signed by Rider Painting")   # force flags
a["_door_ledger"] = {"mode": "schedule", "count": 78}
before = json.dumps(a, sort_keys=True, default=str)
rec = dv.run_delivery_checks(a)
check(json.dumps(a, sort_keys=True, default=str) == before,
      "read-only: run_delivery_checks left the analysis byte-identical")
check(rec["n_flags"] >= 2 and isinstance(rec["checks"], list)
      and len(rec["checks"]) == 7,
      "shape: 7 checks and an honest n_flags count")

# A hostile analysis can never raise.
rec = dv.run_delivery_checks({"floors": "not-a-list",
                              "_ledger_reconcile": "garbage",
                              "coverage": {"total_pages": "x"},
                              "notes": [object()]})
check(isinstance(rec, dict) and len(rec["checks"]) == 7,
      "robustness: hostile analysis yields a record, never a crash")

# ---------------------------------------------------------------------
# Chain hook: OFF = byte identity; ON stores the record and touches
# nothing else; HOLD escalates.
import Takeoff_DIRECT as T  # noqa: E402

def hook_fixture():
    a = base_analysis()
    a["notes"].append("prepared by Rider Painting")     # 1 honest flag
    return a

os.environ.pop("NIGHTSHIFT_DELIVERY_VERIFICATION", None)
off1 = T.build_priced_takeoff(hook_fixture())
off2 = T.build_priced_takeoff(hook_fixture())
check("_delivery_verification" not in off1,
      "hook OFF: no record is stored")
check(json.dumps(off1, sort_keys=True, default=str)
      == json.dumps(off2, sort_keys=True, default=str),
      "hook OFF: byte-identical replays")

os.environ["NIGHTSHIFT_DELIVERY_VERIFICATION"] = "1"
on = T.build_priced_takeoff(hook_fixture())
check(isinstance(on.get("_delivery_verification"), dict)
      and on["_delivery_verification"]["n_flags"] >= 1,
      "hook ON: record stored with the honest white-label flag")
check(not on.get("manual_review_required"),
      "hook ON without HOLD: flags record, they do not hold")
on_stripped = {k: v for k, v in on.items()
               if k != "_delivery_verification"}
check(json.dumps(on_stripped, sort_keys=True, default=str)
      == json.dumps(off1, sort_keys=True, default=str),
      "hook ON: minus the record, output is byte-identical to OFF")

os.environ["NIGHTSHIFT_DELIVERY_VERIFICATION_HOLD"] = "1"
held = T.build_priced_takeoff(hook_fixture())
check(held.get("manual_review_required") is True,
      "HOLD: n_flags>0 sets manual_review_required")
check("white_label" in (held.get("manual_review_reason") or ""),
      "HOLD: the reason names the flagged check ids")
os.environ.pop("NIGHTSHIFT_DELIVERY_VERIFICATION", None)
os.environ.pop("NIGHTSHIFT_DELIVERY_VERIFICATION_HOLD", None)

clean = base_analysis()
os.environ["NIGHTSHIFT_DELIVERY_VERIFICATION"] = "1"
os.environ["NIGHTSHIFT_DELIVERY_VERIFICATION_HOLD"] = "1"
held_clean = T.build_priced_takeoff(copy.deepcopy(clean))
check(not held_clean.get("manual_review_required"),
      "HOLD: a clean job is not held")
os.environ.pop("NIGHTSHIFT_DELIVERY_VERIFICATION", None)
os.environ.pop("NIGHTSHIFT_DELIVERY_VERIFICATION_HOLD", None)

print()
if fails:
    print(f"FAILED {len(fails)} check(s):")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print(f"delivery verification: all checks passed")
