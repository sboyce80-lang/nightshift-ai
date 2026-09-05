#!/usr/bin/env python3
"""Aggregate-drift shadow audit (NIGHTSHIFT_AGG_DRIFT_AUDIT).

aggregated_totals and the room inventory are dual sources of truth
reconciled by hand inside ~30 gates; a mis-decrement clamps silently to
zero. The audit diffs the chain's aggregates against a clean room-data
recompute at the end of build_priced_takeoff. Locks in: the live analysis
is never mutated by the recompute; drift is recorded with per-key detail;
legit-equal totals record zero drift; the review escalation only fires
when its own flag is on; audit failure can never fail the job.
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


def rm(name, wall, ceil=0.0):
    return {"room_name": name, "in_scope": True, "source_sheet": "A1.1",
            "dimensions": {"wall_area_sqft": float(wall),
                           "ceiling_area_sqft": float(ceil)},
            "materials": {"walls": "gypsum", "ceiling": ""},
            "ceiling_painted": bool(ceil),
            "elements": {}}


def analysis(agg_walls):
    """Two rooms totalling 1,000 SF walls; the chain's ledger says
    agg_walls. Idempotency flags pre-set so the recompute only re-sums,
    the way it would at the end of a real run."""
    return {
        "project_info": {"building_type": "commercial"},
        "floors": [{"floor_name": "1st Floor",
                    "rooms": [rm("Office", 600), rm("Lobby", 400)]}],
        "aggregated_totals": {"total_paintable_wall_sqft": float(agg_walls)},
        "_template_floors_deduped": True,
        "_residential_corridor_ceiling_fixed": True,
    }


print("aggregate drift audit checks")
for k in ("NIGHTSHIFT_AGG_DRIFT_AUDIT", "NIGHTSHIFT_AGG_DRIFT_REVIEW",
          "NIGHTSHIFT_AGG_DRIFT_REVIEW_PCT"):
    os.environ.pop(k, None)

# --- default-on shadow recording -------------------------------------------
a = analysis(agg_walls=1000.0)
baseline = T._recalculate_totals(
    __import__("copy").deepcopy(a))["aggregated_totals"]
recomputed_walls = baseline.get("total_paintable_wall_sqft")
check(recomputed_walls == 1000.0,
      f"fixture sanity: recompute reads 1000 SF, got {recomputed_walls}")

a = analysis(agg_walls=1000.0)
a = T._audit_aggregate_drift(a)
drift = a.get("_agg_drift")
check(isinstance(drift, dict), "audit records _agg_drift by default")
check(drift.get("n_keys_drifted") == 0 or
      "total_paintable_wall_sqft" not in drift.get("keys", {}),
      "agreeing totals record no wall drift")
check(not a.get("manual_review_required"),
      "no review hold when totals agree")

# --- drift detection --------------------------------------------------------
# The chain says 700 SF; the rooms say 1,000. A gate dropped rooms' worth
# of aggregate (or vice versa) — the exact hand-decrement failure family.
a = analysis(agg_walls=700.0)
a = T._audit_aggregate_drift(a)
d = a["_agg_drift"]["keys"].get("total_paintable_wall_sqft")
check(d is not None, "30% wall drift is recorded")
check(d and d["chain"] == 700.0 and d["room_recompute"] == 1000.0,
      f"both values recorded: {d}")
check(d and d["drift_pct"] == -30.0,
      f"signed drift pct: got {d and d['drift_pct']}")
check(a["_agg_drift"]["max_abs_drift_pct"] == 30.0, "max drift summarized")
check(not a.get("manual_review_required"),
      "shadow mode records but never holds")

# --- the recompute must not touch the live analysis ------------------------
a = analysis(agg_walls=700.0)
a = T._audit_aggregate_drift(a)
check(a["aggregated_totals"]["total_paintable_wall_sqft"] == 700.0,
      "live aggregates unchanged by the audit")
check(len(a["floors"][0]["rooms"]) == 2, "live rooms unchanged by the audit")

# --- review escalation, only behind its own flag ---------------------------
os.environ["NIGHTSHIFT_AGG_DRIFT_REVIEW"] = "1"
a = analysis(agg_walls=700.0)
a["manual_review_reason"] = "prior reason"
a = T._audit_aggregate_drift(a)
check(a.get("manual_review_required") is True,
      "review flag on: 30% drift holds the job")
check("prior reason |" in a.get("manual_review_reason", ""),
      "hold reason appends, never clobbers")
check(any("[Aggregate Drift]" in n for n in a.get("notes", [])),
      "drift note names the divergence")

a = analysis(agg_walls=950.0)  # 5% — under the 10% default threshold
a = T._audit_aggregate_drift(a)
check(not a.get("manual_review_required"),
      "sub-threshold drift records without holding")
os.environ.pop("NIGHTSHIFT_AGG_DRIFT_REVIEW", None)

# --- kill switch ------------------------------------------------------------
os.environ["NIGHTSHIFT_AGG_DRIFT_AUDIT"] = "0"
a = analysis(agg_walls=700.0)
a = T._audit_aggregate_drift(a)
check("_agg_drift" not in a, "kill switch: audit fully inert")
os.environ.pop("NIGHTSHIFT_AGG_DRIFT_AUDIT", None)

# --- the audit is a thermometer, never a tourniquet ------------------------
broken = {"aggregated_totals": {"total_paintable_wall_sqft": 100.0},
          "floors": "not-a-list"}
out = T._audit_aggregate_drift(broken)
check(out is broken and isinstance(out.get("_agg_drift"), dict),
      "a crash inside the audit records an error and returns the analysis")

# --- end-to-end: runs as the last chain step -------------------------------
a = analysis(agg_walls=700.0)
a = T.build_priced_takeoff(a)
check(isinstance(a.get("_agg_drift"), dict),
      "build_priced_takeoff ends with the drift audit")
check(a.get("_priced_takeoff_built") is True, "chain still completes")

print()
if fails:
    print(f"❌ {len(fails)} aggregate drift check(s) failed")
    sys.exit(1)
print("✅ all aggregate drift audit checks passed")
