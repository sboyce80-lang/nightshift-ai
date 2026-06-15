#!/usr/bin/env python3
"""Offline tests for Phase 2.4: calibrated confidence (confidence.py).

Covers the deterministic inputs extractor, the monotone evidence model,
hard-gate caps, the calibrated-vs-uncalibrated branch, golden binning once
N>=threshold, and graceful degradation on missing data. No API, no PDF.

Run: python3 test_confidence_calibration.py
"""
import importlib.util as iu
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = iu.spec_from_file_location("C", os.path.join(HERE, "confidence.py"))
C = iu.module_from_spec(spec)
spec.loader.exec_module(C)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def good_analysis():
    """A clean residential job: full coverage, well anchored, all measured."""
    rooms = [{"room_name": f"R{i}", "_anchor": {"label": f"R{i}"}}
             for i in range(20)]
    return {
        "project_info": {"building_type": "residential", "footprint_sqft": 8000},
        "floors": [{"floor_name": "1st Floor", "rooms": rooms}],
        "coverage": {"totals": {"measured": 17, "failed": 0, "degraded": 0,
                                "unaccounted": 0, "excluded": 3}},
        "bbox_spike_summary": {"total_rooms": 20, "anchored": 19,
                               "coverage_pct": 95},
        "aggregated_totals": {"total_paintable_wall_sqft": 43000,
                              "total_paintable_ceiling_sqft": 13000},
        "_priced_takeoff": {"breakdown": {
            "total_paintable_wall_sqft": {"priced": 43000, "measured": 43000,
                                          "derived": 0, "assumed": 0},
            "total_paintable_ceiling_sqft": {"priced": 13000, "measured": 13000,
                                             "derived": 0, "assumed": 0},
        }},
        "_quantity_adjustments": [],
    }


def bad_analysis():
    """A weak job: failed pages, poor anchors, heavy assumed scope."""
    rooms = [{"room_name": f"R{i}"} for i in range(20)]
    rooms[0]["_no_anchor"] = True
    rooms[1]["_no_anchor"] = True
    rooms[2]["_added_by_verification"] = True
    return {
        "project_info": {"building_type": "residential", "footprint_sqft": 0},
        "floors": [{"floor_name": "1st Floor", "rooms": rooms}],
        "coverage": {"totals": {"measured": 8, "failed": 6, "degraded": 0,
                                "unaccounted": 3, "excluded": 1}},
        "bbox_spike_summary": {"total_rooms": 20, "anchored": 6,
                               "coverage_pct": 30},
        "aggregated_totals": {"total_paintable_wall_sqft": 60000,
                              "total_paintable_ceiling_sqft": 18000},
        "_priced_takeoff": {"breakdown": {
            "total_paintable_wall_sqft": {"priced": 60000, "measured": 35000,
                                          "derived": 5000, "assumed": 20000},
            "total_paintable_ceiling_sqft": {"priced": 18000, "measured": 10000,
                                             "derived": 0, "assumed": 8000},
        }},
        "_quantity_adjustments": [
            {"stage": "supplement", "item": "total_paintable_wall_sqft",
             "delta": 20000, "source": "assumed"},
        ],
        "manual_review_required": True,
    }


