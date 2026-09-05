#!/usr/bin/env python3
"""Held (needs_review) jobs count against the freemium quota.

Steven, 2026-09-04, reversing the 2026-09-01 held-jobs-free exemption:
a held job ran and is in the customer's hands, so it burns a credit.
The old exemption let a bid parked in review temporarily release its
credit — Profeta reached 6/5 by submitting while an earlier bid sat in
needs_review. The lifetime cap is hard: any root submission that didn't
fail or get cancelled counts, regardless of NIGHTSHIFT_MANDATORY_REVIEW.

Locks in: held jobs count with the blanket flag on AND off, delivered
bids count, failed/cancelled always free, revisions never count, and
the paywall follows the same rule.
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


import config  # noqa: E402
import orgs  # noqa: E402
from models import Base, Organization, Submission, User  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

engine = create_engine("sqlite://")
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


def seed(statuses):
    s = Session()
    org = Organization(name="Trial Painter Co", plan="freemium")
    s.add(org)
    s.flush()
    user = User(email=f"trial{org.id}@example.com", name="Trial")
    s.add(user)
    s.flush()
    for st in statuses:
        s.add(Submission(org_id=org.id, user_id=user.id, status=st,
                         parent_submission_id=None))
    # a revision of the first bid — never counts
    s.flush()
    first = s.query(Submission).first()
    s.add(Submission(org_id=org.id, user_id=user.id, status="completed",
                     parent_submission_id=first.id))
    s.commit()
    return s, org


def _set_flag(on):
    if on:
        os.environ["NIGHTSHIFT_MANDATORY_REVIEW"] = "1"
    else:
        os.environ.pop("NIGHTSHIFT_MANDATORY_REVIEW", None)


print("1) Held bids count, blanket review ON or OFF")
for flag in (True, False):
    _set_flag(flag)
    s, org = seed(["needs_review"] * 5)
    used = orgs.count_freemium_bids(s, org.id)
    check(used == 5, f"flag={flag}: 5 held bids must count, got {used}")
    s.close()

print("\n2) Mixed statuses: everything but failed/cancelled counts")
s, org = seed(["completed", "completed", "needs_review", "queued",
               "processing", "failed", "cancelled"])
used = orgs.count_freemium_bids(s, org.id)
check(used == 5, f"5 non-failed root bids must count, got {used}")
s.close()

print("\n3) failed/cancelled always free, revisions never count")
for flag in (True, False):
    _set_flag(flag)
    s, org = seed(["failed", "cancelled", "completed"])
    used = orgs.count_freemium_bids(s, org.id)
    check(used == 1,
          f"flag={flag}: only the completed root bid counts, got {used}")
    s.close()

print("\n4) The paywall follows the same rule")
_set_flag(True)
config.PLG_SELF_SERVE_ENABLED = True
config.FREEMIUM_BID_LIMIT = 5
s, org = seed(["needs_review"] * 5)
used, limit, blocked = orgs.freemium_quota_state(s, org)
check(used == 5 and blocked is True,
      f"5 held bids must hit the wall: used={used} blocked={blocked}")
s.close()
s, org = seed(["needs_review"] * 4)
used, limit, blocked = orgs.freemium_quota_state(s, org)
check(used == 4 and blocked is False,
      f"4 held bids must NOT be paywalled: used={used} blocked={blocked}")
s.close()

_set_flag(False)
print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ freemium held-jobs checks passed")
