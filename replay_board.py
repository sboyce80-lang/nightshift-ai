#!/usr/bin/env python3
"""The replay board — the whole golden set scored in seconds, for $0.

Phase 2 of the accuracy program: evaluation as infrastructure. One
command scores every golden job's most recent STORED result on
component-wise mean absolute % error, appends one attributable entry to
the unified accuracy ledger, and tracks the tier-1 exit criterion
(component MAE <= 10% on consecutive boards). No API calls: rerunning
extraction resamples ±30% draw noise at $2.50-7 a job — the board scores
what already ran, so a code change's effect is measured against a FIXED
extraction instead of a fresh sample.

    python3 replay_board.py                # score + append to ledger
    python3 replay_board.py --no-log       # score only
    python3 replay_board.py --determinism  # deterministic-layer check:
        replay build_priced_takeoff twice on a stored roster under the
        committed prod flag posture; any diff between the two runs is a
        nondeterminism bug in the gate chain (Phase 2 exit requires
        bit-identical replays).

Registry: composes regression_test.REFERENCE_CASES (Rider, tiers 1-3) +
jw_golden.JW_CASES (JW five) + the board-local cases below (Northwell =
case 14 with JW's full component key; Academy 88 formally declared
raster-class — scored for visibility, permanently excluded from the ±10%
program because VME cannot see scanned sheets).
"""
import argparse
import copy
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import accuracy_ledger as ledger  # noqa: E402
import regression_test as rt  # noqa: E402
from jw_golden import JW_CASES  # noqa: E402

MAE_GATE_PCT = 10.0
STREAK_NEEDED = 2

# Board-local cases (shape matches REFERENCE_CASES/JW_CASES targets).
BOARD_CASES = {
    "jw_northwell_phelps": {
        "display_name": "Phelps Hospital / Northwell (JW RP 26-010-AUG)",
        "tier": 1, "cls": "jw", "program": "vector",
        "bid": 39004.63,
        "targets": {
            "cost_estimate_subtotal": (39004.63, 0.10),
            "total_paintable_wall_sqft": (20308, 0.15),
            "total_paintable_ceiling_sqft": (2365, 0.25),
            "total_doors_full_paint": (78, 0.15),
            "total_windows_painted_interior": (25, 0.30),
        },
        "rosters": [
            "nsai_jw_northwell_rerun5_2026-09-04/result.json",
            "nsai_jw_northwell_rerun4_2026-09-03/result.json",
            "nsai_jw_northwell_rerun3_2026-09-02/result.json",
        ],
    },
    "academy_88": {
        "display_name": "88 Academy (RASTER — outside the ±10% program)",
        "tier": 3, "cls": "raster", "program": "raster-excluded",
        "bid": None,
        "targets": {},
        "rosters": ["nsai_batch_2026-08-20/academy_88/result.json"],
    },
}

# Where each registry job's stored results live, newest preferred.
ROSTER_HINTS = {
    "_round5": ("/Users/stevenboyce/Desktop/_Code/NSAI/"
                "nightshift-mergeship-wt/nsai_board_round5_2026-09-04/"
                "results/{job}.result.json"),
    "_marathon": "nsai_marathon_2026-08-23/results/{job}.json",
    "_batch": "nsai_batch_2026-08-20/{short}/result.json",
}

# One-off stored results living outside the standard run dirs.
ROSTER_EXTRA = {
    "fishkill_cenhud": ("/Users/stevenboyce/Desktop/_Code/NSAI/"
                        "rider_batch_durable/cenhud/result.json"),
}


def _registry():
    cases = {}
    for cid, c in rt.REFERENCE_CASES.items():
        cases[cid] = {
            "display": cid, "tier": c.get("tier", 3), "cls": "rider",
            "program": "vector", "targets": c.get("targets") or {},
        }
    for cid, c in JW_CASES.items():
        cases[cid] = {
            "display": c["display_name"], "tier": 1, "cls": "jw",
            "program": "vector", "targets": c.get("targets") or {},
            "batch_dir": c.get("job_dir"),
        }
    for cid, c in BOARD_CASES.items():
        cases[cid] = {
            "display": c["display_name"], "tier": c["tier"],
            "cls": c["cls"], "program": c["program"],
            "targets": c.get("targets") or {},
            "rosters": c.get("rosters"),
        }
    return cases


# Stored artifacts are machine-local (gitignored, customer-confidential);
# the canonical tree holds the run dirs regardless of which worktree the
# board runs from.
CANON = "/Users/stevenboyce/Desktop/_Code/NSAI/nightshift-repo"


def find_roster(cid, case):
    """Newest stored result.json for a job, searched across run dirs."""
    cands = []
    if cid in ROSTER_EXTRA:
        cands.append(ROSTER_EXTRA[cid])
    for pat in case.get("rosters") or []:
        cands.append(os.path.join(HERE, pat))
        cands.append(os.path.join(CANON, pat))
    r5 = ROSTER_HINTS["_round5"].format(job=cid)
    cands.append(r5)
    cands.append(os.path.join(
        HERE, ROSTER_HINTS["_marathon"].format(job=cid)))
    bd = case.get("batch_dir")
    if bd:
        cands.append(os.path.join(HERE, bd, "result.json"))
    short = cid.replace("jw_", "")
    cands.append(os.path.join(
        HERE, ROSTER_HINTS["_batch"].format(short=short)))
    hits = [p for p in cands if os.path.exists(p)]
    if not hits:
        return None
    return max(hits, key=os.path.getmtime)


