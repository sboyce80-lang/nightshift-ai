#!/usr/bin/env python3
"""Tiered delivery reviewer (delivery_reviewer.py).

Locks in the structural constraints: runs only on suite-flagged jobs
under its own flag; consumes only verdict/findings/RFI text — nothing
numeric from the response is ever applied; a hold verdict adds manual
review; a release verdict is recorded ONLY and never clears an existing
hold; unparseable or failing calls fail safe to hold; the evidence
packet is hard-capped.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["NIGHTSHIFT_DELIVERY_REVIEWER"] = "1"

import delivery_reviewer as dr  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


class FakeMessages:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        self.kw = kw
        blk = type("B", (), {"text": self.text})
        return type("R", (), {"content": [blk()]})


class FakeClient:
    def __init__(self, text):
        self.messages = FakeMessages(text)


def flagged_analysis():
    return {"_delivery_verification": {
                "n_flags": 2,
                "checks": [{"id": "white_label", "status": "flag",
                            "detail": "x"},
                           {"id": "cross_sheet_dedup", "status": "flag",
                            "detail": "y"}]},
            "aggregated_totals": {"total_paintable_wall_sqft": 1000.0},
            "notes": [], "rfi_items": []}


print("delivery reviewer checks")

# Hold verdict adds manual review + RFIs land.
resp = json.dumps({"verdict": "hold",
                   "findings": [{"check_id": "white_label",
                                 "severity": "high",
                                 "explanation": "brand leak"}],
                   "rfis": ["Confirm door schedule"],
                   "reviewer_note": "brand copy in exclusions"})
a = flagged_analysis()
out = dr.attach_delivery_review(a, client=FakeClient(resp))
check(out.get("manual_review_required") is True,
      "hold verdict: manual review set")
check(out["_delivery_review"]["applied"] == "hold",
      "hold verdict: recorded as applied")
check(any(r.get("category") == "Delivery Review"
          for r in out.get("rfi_items", [])),
      "hold verdict: reviewer RFIs filed")

# Release verdict never clears an existing hold.
resp = json.dumps({"verdict": "release", "findings": [], "rfis": [],
                   "reviewer_note": "benign"})
a = flagged_analysis()
a["manual_review_required"] = True
a["manual_review_reason"] = "suite hold"
out = dr.attach_delivery_review(a, client=FakeClient(resp))
check(out.get("manual_review_required") is True
      and out["manual_review_reason"] == "suite hold",
      "release verdict: existing hold untouched (recorded-only)")
check(out["_delivery_review"]["applied"] == "recorded-only",
      "release verdict: marked recorded-only")

# Nothing numeric from the response is applied.
resp = json.dumps({"verdict": "hold", "findings": [],
                   "rfis": [], "reviewer_note": "walls should be 2000",
                   "total_paintable_wall_sqft": 2000,
                   "quantities": {"walls": 2000}})
a = flagged_analysis()
out = dr.attach_delivery_review(a, client=FakeClient(resp))
check(out["aggregated_totals"]["total_paintable_wall_sqft"] == 1000.0,
      "quantity injection: aggregates untouched")
check("total_paintable_wall_sqft" not in out["_delivery_review"],
      "quantity injection: numeric fields not even recorded")

# Unparseable response fails safe to hold.
a = flagged_analysis()
out = dr.attach_delivery_review(a, client=FakeClient("no json here"))
check(out["_delivery_review"]["verdict"] == "hold"
      and out.get("manual_review_required") is True,
      "unparseable response: fail-safe hold")

# API failure fails safe to hold.
class BoomClient:
    class messages:
        @staticmethod
        def create(**kw):
            raise RuntimeError("api down")

a = flagged_analysis()
out = dr.attach_delivery_review(a, client=BoomClient())
check(out["_delivery_review"]["verdict"] == "hold",
      "API failure: fail-safe hold")

# Tiering: unflagged jobs never call the model.
fc = FakeClient("{}")
a = {"_delivery_verification": {"n_flags": 0, "checks": []}}
out = dr.attach_delivery_review(a, client=fc)
check(fc.messages.calls == 0 and "_delivery_review" not in out,
      "tiering: clean jobs never pay for the reviewer")

# Flag off: inert even on flagged jobs.
os.environ["NIGHTSHIFT_DELIVERY_REVIEWER"] = "0"
fc = FakeClient("{}")
out = dr.attach_delivery_review(flagged_analysis(), client=fc)
check(fc.messages.calls == 0 and "_delivery_review" not in out,
      "flag off: inert")
os.environ["NIGHTSHIFT_DELIVERY_REVIEWER"] = "1"

# Packet is capped and bounded call params hold.
a = flagged_analysis()
a["notes"] = ["x" * 5000] * 30
packet = dr.build_review_packet(a)
check(len(packet) <= dr._PACKET_CAP_CHARS + 20,
      "packet: hard character cap")
fc = FakeClient(json.dumps({"verdict": "hold", "findings": [],
                            "rfis": [], "reviewer_note": ""}))
dr.attach_delivery_review(a, client=fc)
check(fc.messages.kw.get("max_tokens") == dr._MAX_TOKENS
      and fc.messages.kw.get("temperature") == 0,
      "call: bounded max_tokens, temperature 0")

print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all delivery reviewer checks passed")
