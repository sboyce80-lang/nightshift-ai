#!/usr/bin/env python3
"""The unified accuracy ledger (accuracy_ledger.py).

Locks in: component MAE excludes subtotal rollups and reports how many
components it averaged; manual_review is read at BOTH result levels (the
dead-gate bug class); provenance comes from the result's own stamp and is
honestly blank for pre-stamp results; entries append with source + ts.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import accuracy_ledger as al  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


print("accuracy ledger checks")

# --- manual_review: both levels, never one ---------------------------------
check(al.read_manual_review({"manual_review_required": True}),
      "manual_review read at top level")
check(al.read_manual_review(
    {"analysis": {"manual_review_required": True}}),
    "manual_review read at analysis level (the dead-gate class)")
check(not al.read_manual_review({"analysis": {}}),
      "unflagged result reads False")
check(not al.read_manual_review(None), "non-dict reads False, no crash")

# --- component MAE ----------------------------------------------------------
rows = [
    {"key": "total_paintable_wall_sqft", "actual": 90, "target": 100,
     "delta_pct": -10.0},
    {"key": "total_door_count", "actual": 60, "target": 50,
     "delta_pct": 20.0},
    {"key": "cost_estimate_subtotal", "actual": 101, "target": 100,
     "delta_pct": 1.0},
    {"key": "total_stair_sections", "actual": None, "target": 8,
     "delta_pct": None},
]
mae, n = al.component_mae_pct(rows)
check(mae == 15.0, f"MAE averages quantity components only: got {mae}")
check(n == 2, f"unmeasured components are skipped, not zero-filled: n={n}")
check(al.component_mae_pct([]) == (None, 0),
      "no scorable components reports None, not 0.0")

# A subtotal in band with terrible components must not look accurate.
offsetting = [
    {"key": "walls", "delta_pct": -34.0, "actual": 1, "target": 1},
    {"key": "ceilings", "delta_pct": 138.0, "actual": 1, "target": 1},
    {"key": "cost_estimate_subtotal", "delta_pct": 8.3, "actual": 1,
     "target": 1},
]
mae_off, _ = al.component_mae_pct(offsetting)
check(mae_off == 86.0,
      f"offsetting errors read as 86% MAE, not +8.3% 'in band': {mae_off}")

# --- provenance -------------------------------------------------------------
stamped = {"analysis": {"run_fingerprint": {
    "flag_fingerprint": "abc123def456", "git_sha": "0686ddcd4044"}}}
rec = al.job_record("case_x", stamped, rows, subtotal=101,
                    subtotal_delta_pct=1.0)
check(rec["flag_fingerprint"] == "abc123def456",
      "fingerprint comes from the result's own stamp")
check(rec["git_sha"] == "0686ddcd4044", "git sha from the stamp")
check(rec["in_band_10"] is True, "band computed from subtotal delta")
check("cost_estimate_subtotal" not in rec["components"],
      "components dict excludes subtotal rollups")

unstamped = al.job_record("case_y", {"analysis": {}}, rows)
check(unstamped["flag_fingerprint"] is None,
      "pre-stamp result gets an honest blank, not today's env")

# --- append + load ----------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "hist", "accuracy_history.jsonl")
    e1 = al.append_entry("test_harness", [rec], history_path=path)
    al.append_entry("test_harness", [unstamped], history_path=path,
                    extra={"note": "second"})
    loaded = al.load_history(path)
    check(len(loaded) == 2, f"two entries round-trip: {len(loaded)}")
    check(loaded[0]["source"] == "test_harness", "source recorded")
    check(loaded[0]["ts"] and "T" in loaded[0]["ts"], "UTC timestamp recorded")
    check(loaded[1].get("note") == "second", "extra fields ride along")
    check(json.dumps(e1) is not None, "entry is JSON-serializable")

print()
if fails:
    print(f"❌ {len(fails)} accuracy ledger check(s) failed")
    sys.exit(1)
print("✅ all accuracy ledger checks passed")
