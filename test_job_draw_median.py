#!/usr/bin/env python3
"""Job-level draw-median consensus (NIGHTSHIFT_JOB_DRAW_MEDIAN).

2026-08-25 board: 7/9 goldens banded at least once at an identical
posture while single draws swung ±40% — extraction-draw variance, not
mechanism. K independent draws, keep the composition median. Locks in:
selection math, cold-draw exclusion + per-draw retry, checkpoint-key
draw namespacing, recursion/interactive/page-cap guards, spread-triggered
manual review, env restoration.
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


def _clear_env():
    for k in ("NIGHTSHIFT_JOB_DRAW_MEDIAN", "NIGHTSHIFT_JOB_DRAW_ACTIVE",
              "NIGHTSHIFT_JOB_DRAW_TAG", "NIGHTSHIFT_DRAW_MEDIAN_MAX_PAGES",
              "NIGHTSHIFT_DRAW_SPREAD_REVIEW_PCT"):
        os.environ.pop(k, None)


def _fake_result(walls, doors, wc, subtotal, ext=0.0, rooms=10,
                 cold=None):
    analysis = {
        "aggregated_totals": {
            "total_paintable_wall_sqft": walls,
            "total_doors_full_paint": doors,
            "total_wallcovering_sqft": wc,
        },
        "exterior": {"exterior_paint_sqft": ext},
        "floors": [{"rooms": [{"name": f"R{i}"} for i in range(rooms)]}],
        "notes": [],
    }
    if cold:
        analysis["_cold_draw_suspect"] = cold
    return {"analysis": analysis,
            "cost_estimate": {"subtotal": subtotal},
            "output_json_path": None}


print("\n1) Selection math: middle draw of three wins")
comps = [T._draw_composition(_fake_result(20000, 60, 0, 100000)),
         T._draw_composition(_fake_result(31000, 95, 0, 155000)),
         T._draw_composition(_fake_result(30000, 90, 0, 150000))]
best, rep = T._draw_median_select(comps)
check(best == 2, f"expected middle-composition draw 2, got {best} ({rep})")
check(rep["scores"][0] > rep["scores"][2],
      f"cold draw should score worse: {rep['scores']}")

print("\n2) Zero-component draw penalized against agreeing peers")
comps = [T._draw_composition(_fake_result(30000, 0, 12000, 150000)),
         T._draw_composition(_fake_result(29500, 88, 11800, 148000)),
         T._draw_composition(_fake_result(30500, 92, 12500, 152000))]
best, rep = T._draw_median_select(comps)
check(best != 0, f"doors=0 draw must not be selected: {best} ({rep})")

print("\n3) Composition vector reads the aggregate fields")
c = T._draw_composition(_fake_result(1000, 5, 200, 9000, ext=300, rooms=4))
check(c["walls_sqft"] == 1000 and c["doors"] == 5
      and c["wallcovering_sqft"] == 200 and c["subtotal"] == 9000
      and c["exterior_sqft"] == 300 and c["rooms"] == 4,
      f"composition mismatch: {c}")

print("\n4) K guards: default off, env on, recursion, interactive")
_clear_env()
check(T._job_draw_median_k(["x.pdf"]) == 1, "default must be off (k=1)")
os.environ["NIGHTSHIFT_JOB_DRAW_MEDIAN"] = "3"
check(T._job_draw_median_k(["x.pdf"]) == 3, "env k=3 not honored")
check(T._job_draw_median_k(["x.pdf"], interactive=True) == 1,
      "interactive session must not multi-draw")
os.environ["NIGHTSHIFT_JOB_DRAW_ACTIVE"] = "1"
check(T._job_draw_median_k(["x.pdf"]) == 1,
      "recursion guard: draws must not spawn draws")
_clear_env()

print("\n5) Page cap falls back to a single draw")
os.environ["NIGHTSHIFT_JOB_DRAW_MEDIAN"] = "3"
os.environ["NIGHTSHIFT_DRAW_MEDIAN_MAX_PAGES"] = "1"
sample = None
for cand in (os.path.join(HERE, "spike_samples", "364Main.pdf"),
             os.path.join(HERE, "golden", "plans",
                          "TSC_Fusion_Highland_Rev2.pdf")):
    if os.path.exists(cand):
        sample = cand
        break
if sample:
    check(T._job_draw_median_k([sample]) == 1,
          f"page cap ignored on {os.path.basename(sample)}")
else:
    print("  (no local sample PDF — cap check skipped)")
_clear_env()

print("\n6) Checkpoint key namespaces by draw tag")
_clear_env()
k_plain = T._sheet_checkpoint_key("prompt", "ctx", True)
os.environ["NIGHTSHIFT_JOB_DRAW_TAG"] = "d1"
k_d1 = T._sheet_checkpoint_key("prompt", "ctx", True)
k_d1_again = T._sheet_checkpoint_key("prompt", "ctx", True)
os.environ["NIGHTSHIFT_JOB_DRAW_TAG"] = "d2"
k_d2 = T._sheet_checkpoint_key("prompt", "ctx", True)
_clear_env()
check(k_plain != k_d1 and k_d1 != k_d2,
      f"draw tags must namespace checkpoint keys: {k_plain} {k_d1} {k_d2}")
check(k_d1 == k_d1_again, "same tag must be stable (within-draw resume)")

print("\n7) Orchestrator end-to-end: median wins, env restored")
_clear_env()
_calls = []
_seq = [_fake_result(20000, 60, 0, 100000),
        _fake_result(31000, 95, 0, 155000),
        _fake_result(30000, 90, 0, 150000)]
_real_run = T.run_analysis


def _fake_run(pdf_paths, **kw):
    _calls.append(os.environ.get("NIGHTSHIFT_JOB_DRAW_TAG"))
    return _seq[len(_calls) - 1]


T.run_analysis = _fake_run
try:
    out = T._run_job_draw_median(3, ["x.pdf"], {})
finally:
    T.run_analysis = _real_run
rep = out["analysis"]["_job_draw_median"]
check(rep["selected_draw"] == 3 and out["cost_estimate"]["subtotal"] == 150000,
      f"expected draw 3 selected: {rep}")
check(_calls == ["d1", "d2", "d3"], f"draw tags wrong: {_calls}")
check("NIGHTSHIFT_JOB_DRAW_ACTIVE" not in os.environ
      and "NIGHTSHIFT_JOB_DRAW_TAG" not in os.environ,
      "orchestrator must restore env")
check(any("[Draw Median]" in n for n in out["analysis"]["notes"]),
      "reviewer note missing")

print("\n8) Cold-flagged draw retried once and excluded from the vote")
_clear_env()
_calls = []
# draw 1 cold -> retry stays cold; draws 2-3 clean and close together
_seq = [_fake_result(9000, 2, 0, 40000, cold={"trigger": "doors_zero"}),
        _fake_result(9100, 2, 0, 41000, cold={"trigger": "doors_zero"}),
        _fake_result(30000, 90, 0, 150000),
        _fake_result(31000, 95, 0, 156000)]
T.run_analysis = _fake_run
try:
    out = T._run_job_draw_median(3, ["x.pdf"], {})
finally:
    T.run_analysis = _real_run
rep = out["analysis"]["_job_draw_median"]
check(len(_calls) == 4, f"cold draw should retry once: {len(_calls)} calls")
check(rep["excluded_cold_draws"] == [1], f"draw 1 not excluded: {rep}")
check(out["cost_estimate"]["subtotal"] in (150000, 156000),
      f"selected a cold draw: {out['cost_estimate']['subtotal']}")

print("\n9) Wide subtotal spread across plausible draws holds for review")
_clear_env()
_calls = []
_seq = [_fake_result(20000, 60, 0, 90000),
        _fake_result(30000, 90, 0, 150000),
        _fake_result(31000, 95, 0, 156000)]
T.run_analysis = _fake_run
try:
    out = T._run_job_draw_median(3, ["x.pdf"], {})
finally:
    T.run_analysis = _real_run
check(out["analysis"].get("manual_review_required") is True,
      "50%-spread job must be held for review")
check(out["analysis"]["_job_draw_median"].get("subtotal_spread_pct", 0) > 40,
      f"spread not recorded: {out['analysis']['_job_draw_median']}")

print("\n10) Tight spread does not force review on its own")
_clear_env()
_calls = []
_seq = [_fake_result(29000, 88, 0, 147000),
        _fake_result(30000, 90, 0, 150000),
        _fake_result(31000, 92, 0, 153000)]
T.run_analysis = _fake_run
try:
    out = T._run_job_draw_median(3, ["x.pdf"], {})
finally:
    T.run_analysis = _real_run
check(not out["analysis"].get("manual_review_required"),
      "tight-spread job wrongly held")
_clear_env()

print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ all draw-median checks passed")
