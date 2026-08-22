#!/usr/bin/env python3
"""Score the five JW golden jobs and track the mandatory-review exit
criterion (Hudson + Homewood inside ±10% on consecutive scored runs).

Default is SCORE-ONLY: reads each job's existing result.json (free, CI-
safe). Produce fresh results first with the flag-set batch:
    zsh nsai_batch_2026-08-20/rerun_batch.sh   (after clearing run_meta)

Usage:
    run_jw_golden.py            # score latest results, append history
    run_jw_golden.py --no-log   # score without appending history
    run_jw_golden.py --strict   # exit 1 if any subtotal band fails

Every scored run appends one JSONL line to
nsai_batch_2026-08-20/golden_history.jsonl — the consecutive-runs record.
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import regression_test as rt  # noqa: E402
from jw_golden import JW_CASES  # noqa: E402

HISTORY = os.path.join(HERE, "nsai_batch_2026-08-20", "golden_history.jsonl")
EXIT_CRITERION_JOBS = ("jw_hudson_hotel", "jw_homewood_suites")
CONSECUTIVE_NEEDED = 2


def score_case(cid, case):
    path = os.path.join(HERE, case["job_dir"], "result.json")
    if not os.path.exists(path):
        return {"case": cid, "error": "no result.json"}
    data = json.load(open(path))
    metrics = rt.extract_metrics(data)
    rows = []
    for key, (target, tol) in case["targets"].items():
        actual = metrics.get(key)
        if actual is None or not target:
            rows.append({"key": key, "actual": actual, "target": target,
                         "delta_pct": None, "ok": None})
            continue
        delta = (actual - target) / target
        rows.append({"key": key, "actual": round(actual, 1),
                     "target": target, "delta_pct": round(delta * 100, 1),
                     "ok": abs(delta) <= tol})
    sub = next((r for r in rows if r["key"] == "cost_estimate_subtotal"),
               None)
    subtotal_delta = sub["delta_pct"] if sub else None
    in_band_10 = (subtotal_delta is not None
                  and abs(subtotal_delta) <= 10.0)
    return {"case": cid, "display": case["display_name"],
            "jw_bid": case["jw_bid"],
            "subtotal": metrics.get("cost_estimate_subtotal"),
            "subtotal_delta_pct": subtotal_delta,
            "in_band_10": in_band_10,
            "manual_review": bool(data.get("manual_review_required")),
            "rows": rows}


def consecutive_streak(history, cid):
    n = 0
    for entry in reversed(history):
        job = next((j for j in entry.get("jobs", [])
                    if j.get("case") == cid), None)
        if job and job.get("in_band_10"):
            n += 1
        else:
            break
    return n


def main():
    log = "--no-log" not in sys.argv
    strict = "--strict" in sys.argv

    results = [score_case(cid, c) for cid, c in JW_CASES.items()]
    print(f"\n═══ JW GOLDEN SET — "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ═══")
    all_pass = True
    for r in results:
        if r.get("error"):
            print(f"  {r['case']:24s} ERROR: {r['error']}")
            all_pass = False
            continue
        band = "✅" if r["in_band_10"] else "  "
        print(f"  {band} {r['display'][:44]:44s} "
              f"${r['subtotal'] or 0:>12,.0f} vs ${r['jw_bid']:>12,.0f} "
              f"({r['subtotal_delta_pct']:+.1f}%)"
              f"{'' if r['manual_review'] else '  [no review flag!]'}")
        for row in r["rows"]:
            if row["ok"] is False:
                print(f"       ✗ {row['key']}: {row['actual']} vs "
                      f"{row['target']} ({row['delta_pct']:+.1f}%)")
                all_pass = False

    history = []
    if os.path.exists(HISTORY):
        with open(HISTORY) as f:
            history = [json.loads(l) for l in f if l.strip()]
    entry = {"ts": datetime.now(timezone.utc).isoformat(),
             "jobs": [{k: r.get(k) for k in
                       ("case", "subtotal", "subtotal_delta_pct",
                        "in_band_10", "manual_review")}
                      for r in results if not r.get("error")]}
    if log:
        with open(HISTORY, "a") as f:
            f.write(json.dumps(entry) + "\n")
        history.append(entry)

    print("\n  Mandatory-review exit criterion "
          f"(±10% × {CONSECUTIVE_NEEDED} consecutive):")
    met = True
    for cid in EXIT_CRITERION_JOBS:
        streak = consecutive_streak(history, cid)
        met = met and streak >= CONSECUTIVE_NEEDED
        print(f"    {cid:24s} streak: {streak}/{CONSECUTIVE_NEEDED}")
    print(f"    → criterion {'MET — flag removal eligible' if met else 'not met — keep NIGHTSHIFT_MANDATORY_REVIEW=1'}")

    if strict and not all_pass:
        sys.exit(1)


if __name__ == "__main__":
    main()
