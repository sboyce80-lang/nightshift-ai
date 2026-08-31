#!/usr/bin/env python3
"""Offline tests for the PLG self-serve freemium quota + lifecycle emails.

Pins the load-bearing rules of the freemium motion:

  - A "bid" is a ROOT submission that didn't fail/cancel. Failed runs
    (the pipeline's fault) and revisions of an existing project NEVER
    consume a credit.
  - The quota gate is inert while PLG_SELF_SERVE_ENABLED is off, and
    inert for 'beta'/'paid' orgs regardless — a grandfathered customer
    can never hit the paywall.
  - The exhausted / hot-lead emails send exactly once per org (atomic
    claim on the org row), so a warm-shutdown requeue can't double-send.

Run: python3 test_plg_freemium.py
"""
import os
import sys
import tempfile

_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="plg-test-"), "plg.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config  # noqa: E402
import notifications  # noqa: E402
from db import engine, session_scope  # noqa: E402
from models import Base, Organization, OrganizationMembership, Submission, User  # noqa: E402
from orgs import (  # noqa: E402
    count_freemium_bids, freemium_quota_state, is_free_email_domain,
)

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  got={got!r} want={want!r}")


def _mk_org(session, plan="freemium", approved=True):
    org = Organization(name="Test Painting Co", plan=plan,
                       is_beta_approved=approved)
    session.add(org)
    session.flush()
    user = User(email=f"owner{org.id}@testpainting.com", name="Pat Owner")
    session.add(user)
    session.flush()
    session.add(OrganizationMembership(
        organization_id=org.id, user_id=user.id, role="owner"))
    user.current_organization_id = org.id
    return org.id, user.id


def _mk_sub(session, org_id, user_id, status="completed", parent=None):
    sub = Submission(user_id=user_id, org_id=org_id, status=status,
                     parent_submission_id=parent)
    session.add(sub)
    session.flush()
    return sub.id


def test_bid_counting():
    print("bid counting:")
    with session_scope() as session:
        org_id, user_id = _mk_org(session)
        root1 = _mk_sub(session, org_id, user_id, "completed")
        _mk_sub(session, org_id, user_id, "queued")
        _mk_sub(session, org_id, user_id, "processing")
        _mk_sub(session, org_id, user_id, "needs_review")
        _mk_sub(session, org_id, user_id, "failed")
        _mk_sub(session, org_id, user_id, "cancelled")
        _mk_sub(session, org_id, user_id, "completed", parent=root1)  # revision
        check("counts queued/processing/needs_review/completed roots only",
              count_freemium_bids(session, org_id), 4)


def test_quota_gate():
    print("quota gate:")
    with session_scope() as session:
        org_id, user_id = _mk_org(session)
        for _ in range(config.FREEMIUM_BID_LIMIT):
            _mk_sub(session, org_id, user_id, "completed")
        org = session.get(Organization, org_id)

        config.PLG_SELF_SERVE_ENABLED = False
        check("flag OFF → never blocked",
              freemium_quota_state(session, org)[2], False)

        config.PLG_SELF_SERVE_ENABLED = True
        used, limit, blocked = freemium_quota_state(session, org)
        check("flag ON, freemium at limit → blocked", blocked, True)
        check("used == limit", used, limit)

        org.plan = "beta"
        check("beta plan → never blocked even over limit",
              freemium_quota_state(session, org)[2], False)
        org.plan = "paid"
        check("paid plan → never blocked",
              freemium_quota_state(session, org)[2], False)

    with session_scope() as session:
        org_id, user_id = _mk_org(session)
        for _ in range(config.FREEMIUM_BID_LIMIT - 1):
            _mk_sub(session, org_id, user_id, "completed")
        _mk_sub(session, org_id, user_id, "failed")  # free retry
        org = session.get(Organization, org_id)
        used, _limit, blocked = freemium_quota_state(session, org)
        check("failed run doesn't consume the last credit", blocked, False)
        check("used is limit-1", used, config.FREEMIUM_BID_LIMIT - 1)
    config.PLG_SELF_SERVE_ENABLED = False


def test_free_email_domain():
    print("free-email detection:")
    check("gmail is free", is_free_email_domain("a@gmail.com"), True)
    check("corporate is not", is_free_email_domain("a@riderpaintingny.com"), False)
    check("empty is treated as free", is_free_email_domain(""), True)


