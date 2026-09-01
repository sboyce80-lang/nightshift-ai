#!/usr/bin/env python3
"""Held jobs don't burn freemium credits while blanket review is on.

NIGHTSHIFT_MANDATORY_REVIEW holds EVERY estimate during the accuracy
rollout, and needs_review counted toward the freemium quota — so a
trial user could burn all 5 lifetime credits and hit the paywall having
never received an automated estimate. Same principle the code already
applies to failed runs: the user did nothing wrong, we chose to hold
the bid. Steven's call, 2026-09-01.

Locks in: held jobs free while the flag is on, counted when it's off
(there needs_review means THIS job's own checks fired), delivered bids
always count, failed/cancelled always free, revisions never count.
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


print("1) Blanket review ON: held bids are free")
_set_flag(True)
s, org = seed(["needs_review"] * 5)
used = orgs.count_freemium_bids(s, org.id)
check(used == 0, f"5 held bids must cost nothing, got {used}")
s.close()

print("\n2) Blanket review ON: delivered bids still count")
s, org = seed(["completed", "completed", "needs_review", "needs_review"])
used = orgs.count_freemium_bids(s, org.id)
check(used == 2, f"only the 2 delivered bids count, got {used}")
s.close()

print("\n3) Blanket review OFF: held bids count (this job's own checks fired)")
_set_flag(False)
s, org = seed(["needs_review", "needs_review", "completed"])
used = orgs.count_freemium_bids(s, org.id)
check(used == 3, f"legacy behavior when the blanket flag is off, got {used}")
s.close()

print("\n4) failed/cancelled always free, revisions never count")
for flag in (True, False):
    _set_flag(flag)
    s, org = seed(["failed", "cancelled", "completed"])
    used = orgs.count_freemium_bids(s, org.id)
    check(used == 1,
          f"flag={flag}: only the completed root bid counts, got {used}")
    s.close()

print("\n5) The paywall follows the same rule")
_set_flag(True)
config.PLG_SELF_SERVE_ENABLED = True
config.FREEMIUM_BID_LIMIT = 5
s, org = seed(["needs_review"] * 5)
used, limit, blocked = orgs.freemium_quota_state(s, org)
check(used == 0 and blocked is False,
      f"a user with 5 held bids must NOT be paywalled: "
      f"used={used} blocked={blocked}")
s.close()
s, org = seed(["completed"] * 5)
used, limit, blocked = orgs.freemium_quota_state(s, org)
check(used == 5 and blocked is True,
      f"5 delivered bids still hits the wall: used={used} blocked={blocked}")
s.close()

_set_flag(False)
print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ freemium held-jobs checks passed")
