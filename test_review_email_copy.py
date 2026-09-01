#!/usr/bin/env python3
"""The review-hold email must not tell healthy jobs they failed.

NIGHTSHIFT_MANDATORY_REVIEW holds EVERY estimate during the accuracy
rollout, so send_manual_review_email now fires on healthy jobs too —
and with self-serve signup live, that email is a new customer's entire
first impression. The original copy ("unlikely to reflect the full
painting scope", "don't act on any preliminary numbers", plus our
internal rollout sentence quoted back at them) is wrong for a job where
nothing failed. Locks in: policy holds get confident review copy; real
sanity-check failures keep the original warning copy.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


sent = {}


class _FakeSMTP:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg):
        sent["subject"] = msg["Subject"]
        payload = msg.get_payload()[0]
        raw = payload.get_payload(decode=True)
        sent["body"] = (raw.decode("utf-8", "replace") if raw
                        else payload.get_payload())


import jobs  # noqa: E402

jobs.EMAIL_ADDRESS = "test@knightshiftai.com"
jobs.EMAIL_APP_PASSWORD = "x"
jobs.smtplib.SMTP = _FakeSMTP

CONTACT = {"name": "Dana", "email": "dana@example.com",
           "business_name": "Riverside Retail Fitout"}

POLICY_ONLY = {
    "manual_review_required": True,
    "analysis": {"notes": [
        "[Mandatory Review] Reviewer sign-off required on every estimate "
        "under the current accuracy rollout — no estimate ships "
        "unreviewed regardless of confidence."]},
    "cost_estimate": {"subtotal": 48000},
}
REAL_FAILURE = {
    "manual_review_required": True,
    "manual_review_reason": (
        "[MANUAL REVIEW REQUIRED] Total extracted paintable surface "
        "(4,200 sqft) is implausibly low relative to building footprint "
        "(18,000 sqft) — ratio is 0.2x, expected 3-6x."),
    "analysis": {"notes": ["[Mandatory Review] Reviewer sign-off required."]},
    "cost_estimate": {"subtotal": 12000},
}

print("1) Policy hold — healthy job, blanket review")
sent.clear()
jobs.send_manual_review_email(CONTACT, POLICY_ONLY, "sub-101")
body, subj = sent.get("body", ""), sent.get("subject", "")
check("in review" in subj.lower() and "not auto-sent" not in subj.lower(),
      f"subject must not shout failure: {subj}")
for bad in ("unlikely to reflect", "don't act on any preliminary",
            "sheets were missed", "didn't render", "NOT been validated",
            "accuracy rollout", "confidence check"):
    check(bad.lower() not in body.lower(),
          f"policy copy must not contain {bad!r}")
check("reviewed by one of our estimators" in body,
      "policy copy explains the human review as deliberate")
check("sub-101" in body, "submission id present")

print("\n2) Real sanity-check failure keeps the honest warning")
sent.clear()
jobs.send_manual_review_email(CONTACT, REAL_FAILURE, "sub-202")
body, subj = sent.get("body", ""), sent.get("subject", "")
check("flagged for manual" in subj.lower(),
      f"failure subject unchanged: {subj}")
check("implausibly low" in body,
      "the real reason is still shown to the customer")
check("NOT been validated" in body,
      "failure copy keeps the do-not-act warning")
check("[MANUAL REVIEW REQUIRED]" not in body,
      "internal marker still trimmed from the body")

print("\n3) Neither variant leaks internal rollout language")
check("regardless of confidence" not in body.lower(),
      "policy sentence never quoted to a customer")

print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ review-email copy checks passed")