def main():
    print("\n── Inputs extraction (good job) ──")
    gi = C.compute_confidence_inputs(good_analysis())
    check("coverage_pct = 17/17 = 1.0", gi["coverage_pct"] == 1.0)
    check("anchor_pct from bbox summary = 0.95", gi["anchor_pct"] == 0.95)
    check("assumed_frac = 0 (all measured)", gi["assumed_frac"] == 0.0)
    check("no verifier misses", gi["verifier_miss_rate"] == 0.0)
    check("no hard gate tripped on the clean job",
          gi["hard_gate_tripped"] is False, str(gi["hard_gates"]))

    print("\n── Inputs extraction (bad job) ──")
    bi = C.compute_confidence_inputs(bad_analysis())
    check("coverage_pct = 8/(8+6+3) ≈ 0.47",
          abs(bi["coverage_pct"] - 8/17) < 0.01)
    check("anchor_pct = 0.30", bi["anchor_pct"] == 0.30)
    check("assumed_frac = 28000/78000 ≈ 0.36",
          abs(bi["assumed_frac"] - 28000/78000) < 0.01)
    check("unanchored_rate = 2/20 = 0.10", bi["unanchored_rate"] == 0.10)
    check("verifier_miss_rate = 1/20 = 0.05", bi["verifier_miss_rate"] == 0.05)
    check("failed-pages gate tripped", bi["hard_gates"]["failed_pages"] is True)
    check("missing-footprint gate tripped",
          bi["hard_gates"]["missing_footprint"] is True)
    check("manual-review gate tripped",
          bi["hard_gates"]["manual_review"] is True)

    print("\n── Evidence model monotonicity ──")
    ge = C._evidence_score(gi)
    be = C._evidence_score(bi)
    check("worse evidence → higher predicted error", be > ge,
          f"good={ge:.1f} bad={be:.1f}")
    check("clean job predicts a tight band (< 12%)", ge < 12, f"{ge:.1f}")
    check("weak job predicts a wide band (> 25%)", be > 25, f"{be:.1f}")
    # raising assumed_frac alone must not lower the error
    bi2 = dict(bi)
    bi2["assumed_frac"] = min(1.0, bi["assumed_frac"] + 0.3)
    check("raising assumed_frac cannot lower predicted error",
          C._evidence_score(bi2) >= be)

    print("\n── Uncalibrated path ──")
    p = C.predict_error(gi, calibration=[])
    check("no golden data → calibrated False", p["calibrated"] is False)
    check("uncalibrated applies the safety multiplier",
          p["predicted_error_pct"] > C._evidence_score(gi))
    check("basis names the missing calibration",
          "not yet calibrated" in p["basis"])

    print("\n── Calibrated path (golden binning) ──")
    # Build >= MIN_CALIBRATION rows spanning a range of evidence/error.
    rows = []
    for i in range(C.MIN_CALIBRATION + 4):
        frac = i / (C.MIN_CALIBRATION + 3)
        inp = {"coverage_pct": 1.0 - 0.5 * frac, "anchor_pct": 1.0 - 0.6 * frac,
               "verifier_miss_rate": 0.1 * frac, "unanchored_rate": 0.1 * frac,
               "assumed_frac": 0.4 * frac, "adjustment_mag": 0.2 * frac}
        # true error tracks evidence with a little noise
        rows.append({"job": f"j{i}", "inputs": inp,
                     "true_error_pct": 5 + 40 * frac + (1 if i % 2 else -1)})
    pc = C.predict_error(gi, calibration=rows)
    check("with >= MIN_CALIBRATION rows → calibrated True",
          pc["calibrated"] is True, pc["basis"])
    check("calibrated basis cites the golden bin", "golden bin" in pc["basis"])
    check("clean job calibrates to a low error band",
          pc["predicted_error_pct"] < 20, str(pc["predicted_error_pct"]))
    pc_bad = C.predict_error(bi, calibration=rows)
    check("weak job calibrates to a higher band than the clean job",
          pc_bad["predicted_error_pct"] > pc["predicted_error_pct"])

    print("\n── Hard-gate caps (assess_confidence) ──")
    ga = C.assess_confidence(good_analysis())
    check("clean job: no caps applied", ga["caps_applied"] == [],
          str(ga["caps_applied"]))
    check("clean job: high confidence level", ga["confidence_level"] >= 80,
          str(ga["confidence_level"]))
    ba = C.assess_confidence(bad_analysis())
    check("weak job: error floored by hard gate",
          ba["predicted_error_pct"] >= C.HARD_GATE_ERROR_FLOOR_PCT)
    check("weak job: confidence level capped at the hard-gate cap",
          ba["confidence_level"] <= C.HARD_GATE_CONFIDENCE_CAP)
    check("weak job: caps_applied is populated", len(ba["caps_applied"]) >= 1)

    print("\n── Graceful degradation ──")
    empty = C.compute_confidence_inputs({})
    check("empty analysis → coverage None (no false confidence)",
          empty["coverage_pct"] is None)
    ea = C.assess_confidence({})
    check("empty analysis assess does not crash + returns a number",
          isinstance(ea["predicted_error_pct"], float))
    check("missing coverage/anchor treated as uncertain, not perfect",
          C._evidence_score(empty) > C.BASE_ERROR_PCT)
    check("assess_confidence on non-dict-safe input",
          isinstance(C.compute_confidence_inputs(None), dict))

    print("\n── Closed feedback loop (append + status) ──")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "calibration_data.json")
        st0 = C.calibration_status(p)
        check("status on missing file: 0 rows, inactive, needs MIN",
              st0["n"] == 0 and st0["active"] is False
              and st0["needed"] == C.MIN_CALIBRATION)
        n1 = C.append_calibration_row(p, "jobA", C.compute_confidence_inputs(good_analysis()),
                                      7.0, verified_on="2026-06-13",
                                      metric="walls")
        check("append writes the first row", n1 == 1 and os.path.exists(p))
        # re-verify same job → replace, not duplicate
        n2 = C.append_calibration_row(p, "jobA", C.compute_confidence_inputs(bad_analysis()),
                                      30.0)
        check("re-verifying a job replaces its row (dedupe by job)", n2 == 1)
        rows = C.load_calibration(p)
        check("replaced row carries the new error", rows[0]["true_error_pct"] == 30.0)
        for i in range(C.MIN_CALIBRATION - 1):
            C.append_calibration_row(p, f"job{i}",
                                     C.compute_confidence_inputs(good_analysis()), 8.0)
        st = C.calibration_status(p)
        check("status activates once MIN_CALIBRATION rows reached",
              st["n"] >= C.MIN_CALIBRATION and st["active"] is True
              and st["needed"] == 0)
        # the loop is closed: assess now uses the calibrated branch
        cc = C.assess_confidence(good_analysis(), calibration_path=p)
        check("assess_confidence flips to calibrated once table is full",
              cc["calibrated"] is True, cc["basis"])
        with open(p, "w") as _f:
            _f.write('[{"job":"x","true_error_pct":5,"inputs":{}}]')
        n_legacy = C.append_calibration_row(
            p, "y", C.compute_confidence_inputs(good_analysis()), 9.0)
        check("append tolerates a legacy bare-list table", n_legacy == 2)

    print("\n── Over-extraction signal (walls/ceiling per schedule door) ──")
    def over_extracted(walls, ceil, doors):
        return {"project_info": {"building_type": "residential"},
                "floors": [], "coverage": {"totals": {"measured": 5, "failed": 0,
                            "degraded": 0, "unaccounted": 0}},
                "aggregated_totals": {"total_paintable_wall_sqft": walls,
                    "total_paintable_ceiling_sqft": ceil,
                    "total_doors_full_paint": doors, "total_doors_hm_panel": 0}}
    # Dutchess-like 5x over: walls 28557 / 29 doors = 985, ceil 567/door
    bad = C.compute_confidence_inputs(over_extracted(28557, 16455, 29))
    check("over-extracted job: walls_per_door computed",
          bad["walls_per_door"] == round(28557/29, 1))
    check("over-extracted job: over_extraction_ratio fires (>1)",
          bad["over_extraction_ratio"] > 1.0, str(bad["over_extraction_ratio"]))
    # In-band job: golden Fishkill 43003 / 159 = 270/door, 13451/159 = 85/door
    okj = C.compute_confidence_inputs(over_extracted(43003, 13451, 159))
    check("in-band job: over_extraction_ratio = 0", okj["over_extraction_ratio"] == 0.0)
    # Low-door building (warehouse): big walls, 2 doors — must NOT flag (< MIN_DOORS)
    wh = C.compute_confidence_inputs(over_extracted(20000, 8000, 2))
    check("low-door building below MIN_DOORS is not flagged (no false positive)",
          wh["over_extraction_ratio"] == 0.0)
    check("ratio is clamped at the cap",
          C.compute_confidence_inputs(over_extracted(500000, 300000, 20))
          ["over_extraction_ratio"] == C.OVEREXT_RATIO_CAP)
    check("over-extraction raises predicted error well above an in-band job",
          C._evidence_score(bad) > C._evidence_score(okj) + 40,
          f"bad={C._evidence_score(bad):.0f} ok={C._evidence_score(okj):.0f}")
    check("the over-extraction signal would catch a Dutchess-class disaster "
          "the other inputs miss",
          C.predict_error(bad)["predicted_error_pct"] > 80)

    print("\n── Will / calibrated-confidence reconciliation ──")
    # Will says ready; calibrated confidence is high + no gate → stays ready.
    cc_good = C.assess_confidence(good_analysis())
    w = {"confidence": {"level_pct": 90},
         "pipeline_flags": {"ready_to_send": True}}
    C.reconcile_will_confidence(w, cc_good)
    check("both confident → ready_to_send preserved",
          w["pipeline_flags"]["ready_to_send"] is True)
    check("calibrated numbers recorded alongside Will's",
          "calibrated_error_pct" in w["confidence"]
          and "calibrated_level" in w["confidence"])
    # Will says ready; calibrated has a hard gate (bad job) → vetoed.
    cc_bad = C.assess_confidence(bad_analysis())
    w2 = {"confidence": {"level_pct": 92},
          "pipeline_flags": {"ready_to_send": True}}
    C.reconcile_will_confidence(w2, cc_bad)
    check("calibrated hard gate vetoes Will's ready_to_send",
          w2["pipeline_flags"]["ready_to_send"] is False
          and w2["pipeline_flags"]["route_to_human_review"] is True)
    check("veto reason recorded", w2["pipeline_flags"].get("ready_to_send_overrides"))
    check("Will's own level_pct not overwritten", w2["confidence"]["level_pct"] == 92)
    # Calibrated can only TIGHTEN: if Will already said not-ready, stays not-ready.
    w3 = {"confidence": {"level_pct": 50},
          "pipeline_flags": {"ready_to_send": False}}
    C.reconcile_will_confidence(w3, cc_good)
    check("reconciliation never loosens a not-ready job",
          w3["pipeline_flags"]["ready_to_send"] is False)
    check("reconcile on malformed input is a no-op",
          C.reconcile_will_confidence(None, cc_good) is None)

    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
