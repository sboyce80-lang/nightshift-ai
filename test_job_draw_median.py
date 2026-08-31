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
              "NIGHTSHIFT_DRAW_SPREAD_REVIEW_PCT",
              "NIGHTSHIFT_DRAW_MEDIAN_K_SMALL",
              "NIGHTSHIFT_DRAW_MEDIAN_SMALL_MAX_PAGES"):
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
                          "TSC_Fusion_Highland_Rev2.pdf"),
             # canonical-repo fallback for worktree runs; CI skips
             os.path.join(os.path.dirname(HERE), "nightshift-repo",
                          "spike_samples", "364Main.pdf")):
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

print("\n11) Selected draw's saved JSON gets the report persisted")
_clear_env()
import json as _json
import tempfile
_calls = []
_tmp = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False)
_mid = _fake_result(30000, 90, 0, 150000)
_json.dump({"analysis": dict(_mid["analysis"]), "cost_estimate":
            {"subtotal": 150000}, "manual_review_required": False,
            "manual_review_reason": None}, _tmp)
_tmp.close()
_mid["output_json_path"] = _tmp.name
_seq = [_fake_result(29000, 88, 0, 147000), _mid,
        _fake_result(31000, 92, 0, 153000)]
T.run_analysis = _fake_run
try:
    out = T._run_job_draw_median(3, ["x.pdf"], {})
finally:
    T.run_analysis = _real_run
saved = _json.load(open(_tmp.name))
os.unlink(_tmp.name)
check(saved["analysis"].get("_job_draw_median", {}).get(
    "selected_draw") == 2, f"report not persisted to JSON: "
    f"{list(saved['analysis'].keys())}")
check(any("[Draw Median]" in n for n in saved["analysis"]["notes"]),
      "note not persisted to JSON")

print("\n13) Minority-nonzero component surfaces as scope disagreement")
_clear_env()
_calls = []
# exterior fires in draw 2 only; walls/doors/subtotal agree
_d1 = _fake_result(30000, 90, 0, 150000, ext=0)
_d2 = _fake_result(30500, 91, 0, 171000, ext=4800)
_d3 = _fake_result(29500, 89, 0, 149000, ext=0)
_seq = [_d1, _d2, _d3]
T.run_analysis = _fake_run
try:
    out = T._run_job_draw_median(3, ["x.pdf"], {})
finally:
    T.run_analysis = _real_run
_sds = out["analysis"]["_job_draw_median"]["scope_disagreements"]
check(any(s["component"] == "exterior_sqft"
          and s["nonzero_draws"] == [2] for s in _sds),
      f"exterior 1-of-3 not reported: {_sds}")
check(any("Scope disagreement" in n for n in out["analysis"]["notes"]),
      "scope-disagreement note missing")
check(out["cost_estimate"]["subtotal"] in (149000, 150000),
      f"majority (no-exterior) draw should win: "
      f"{out['cost_estimate']['subtotal']}")

print("\n14) Final-composition implausibility excludes gate-hollowed draws")
_clear_env()
_calls = []
# draw 3: gates stripped walls to ~nothing (110 SF / 30 rooms) but no
# pipeline cold-draw flag (the Dutchess ordering gap)
_seq = [_fake_result(9329, 28, 0, 24174, rooms=32),
        _fake_result(3300, 29, 0, 14782, rooms=31),
        _fake_result(110, 29, 0, 9981, rooms=30)]
T.run_analysis = _fake_run
try:
    out = T._run_job_draw_median(3, ["x.pdf"], {})
finally:
    T.run_analysis = _real_run
rep = out["analysis"]["_job_draw_median"]
check(3 in [int(x) for x in rep["excluded_cold_draws"]],
      f"hollowed draw must leave the vote: {rep['excluded_cold_draws']}")
check(rep["excluded_reasons"].get(3) == "final_composition_implausible",
      f"exclusion reason missing: {rep.get('excluded_reasons')}")
check(out["cost_estimate"]["subtotal"] in (24174, 14782),
      f"selection must come from plausible draws: "
      f"{out['cost_estimate']['subtotal']}")

