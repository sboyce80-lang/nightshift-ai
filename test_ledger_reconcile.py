#!/usr/bin/env python3
"""Ledger-reconciliation invariant (_reconcile_quantity_ledger).

Locks in: a contiguous ledger chain is clean; a mid-chain gap (a gate
mutated aggregates between ledgered stages without recording it) and a
tail gap (a mutation after the final ledgered stage) are both caught with
the right key and magnitude; the review escalation holds the job only
when enabled AND the gap clears the threshold; the kill switch removes
the record entirely; a malformed ledger can never fail the job.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["NIGHTSHIFT_LEDGER_RECONCILE"] = "1"
os.environ.pop("NIGHTSHIFT_LEDGER_RECONCILE_REVIEW", None)

import Takeoff_DIRECT as T  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def led(stage, item, frm, to, source="schedule"):
    return {"stage": stage, "item": item, "from": frm, "to": to,
            "delta": round(to - frm, 2), "source": source, "basis": "test"}


def analysis_with(agg, ledger):
    return {"aggregated_totals": dict(agg),
            "_quantity_adjustments": list(ledger)}


print("ledger reconcile checks")

# --- Contiguous chain, final matches: clean. ---
a = analysis_with(
    {"total_doors_full_paint": 78},
    [led("aggregation", "total_doors_full_paint", 0, 74, "measured"),
     led("schedule_overrides", "total_doors_full_paint", 74, 78)])
out = T._reconcile_quantity_ledger(a)
rec = out.get("_ledger_reconcile")
check(rec is not None and rec.get("n_keys") == 0,
      "clean chain: contiguous ledger with matching final records no keys")

# --- Tail gap: aggregate moved after the last ledgered stage. ---
a = analysis_with(
    {"total_doors_hm_panel": 17},
    [led("schedule_overrides", "total_doors_hm_panel", 6, 7)])
out = T._reconcile_quantity_ledger(a)
rec = out.get("_ledger_reconcile", {})
bad = rec.get("unledgered", {}).get("total_doors_hm_panel")
check(bad is not None, "tail gap: post-schedule write is caught")
check(bad and bad["gaps"][-1]["where"] == "after final ledgered stage"
      and bad["gaps"][-1]["gap"] == 10.0,
      "tail gap: located after final stage with gap=+10")
check(not out.get("manual_review_required"),
      "tail gap: review OFF by default — records, does not hold")

# --- Mid gap: a stage saw a `from` the previous stage never wrote. ---
a = analysis_with(
    {"total_paintable_wall_sqft": 900},
    [led("aggregation", "total_paintable_wall_sqft", 0, 1000, "measured"),
     led("dedup", "total_paintable_wall_sqft", 700, 900, "correction")])
out = T._reconcile_quantity_ledger(a)
bad = out.get("_ledger_reconcile", {}).get("unledgered", {}) \
         .get("total_paintable_wall_sqft")
check(bad is not None and "before stage 'dedup'" in bad["gaps"][0]["where"],
      "mid gap: unledgered write between stages caught at the right stage")

# --- Review escalation: only when enabled and above threshold. ---
os.environ["NIGHTSHIFT_LEDGER_RECONCILE_REVIEW"] = "1"
os.environ["NIGHTSHIFT_LEDGER_RECONCILE_REVIEW_PCT"] = "10"
a = analysis_with(
    {"total_windows_field_paintable": 0},
    [led("schedule_overrides", "total_windows_field_paintable", 82, 71)])
out = T._reconcile_quantity_ledger(a)
check(out.get("manual_review_required") is True,
      "review ON: schedule value zeroed outside the ledger holds the job")
check("outside the ledger" in (out.get("manual_review_reason") or ""),
      "review ON: reason names the unledgered write")

a = analysis_with(
    {"total_base_trim_lf": 1004},
    [led("aggregation", "total_base_trim_lf", 0, 1000, "measured")])
out = T._reconcile_quantity_ledger(a)
check(not out.get("manual_review_required"),
      "review ON: sub-threshold gap (0.4%) records but does not hold")
os.environ.pop("NIGHTSHIFT_LEDGER_RECONCILE_REVIEW", None)
os.environ.pop("NIGHTSHIFT_LEDGER_RECONCILE_REVIEW_PCT", None)

# --- Prior review reason is preserved, not clobbered. ---
os.environ["NIGHTSHIFT_LEDGER_RECONCILE_REVIEW"] = "1"
a = analysis_with(
    {"total_doors_hm_panel": 17},
    [led("schedule_overrides", "total_doors_hm_panel", 6, 7)])
a["manual_review_required"] = True
a["manual_review_reason"] = "prior reason"
out = T._reconcile_quantity_ledger(a)
check((out.get("manual_review_reason") or "").startswith("prior reason | "),
      "review ON: existing review reason is appended to, not replaced")
os.environ.pop("NIGHTSHIFT_LEDGER_RECONCILE_REVIEW", None)

# --- Kill switch. ---
os.environ["NIGHTSHIFT_LEDGER_RECONCILE"] = "0"
a = analysis_with(
    {"total_doors_hm_panel": 17},
    [led("schedule_overrides", "total_doors_hm_panel", 6, 7)])
out = T._reconcile_quantity_ledger(a)
check("_ledger_reconcile" not in out,
      "kill switch: NIGHTSHIFT_LEDGER_RECONCILE=0 records nothing")
os.environ["NIGHTSHIFT_LEDGER_RECONCILE"] = "1"

# --- Malformed ledger can never fail the job. ---
a = {"aggregated_totals": {"total_doors_full_paint": 5},
     "_quantity_adjustments": [{"item": "total_doors_full_paint",
                                "from": "garbage", "to": None}]}
try:
    out = T._reconcile_quantity_ledger(a)
    check(isinstance(out, dict),
          "resilience: malformed ledger entries never raise")
except Exception as e:  # pragma: no cover
    check(False, f"resilience: raised {e}")

# --- No ledger at all: no record, no crash. ---
out = T._reconcile_quantity_ledger({"aggregated_totals": {"x": 1}})
check("_ledger_reconcile" not in out,
      "no ledger: absent _quantity_adjustments is a no-op")

print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all ledger reconcile checks passed")
