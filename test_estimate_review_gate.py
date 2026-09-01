#!/usr/bin/env python3
"""A not-cleared takeoff must never render as a clean, caveat-free bid.

2026-09-01 (Profeta Painting / 168 Holley St — the first PLG self-serve job):
the pipeline set ready_to_send=false, route_to_human_review=true,
manual_review_required=true and calibrated confidence 24 (+/-54%), because
4 plan pages failed extraction and the entire exterior scope priced at $0.
The branded estimate still rendered as a clean $34,139.02 bid carrying none
of the 20 exclusions or 19 RFIs, over a "Trim, doors, and windows" row that
priced zero doors while promising "doors, and frames as scheduled".

Locks in: (1) the DRAFT banner fires off the pipeline's own routing flags,
(2) job-specific exclusions and open items travel with the price,
(3) the trim row's scope names only what it actually priced,
(4) a clean, cleared job still renders with no banner.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate_estimate_pdf as G

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


# --- 1) The Profeta failure shape trips the gate on every available signal.
profeta = {
    "manual_review_required": True,
    "manual_review_reason": "4 page(s) could not be analyzed and are MISSING",
    "analysis": {"calibrated_confidence": {"predicted_error_pct": 54.2,
                                           "confidence_level": 24}},
    "will_synthesis": {"pipeline_flags": {"ready_to_send": False,
                                          "route_to_human_review": True,
                                          "missing_information": ["Sheet A-301"]}},
}
rev = G._review_state(profeta)
check(rev["needs_review"], "Profeta shape did not trip the review gate")
blob = " ".join(rev["reasons"])
check("ready_to_send" in blob, f"ready_to_send not cited: {rev['reasons']}")
check("Manual review" in blob, f"manual review not cited: {rev['reasons']}")
check("54%" in blob, f"calibrated band not cited: {rev['reasons']}")

# --- 2) Each signal trips the gate on its own.
check(G._review_state({"manual_review_required": True})["needs_review"],
      "manual_review alone did not trip the gate")
check(G._review_state({"will_synthesis": {"pipeline_flags": {
          "ready_to_send": False}}})["needs_review"],
      "ready_to_send=false alone did not trip the gate")
check(G._review_state({"analysis": {"calibrated_confidence": {
          "predicted_error_pct": 40.0}}})["needs_review"],
      "a wide calibrated band alone did not trip the gate")

# --- 3) A cleared, tight job renders clean — the banner must stay rare.
clean = {
    "manual_review_required": False,
    "analysis": {"calibrated_confidence": {"predicted_error_pct": 8.0,
                                           "confidence_level": 91}},
    "will_synthesis": {"pipeline_flags": {"ready_to_send": True,
                                          "route_to_human_review": False}},
}
check(not G._review_state(clean)["needs_review"],
      f"cleared job wrongly flagged: {G._review_state(clean)['reasons']}")

# A job that predates will_synthesis must not be read as "not ready".
check(not G._review_state({"analysis": {}})["needs_review"],
      "absent will_synthesis wrongly read as not-ready")

# --- 4) Job-specific exclusions travel with the price; boilerplate does not.
payload = {
    "cost_estimate": {"exclusions": [
        {"item": "Lead paint and asbestos abatement", "reason": "By others.",
         "source": "standard"},
        {"item": "James Hardie lap siding — field paint",
         "reason": "A-301 not analyzed.", "source": "will_synthesis"},
    ]},
    "validation": {"warnings": [
        {"severity": "high", "policy_zero": True,
         "message": "Wallcovering referenced but 0 sqft extracted."},
        {"severity": "medium", "message": "Single line item is 48% of total."},
    ]},
    "will_synthesis": {"pipeline_flags": {
        "missing_information": ["Sheet A-301 Exterior Elevations"]}},
}
oi = G._open_items(payload)
check(any("Hardie" in x for x in oi["excluded"]),
      f"job-specific exclusion dropped: {oi['excluded']}")
check(not any("asbestos" in x for x in oi["excluded"]),
      f"boilerplate exclusion duplicated into open items: {oi['excluded']}")
check(any("Wallcovering" in x for x in oi["unresolved"]),
      f"high-severity warning dropped: {oi['unresolved']}")
check(not any("48% of total" in x for x in oi["unresolved"]),
      f"medium warning wrongly promoted: {oi['unresolved']}")
check(any("A-301" in x for x in oi["unresolved"]),
      f"missing_information dropped: {oi['unresolved']}")

# --- 5) The trim row names only what it priced.
base_only = {"cost_estimate": {"line_items": [
    {"item": "Base Trim - 388 LF @ $3.25", "qty": 388, "total": 1324.05}]}}
row = G._build_line_items(base_only)[0]
check("door" not in row["scope"].lower(),
      f"trim row promises doors it did not price: {row['scope']}")
check(row["title"] == "Trim, doors, and windows",
      f"bucket title changed — downstream contract: {row['title']}")

with_doors = {"cost_estimate": {"line_items": [
    {"item": "Base Trim - 388 LF @ $3.25", "qty": 388, "total": 1324.05},
    {"item": "Doors (Full Paint) - 12 EA @ $155.00", "qty": 12, "total": 1971.0}]}}
row = G._build_line_items(with_doors)[0]
check("doors and frames" in row["scope"],
      f"priced doors not named in scope: {row['scope']}")
check(row["title"] == "Trim, doors, and windows",
      f"bucket title changed — downstream contract: {row['title']}")

# Zero-qty door lines must not resurrect the promise (the Profeta shape).
zero_doors = {"cost_estimate": {"line_items": [
    {"item": "Base Trim - 388 LF @ $3.25", "qty": 388, "total": 1324.05},
    {"item": "Doors (Full Paint) - 0 EA @ $155.00", "qty": 0, "total": 0.0}]}}
row = G._build_line_items(zero_doors)[0]
check("door" not in row["scope"].lower(),
      f"zero-qty door line resurrected the doors promise: {row['scope']}")

# --- 6) Kill switch restores the pre-fix rendering.
os.environ["NIGHTSHIFT_ESTIMATE_REVIEW_GATE"] = "0"
check(not G._review_gate_enabled(), "kill switch did not disable the gate")
os.environ.pop("NIGHTSHIFT_ESTIMATE_REVIEW_GATE")
check(G._review_gate_enabled(), "gate is not on by default")

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
