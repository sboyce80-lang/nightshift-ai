#!/usr/bin/env python3
"""Replay board (replay_board.py) — Phase 2 evaluation infrastructure.

Locks in: the registry composes all three sources (Rider 8 + JW 5 +
board-local Northwell/Academy = 15 cases); Academy is raster-class and
outside the vector program; Northwell is tier-1 with JW's component key;
scoring computes component rows and MAE through the unified ledger; the
tier-1 exit streak counts only replay_board entries, requires every
scored tier-1 vector job under the gate, and breaks on any miss.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import replay_board as rb  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


print("replay board checks")

reg = rb._registry()
check(len(reg) == 15, f"registry holds 15 golden cases: {len(reg)}")
check(reg["academy_88"]["program"] == "raster-excluded",
      "Academy is declared raster-class, outside the ±10% program")
nw = reg["jw_northwell_phelps"]
check(nw["tier"] == 1 and
      nw["targets"]["total_doors_full_paint"][0] == 78 and
      nw["targets"]["total_paintable_wall_sqft"][0] == 20308,
      "Northwell is tier-1 with JW's component key")
t1 = [cid for cid, c in reg.items()
      if c["tier"] == 1 and c["program"] == "vector"]
check(len(t1) == 11, f"11 tier-1 vector jobs: {len(t1)}")

# scoring through the ledger
import json
import tempfile
res = {"analysis": {"aggregated_totals": {
    "total_paintable_wall_sqft": 900.0,
    "total_doors_full_paint": 10.0},
    "manual_review_required": True},
    "cost_estimate": {"subtotal": 1000.0}}
case = {"tier": 1, "cls": "test", "program": "vector",
        "targets": {"cost_estimate_subtotal": (1000.0, 0.10),
                    "total_paintable_wall_sqft": (1000.0, 0.10),
                    "total_doors_full_paint": (10, 0.10)}}
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump(res, f)
    tmp = f.name
rec = rb.score("test_case", case, tmp)
os.unlink(tmp)
check(rec["component_mae_pct"] == 5.0,
      f"component MAE over quantity rows only: {rec['component_mae_pct']}")
check(rec["subtotal_delta_pct"] == 0.0 and rec["in_band_10"],
      "subtotal recorded but not part of MAE")
check(rec["manual_review"] is True,
      "manual_review via the both-level ledger read")
check(rec["tier"] == 1 and rec["program"] == "vector",
      "tier/program ride on the record")

# streak logic


def entry(mae_by_job, source="replay_board"):
    return {"source": source,
            "jobs": [{"tier": 1, "program": "vector",
                      "component_mae_pct": m, "case": c}
                     for c, m in mae_by_job.items()]}


hist = [entry({"a": 8.0, "b": 9.9}), entry({"a": 7.0, "b": 6.0})]
check(rb.tier1_streak(hist) == 2, "two clean boards → streak 2")
hist = [entry({"a": 8.0}), entry({"a": 12.0}), entry({"a": 9.0})]
check(rb.tier1_streak(hist) == 1,
      "a failing board breaks the streak (only the latest counts)")
hist = [entry({"a": 5.0}, source="run_jw_golden"), entry({"a": 25.0})]
check(rb.tier1_streak(hist) == 0,
      "non-board ledger entries don't count toward the streak")
check(rb.tier1_streak([]) == 0, "empty history → streak 0")

print()
if fails:
    print(f"❌ {len(fails)} replay board check(s) failed")
    sys.exit(1)
print("✅ all replay board checks passed")