print("\n15) Small sets raise K when configured")
_clear_env()
os.environ["NIGHTSHIFT_JOB_DRAW_MEDIAN"] = "3"
os.environ["NIGHTSHIFT_DRAW_MEDIAN_K_SMALL"] = "5"
if sample:
    import PyPDF2 as _pp2
    with open(sample, "rb") as _fh2:
        _n2 = len(_pp2.PdfReader(_fh2).pages)
    os.environ["NIGHTSHIFT_DRAW_MEDIAN_SMALL_MAX_PAGES"] = str(_n2)
    check(T._job_draw_median_k([sample]) == 5,
          f"small set must draw K=5 (n={_n2})")
    os.environ["NIGHTSHIFT_DRAW_MEDIAN_SMALL_MAX_PAGES"] = str(_n2 - 1)
    check(T._job_draw_median_k([sample]) == 3,
          "above the small threshold keeps base K")
else:
    print("  (no local sample PDF — small-K check skipped)")
_clear_env()

print("\n16) A single clean draw beats a field of implausible ones")
_clear_env()
_calls = []
# Dutchess shape: 1 healthy draw + 2 hollow (gates stripped the walls)
_seq = [_fake_result(9372, 29, 0, 28848, rooms=33),
        _fake_result(705, 29, 0, 11159, rooms=28),
        _fake_result(870, 29, 0, 13649, rooms=27)]
T.run_analysis = _fake_run
try:
    out = T._run_job_draw_median(3, ["x.pdf"], {})
finally:
    T.run_analysis = _real_run
rep = out["analysis"]["_job_draw_median"]
check(rep["vote_draws"] == [1],
      f"the lone clean draw must be the whole vote: {rep['vote_draws']}")
check(out["cost_estimate"]["subtotal"] == 28848,
      f"hollow draw must not be priced: {out['cost_estimate']}")

print("\n17) All draws implausible -> priced but forced to review")
_clear_env()
_calls = []
_seq = [_fake_result(705, 29, 0, 11159, rooms=28),
        _fake_result(870, 29, 0, 13649, rooms=27),
        _fake_result(800, 29, 0, 12000, rooms=27)]
T.run_analysis = _fake_run
try:
    out = T._run_job_draw_median(3, ["x.pdf"], {})
finally:
    T.run_analysis = _real_run
rep = out["analysis"]["_job_draw_median"]
check(rep.get("all_draws_implausible") is True,
      f"all-bad field must be recorded: {rep}")
check(out["analysis"].get("manual_review_required") is True,
      "all-bad field must force manual review")

print("\n18) Zero-ceiling draws are judged against their siblings")
_hudson = [
    {"rooms": 88, "walls_sqft": 40063, "doors": 89, "ceilings_sqft": 0},
    {"rooms": 71, "walls_sqft": 25808, "doors": 164, "ceilings_sqft": 4919},
    {"rooms": 146, "walls_sqft": 44730, "doors": 140, "ceilings_sqft": 5160},
]
check(T._hollow_against_field(_hudson[0], _hudson),
      "zero ceilings while siblings measured thousands = hollow")
check(not T._hollow_against_field(_hudson[1], _hudson),
      "a draw with ceilings is fine")
_noceil = [{"rooms": 40, "walls_sqft": 20000, "doors": 13,
            "ceilings_sqft": 0} for _ in range(3)]
check(not any(T._hollow_against_field(c, _noceil) for c in _noceil),
      "a job with no ceiling scope must not be flagged")
check(not T._final_composition_implausible(
    {"rooms": 88, "walls_sqft": 40063, "doors": 89, "ceilings_sqft": 0}),
    "the absolute band no longer judges ceilings")

print("\n12) Page cap sums across multiple PDFs")
_clear_env()
if sample:
    import PyPDF2 as _pp
    with open(sample, "rb") as _fh:
        _n = len(_pp.PdfReader(_fh).pages)
    os.environ["NIGHTSHIFT_JOB_DRAW_MEDIAN"] = "3"
    os.environ["NIGHTSHIFT_DRAW_MEDIAN_MAX_PAGES"] = str(_n + 1)
    check(T._job_draw_median_k([sample]) == 3,
          f"single file under cap must draw (n={_n})")
    check(T._job_draw_median_k([sample, sample]) == 1,
          f"two files over cap must not draw (2x{_n})")
    _clear_env()
else:
    print("  (no local sample PDF — multi-PDF cap check skipped)")

print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ all draw-median checks passed")
