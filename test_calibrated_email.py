"""Tests for the customer-facing calibrated-confidence email block (review 5a)."""
import os
import sys

import email_processor as ep

_fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        _fails.append(name)


def _result(calibrated=None):
    a = {}
    if calibrated is not None:
        a["calibrated_confidence"] = calibrated
    return {"analysis": a}


WILL = {"confidence": {"level_pct": 92, "bid_recommendation": "bid",
                       "top_risks": ["prevailing wage", "no finish schedule"]}}


def with_flag(val, fn):
    old = os.environ.get("NIGHTSHIFT_CALIBRATED_EMAIL")
    if val is None:
        os.environ.pop("NIGHTSHIFT_CALIBRATED_EMAIL", None)
    else:
        os.environ["NIGHTSHIFT_CALIBRATED_EMAIL"] = val
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop("NIGHTSHIFT_CALIBRATED_EMAIL", None)
        else:
            os.environ["NIGHTSHIFT_CALIBRATED_EMAIL"] = old


CAL = {"predicted_error_pct": 18.6, "ci_level": 0.90, "calibrated": True}
PRIOR = {"predicted_error_pct": 27.0, "ci_level": 0.90, "calibrated": False}

print("calibrated-confidence email block")

# Flag OFF -> self-reported (back-compat / safe default)
out = with_flag(None, lambda: ep._confidence_email_block(_result(CAL), WILL))
check("flag off -> shows self-reported level", "ESTIMATOR'S CONFIDENCE: 92%" in out, out.strip()[:60])
check("flag off -> does NOT show calibrated band", "predicted within" not in out)

# Flag ON + calibrated -> honest band
out = with_flag("1", lambda: ep._confidence_email_block(_result(CAL), WILL))
check("flag on + calibrated -> ±band", "predicted within ±19%" in out, out.strip().splitlines()[0])
check("flag on -> 90% confidence", "90% confidence" in out)
check("flag on + calibrated=True -> 'verified past jobs'", "verified past jobs" in out)
check("flag on -> no self-reported 92%", "92%" not in out)
check("keeps Will's recommendation", "Recommendation: bid" in out)
check("keeps top risks", "prevailing wage" in out)

# Flag ON + prior (not yet calibrated) -> preliminary wording
out = with_flag("1", lambda: ep._confidence_email_block(_result(PRIOR), WILL))
check("flag on + calibrated=False -> 'preliminary'", "preliminary evidence estimate" in out)

# Flag ON but no calibrated_confidence -> safe fallback to self-reported
out = with_flag("1", lambda: ep._confidence_email_block(_result(None), WILL))
check("flag on + no calibrated data -> fallback to self-reported", "ESTIMATOR'S CONFIDENCE: 92%" in out)

# No confidence at all -> empty
check("no confidence -> empty block", ep._confidence_email_block(_result(CAL), {}) == "")

print(f"\n=== {'ALL PASS' if not _fails else str(len(_fails)) + ' FAILED'} ===")
sys.exit(1 if _fails else 0)
