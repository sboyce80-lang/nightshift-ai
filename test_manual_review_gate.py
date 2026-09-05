"""The manual-review delivery gate must see the flag wherever it lives.

Regression for the 5/29–9/4 dead-gate window: run_analysis() returns the
flag on result["analysis"] only (the top-level copy exists just in the
JSON written to disk/R2), while the gate in jobs.py read the top level of
the in-memory dict. Every flagged estimate auto-emailed: Otto and BofA on
7/21, then BASC, MCC and Toyota (org 12) on 9/1–9/4 — all three carrying
explicit do-not-send reasons.
"""

from jobs import manual_review_flagged, manual_review_reason


def test_flag_on_analysis_only_is_seen():
    # The exact shape run_analysis returned before the promotion fix —
    # and the shape any older/other caller may still produce.
    result = {
        "analysis": {
            "manual_review_required": True,
            "manual_review_reason": "[MANUAL REVIEW REQUIRED] ratio 1.0x",
        },
        "cost_estimate": {"subtotal": 40264.81},
    }
    assert manual_review_flagged(result) is True
    assert manual_review_reason(result) == "[MANUAL REVIEW REQUIRED] ratio 1.0x"


def test_flag_on_top_level_is_seen():
    result = {"manual_review_required": True,
              "manual_review_reason": "top-level reason",
              "analysis": {}}
    assert manual_review_flagged(result) is True
    assert manual_review_reason(result) == "top-level reason"


def test_top_level_reason_wins_when_both_present():
    result = {"manual_review_required": True,
              "manual_review_reason": "top",
              "analysis": {"manual_review_required": True,
                           "manual_review_reason": "nested"}}
    assert manual_review_flagged(result) is True
    assert manual_review_reason(result) == "top"


def test_unflagged_result_passes():
    result = {"analysis": {"manual_review_required": False},
              "cost_estimate": {"subtotal": 100.0}}
    assert manual_review_flagged(result) is False
    assert manual_review_reason(result) is None


def test_missing_keys_pass():
    assert manual_review_flagged({"analysis": {}}) is False
    assert manual_review_flagged({}) is False
    assert manual_review_reason({}) is None


def test_non_dict_shapes_do_not_crash():
    assert manual_review_flagged(None) is False
    assert manual_review_flagged("oops") is False
    assert manual_review_flagged({"analysis": "not-a-dict"}) is False
    assert manual_review_reason(None) is None
    assert manual_review_reason({"analysis": "not-a-dict"}) is None