def score(cid, case, roster_path):
    with open(roster_path) as f:
        data = json.load(f)
    metrics = rt.extract_metrics(data)
    rows = []
    for key, spec in case["targets"].items():
        tgt = spec[0] if isinstance(spec, (list, tuple)) else spec
        tol = spec[1] if isinstance(spec, (list, tuple)) and \
            len(spec) > 1 else 0.10
        actual = metrics.get(key)
        if actual is None or not tgt:
            rows.append({"key": key, "actual": actual, "target": tgt,
                         "delta_pct": None, "ok": None})
            continue
        delta = (float(actual) - float(tgt)) / float(tgt)
        rows.append({"key": key, "actual": round(float(actual), 1),
                     "target": tgt, "delta_pct": round(delta * 100, 1),
                     "ok": abs(delta) <= tol})
    sub = next((r for r in rows
                if r["key"] == "cost_estimate_subtotal"), None)
    rec = ledger.job_record(
        cid, data, rows,
        subtotal=metrics.get("cost_estimate_subtotal"),
        subtotal_delta_pct=sub["delta_pct"] if sub else None)
    rec["tier"] = case["tier"]
    rec["cls"] = case["cls"]
    rec["program"] = case["program"]
    rec["roster"] = os.path.relpath(roster_path, HERE) \
        if roster_path.startswith(HERE) else roster_path
    return rec


def tier1_streak(history):
    """Consecutive replay_board entries where every scored tier-1
    vector-program job has component MAE <= MAE_GATE_PCT."""
    streak = 0
    for entry in reversed(history):
        if entry.get("source") != "replay_board":
            continue
        t1 = [j for j in entry.get("jobs", [])
              if j.get("tier") == 1 and j.get("program") == "vector"
              and j.get("component_mae_pct") is not None]
        if t1 and all(j["component_mae_pct"] <= MAE_GATE_PCT for j in t1):
            streak += 1
        else:
            break
    return streak


def determinism_check():
    """Replay the pricing chain twice on one stored roster under the
    committed prod posture; any diff is a nondeterminism bug."""
    from golden.load_prod_flags import apply_prod_flags
    apply_prod_flags()
    import Takeoff_DIRECT as T
    reg = _registry()
    for cid, case in reg.items():
        path = find_roster(cid, case)
        if not path:
            continue
        with open(path) as f:
            raw = json.load(f)
        analysis = raw.get("analysis", raw)
        if not isinstance(analysis.get("floors"), list):
            continue
        base = copy.deepcopy(analysis)
        base.pop("_priced_takeoff_built", None)
        a1 = T.build_priced_takeoff(copy.deepcopy(base))
        a2 = T.build_priced_takeoff(copy.deepcopy(base))
        k1 = json.dumps(a1.get("aggregated_totals"), sort_keys=True)
        k2 = json.dumps(a2.get("aggregated_totals"), sort_keys=True)
        verdict = "BIT-IDENTICAL" if k1 == k2 else "DIVERGED"
        print(f"  {cid:26s} {verdict}")
        if k1 != k2:
            print("    run1:", k1[:200])
            print("    run2:", k2[:200])
            return False
        return True   # one job is a smoke check; --all could extend
    print("  no usable roster found")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--determinism", action="store_true")
    args = ap.parse_args()

    if args.determinism:
        print("deterministic-layer replay check "
              "(prod posture, stored roster):")
        ok = determinism_check()
        sys.exit(0 if ok else 1)

    reg = _registry()
    jobs, missing = [], []
    for cid, case in reg.items():
        path = find_roster(cid, case)
        if not path:
            missing.append(cid)
            continue
        jobs.append(score(cid, case, path))

    print(f"\n═══ REPLAY BOARD — {len(jobs)} scored, "
          f"{len(missing)} without stored results ═══")
    print(f"{'job':26s} {'tier':>4s} {'cls':6s} {'mae%':>6s} "
          f"{'n':>2s} {'subΔ%':>7s} {'mr':>3s}")
    for j in sorted(jobs, key=lambda x: (x["tier"], x["case"])):
        mae = j["component_mae_pct"]
        flagchip = "✅" if (mae is not None and mae <= MAE_GATE_PCT
                           and j["program"] == "vector") else "  "
        sub = j.get("subtotal_delta_pct")
        print(f"{flagchip}{j['case']:24s} {j['tier']:>4d} {j['cls']:6s} "
              f"{mae if mae is not None else '—':>6} "
              f"{j['components_scored']:>2d} "
              f"{sub if sub is not None else '—':>7} "
              f"{'Y' if j['manual_review'] else 'n':>3s}")
    if missing:
        print(f"  (no stored result: {', '.join(missing)})")

    history = ledger.load_history()
    if not args.no_log:
        ledger.append_entry(source="replay_board", jobs=jobs)
        history = ledger.load_history()
    streak = tier1_streak(history)
    print(f"\n  Exit criterion (tier-1 vector jobs MAE ≤ "
          f"{MAE_GATE_PCT:.0f}% × {STREAK_NEEDED} consecutive boards): "
          f"streak {streak}/{STREAK_NEEDED}")
    print("  Raster-class jobs are scored for visibility only — "
          "outside the ±10% program by policy.")


if __name__ == "__main__":
    main()