def test_lifecycle_idempotency():
    print("lifecycle email idempotency:")
    from jobs import maybe_send_freemium_lifecycle_emails

    sent = {"exhausted": 0, "milestone": 0}
    real_ex = notifications.notify_freemium_exhausted
    real_ms = notifications.notify_internal_freemium_milestone
    notifications.notify_freemium_exhausted = (
        lambda *a, **k: sent.__setitem__("exhausted", sent["exhausted"] + 1) or True)
    notifications.notify_internal_freemium_milestone = (
        lambda *a, **k: sent.__setitem__("milestone", sent["milestone"] + 1) or True)
    config.PLG_SELF_SERVE_ENABLED = True
    try:
        # Hot-lead threshold: fires internal alert once, no customer email.
        with session_scope() as session:
            org_id, user_id = _mk_org(session)
            for _ in range(config.FREEMIUM_HOT_LEAD_THRESHOLD):
                last = _mk_sub(session, org_id, user_id, "completed")
        maybe_send_freemium_lifecycle_emails(last)
        check("hot lead fires internal alert", sent["milestone"], 1)
        check("hot lead sends NO customer email", sent["exhausted"], 0)
        maybe_send_freemium_lifecycle_emails(last)
        check("hot lead claim is idempotent", sent["milestone"], 1)

        # Exhaustion: customer + internal, once, even across re-runs.
        with session_scope() as session:
            u = session.get(User, user_id)
            for _ in range(config.FREEMIUM_BID_LIMIT
                           - config.FREEMIUM_HOT_LEAD_THRESHOLD):
                last = _mk_sub(session, org_id, u.id, "completed")
        maybe_send_freemium_lifecycle_emails(last)
        check("exhaustion emails the owner", sent["exhausted"], 1)
        check("exhaustion alerts sales", sent["milestone"], 2)
        maybe_send_freemium_lifecycle_emails(last)
        check("exhaustion claim is idempotent", sent["exhausted"], 1)

        # Beta org at any volume: nothing fires.
        sent["exhausted"] = sent["milestone"] = 0
        with session_scope() as session:
            borg_id, buser_id = _mk_org(session, plan="beta")
            for _ in range(config.FREEMIUM_BID_LIMIT + 2):
                blast = _mk_sub(session, borg_id, buser_id, "completed")
        maybe_send_freemium_lifecycle_emails(blast)
        check("beta org never triggers freemium emails",
              (sent["exhausted"], sent["milestone"]), (0, 0))

        # Flag off: inert even for an over-limit freemium org.
        config.PLG_SELF_SERVE_ENABLED = False
        with session_scope() as session:
            forg_id, fuser_id = _mk_org(session)
            for _ in range(config.FREEMIUM_BID_LIMIT):
                flast = _mk_sub(session, forg_id, fuser_id, "completed")
        maybe_send_freemium_lifecycle_emails(flast)
        check("flag OFF → lifecycle inert",
              (sent["exhausted"], sent["milestone"]), (0, 0))
    finally:
        notifications.notify_freemium_exhausted = real_ex
        notifications.notify_internal_freemium_milestone = real_ms
        config.PLG_SELF_SERVE_ENABLED = False


def test_sales_cc():
    print("sales alert CC:")
    captured = {}

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"id": "test"}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json or {})
        return _FakeResp()

    import requests as _requests
    real_post = _requests.post
    real_key = notifications.RESEND_API_KEY
    real_from = notifications.RESEND_FROM_EMAIL
    real_to = notifications.PLG_SALES_EMAILS
    real_cc = notifications.PLG_SALES_CC_EMAILS
    _requests.post = fake_post
    notifications.RESEND_API_KEY = "test-key"
    notifications.RESEND_FROM_EMAIL = "noreply@test"
    notifications.PLG_SALES_EMAILS = frozenset({"admin@x.com"})
    notifications.PLG_SALES_CC_EMAILS = frozenset(
        {"steve@x.com", "admin@x.com"})  # admin@ dup must be dropped from CC
    try:
        notifications.notify_internal_freemium_milestone(
            "Acme", ["owner@acme.com"], 5, 5, exhausted=True)
        check("To is the sales list", captured.get("to"), ["admin@x.com"])
        check("CC set minus To dupes", captured.get("cc"), ["steve@x.com"])
        check("reply template in exhausted customer email",
              "Approximate annual revenue:" in
              notifications.PRICING_REPLY_TEMPLATE, True)
    finally:
        _requests.post = real_post
        notifications.RESEND_API_KEY = real_key
        notifications.RESEND_FROM_EMAIL = real_from
        notifications.PLG_SALES_EMAILS = real_to
        notifications.PLG_SALES_CC_EMAILS = real_cc


def main():
    Base.metadata.create_all(engine)
    test_bid_counting()
    test_quota_gate()
    test_free_email_domain()
    test_lifecycle_idempotency()
    test_sales_cc()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
