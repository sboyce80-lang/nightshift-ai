#!/usr/bin/env python3
"""One accuracy ledger for every harness.

Before this module the repo had two non-comparable scores and no memory:
run_jw_golden.py scored subtotal-vs-bid, run_golden_regression.py scored
quantity error *excluding* subtotal, golden_history.jsonl held exactly one
line, and no record said which flags produced which number. Fishkill
regressed +57% on subtotal while reading "neutral" on quantities and
neither tracker could see it.

This module fixes the record-keeping half:

- ONE history file: golden/accuracy_history.jsonl (committed — the
  longitudinal record is a project artifact, not a scratch log).
- ONE headline metric: component-wise mean absolute % error
  (component_mae_pct). Subtotal delta is recorded but is a reporting
  line, never the score — a subtotal in band with components ±100% is a
  coin-flip, not accuracy (Northwell rerun3: +8.3% subtotal, walls −34%,
  ceilings +138%).
- EVERY entry carries provenance: per-job flag fingerprint (read from the
  result's own run_fingerprint stamp when present), git SHA, source
  harness. A score without its posture is what made the 2026-09 Northwell
  series unattributable.
- manual_review is read at BOTH result levels — the top-level-only read
  is the exact bug that made the manual-review email gate dead code for
  three months and corrupted every run_meta.json in the rerun series.

Usage from a harness:

    from accuracy_ledger import job_record, append_entry
    jobs = [job_record(case_id, result_dict, rows) for ...]
    append_entry(source="run_jw_golden", jobs=jobs)

`rows` are {key, actual, target, delta_pct} dicts; anything with a real
target and delta contributes to component_mae_pct except subtotal keys.
"""
import json
import os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(HERE, "golden", "accuracy_history.jsonl")

# Keys that are money rollups, never quantity components.
_SUBTOTAL_KEYS = {"cost_estimate_subtotal", "cost_estimate_total"}


def read_manual_review(data):
    """True if the result is flagged for review at either level.

    run_analysis writes manual_review_required on the analysis dict; some
    wrappers copy it to the top level and some don't. Reading only one
    level is the dead-gate bug (2026-05-29 → 2026-09-04): jobs.py checked
    a top-level key run_analysis never sets, so the review email never
    fired. Never read this one-level again.
    """
    if not isinstance(data, dict):
        return False
    if data.get("manual_review_required"):
        return True
    analysis = data.get("analysis")
    if isinstance(analysis, dict) and analysis.get("manual_review_required"):
        return True
    return False


def read_run_fingerprint(data):
    """The posture stamp of the result itself, if the run recorded one.

    Returns {} for results produced before the stamp existed — an honest
    blank beats a guess from today's environment.
    """
    if not isinstance(data, dict):
        return {}
    fp = data.get("run_fingerprint")
    if not isinstance(fp, dict):
        analysis = data.get("analysis")
        if isinstance(analysis, dict):
            fp = analysis.get("run_fingerprint")
    return fp if isinstance(fp, dict) else {}


def component_mae_pct(rows):
    """Mean absolute % error across quantity components (subtotal excluded).

    Rows with no target or no measured delta are skipped, and the count of
    scored components rides along so a 1-component mean can't masquerade
    as a 9-component one.
    """
    deltas = [
        abs(r["delta_pct"]) for r in rows
        if r.get("key") not in _SUBTOTAL_KEYS
        and isinstance(r.get("delta_pct"), (int, float))
    ]
    if not deltas:
        return None, 0
    return round(sum(deltas) / len(deltas), 1), len(deltas)


def job_record(case_id, result, rows, subtotal=None, subtotal_delta_pct=None):
    """One job's line in a ledger entry."""
    mae, n = component_mae_pct(rows)
    fp = read_run_fingerprint(result)
    return {
        "case": case_id,
        "subtotal": subtotal,
        "subtotal_delta_pct": subtotal_delta_pct,
        "in_band_10": (subtotal_delta_pct is not None
                       and abs(subtotal_delta_pct) <= 10.0),
        "component_mae_pct": mae,
        "components_scored": n,
        "components": {
            r["key"]: {"actual": r.get("actual"), "target": r.get("target"),
                       "delta_pct": r.get("delta_pct")}
            for r in rows if r.get("key") not in _SUBTOTAL_KEYS
        },
        "manual_review": read_manual_review(result),
        "flag_fingerprint": fp.get("flag_fingerprint"),
        "git_sha": fp.get("git_sha"),
    }


def append_entry(source, jobs, history_path=HISTORY_PATH, extra=None):
    """Append one scored-run entry; returns the entry written."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "jobs": jobs,
    }
    if extra:
        entry.update(extra)
    os.makedirs(os.path.dirname(history_path), exist_ok=True)
    with open(history_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def load_history(history_path=HISTORY_PATH):
    if not os.path.exists(history_path):
        return []
    with open(history_path) as f:
        return [json.loads(line) for line in f if line.strip()]
