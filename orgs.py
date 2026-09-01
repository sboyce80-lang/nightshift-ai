#!/usr/bin/env python3
"""
Knight Shift — Organization Provisioning Helpers
================================================
Auto-provisioning logic for assigning a newly authenticated user to the
right organization. Mirrors the rules used by Alembic migration 0003 so
new sign-ups end up in the same shape as users migrated by the backfill.

Rules:
    free-email domain (gmail/yahoo/etc.) -> create a personal org
    corporate domain w/ existing org      -> join as member
    corporate domain, no existing org     -> create new org as owner,
                                             verified_at = NULL until the
                                             webmaster@ verification flow
                                             ships
"""

import os
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy.orm import Session

import config
from models import Organization, OrganizationMembership, Submission, User


# Free-email providers that should land each user in their own personal org
# rather than getting grouped under a shared "domain" tenant.
FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "yahoo.co.uk", "outlook.com", "hotmail.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "live.com", "msn.com",
    "protonmail.com", "proton.me", "pm.me",
})

# Override the auto-derived org name for these specific domains so the
# admin doesn't land on a clunky placeholder ("Riderpaintingny") and have
# to rename it manually on first login.
DOMAIN_TO_NAME = {
    "riderpaintingny.com": "Rider Painting",
}


def _domain_of(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return email.rsplit("@", 1)[1].strip().lower()


def _humanize_domain(domain: str) -> str:
    """riderpaintingny.com -> 'Riderpaintingny'; smith-co.com -> 'Smith Co'."""
    label = domain.split(".")[0] if domain else ""
    return label.replace("-", " ").replace("_", " ").title() or domain


def provision_org_for_user(session: Session, user: User) -> Organization:
    """Ensure `user` has a current organization. Idempotent.

    If `user.current_organization_id` is already set, returns that org.
    Otherwise creates or joins one based on the user's email domain and
    sets `current_organization_id` accordingly. Caller is responsible for
    flushing/committing the surrounding session.
    """
    if user.current_organization_id is not None:
        org = session.get(Organization, user.current_organization_id)
        if org is not None:
            return org
        # Stale FK — fall through and re-provision.
        user.current_organization_id = None

    email = (user.email or "").lower()
    domain = _domain_of(email)
    is_personal = (not domain) or (domain in FREE_EMAIL_DOMAINS)
    now = datetime.now(timezone.utc)

    if is_personal:
        org = Organization(
            name=user.name or user.email or f"user-{user.id}",
            email_domain=None,
            is_personal=True,
            verified_at=now,  # personal orgs trivially "verified"
        )
        session.add(org)
        session.flush()  # need org.id for the membership row
        role = "owner"
    else:
        org = (session.query(Organization)
                      .filter(Organization.email_domain == domain)
                      .one_or_none())
        if org is None:
            org = Organization(
                name=DOMAIN_TO_NAME.get(domain) or _humanize_domain(domain),
                email_domain=domain,
                is_personal=False,
                # Verified flag stays NULL until the webmaster@ flow ships;
                # no inviting capability is gated on it yet, so we don't
                # block sign-up.
                verified_at=None,
            )
            session.add(org)
            session.flush()
            role = "owner"
        else:
            role = "member"

    session.add(OrganizationMembership(
        organization_id=org.id,
        user_id=user.id,
        role=role,
    ))
    user.current_organization_id = org.id
    return org


# ---------------------------------------------------------------------------
# Freemium quota (PLG self-serve motion)
# ---------------------------------------------------------------------------

def is_free_email_domain(email: str) -> bool:
    """True when `email` is on a free provider (or has no domain at all)."""
    domain = _domain_of(email or "")
    return (not domain) or (domain in FREE_EMAIL_DOMAINS)


def _blanket_review_on() -> bool:
    """True when NIGHTSHIFT_MANDATORY_REVIEW holds every estimate.

    Read at call time (not import) so tests and a mid-rollout flag flip
    both take effect without a restart.
    """
    return os.environ.get(
        "NIGHTSHIFT_MANDATORY_REVIEW", "0").strip() in ("1", "true", "True")


def count_freemium_bids(session: Session, org_id: int) -> int:
    """Lifetime bid count for the freemium quota.

    A "bid" is a ROOT submission (parent_submission_id IS NULL) that didn't
    fail or get cancelled — i.e. queued/processing/needs_review/completed.
    Failed runs never consume a credit (the pipeline fail-safes are our
    fault, not the user's), and revisions of an already-counted bid are the
    same project, not a new one. The 24h daily cap still bounds revision
    volume.

    HELD JOBS ARE FREE WHILE BLANKET REVIEW IS ON (Steven, 2026-09-01).
    NIGHTSHIFT_MANDATORY_REVIEW holds EVERY estimate during the accuracy
    rollout, so without this exemption a trial user burns all
    FREEMIUM_BID_LIMIT credits and hits the paywall having never received
    an automated estimate — their account holding nothing but
    "in review" notices. Same principle as the failed-run exemption
    above: the user did nothing wrong, we chose to hold the bid. A held
    bid starts counting once a reviewer delivers it (status leaves
    needs_review). When the blanket flag is off, needs_review means the
    pipeline's own checks fired on THIS job and the credit stands.
    """
    excluded = ["failed", "cancelled"]
    if _blanket_review_on():
        excluded.append("needs_review")
    return (
        session.query(Submission)
        .filter(
            Submission.org_id == org_id,
            Submission.parent_submission_id.is_(None),
            Submission.status.notin_(tuple(excluded)),
        )
        .count()
    )


def freemium_quota_state(session: Session,
                         org: Optional[Organization]) -> Tuple[int, int, bool]:
    """Return (bids_used, limit, blocked) for `org`.

    blocked is True only when the PLG flag is on, the org is on the
    'freemium' plan, and its lifetime bid count has reached the limit.
    'beta' and 'paid' orgs (and everything while the flag is off) always
    come back (0, limit, False) — the gate is inert for them.

    Reads config at call time (not import time) so the flag is
    monkeypatchable in tests.
    """
    limit = config.FREEMIUM_BID_LIMIT
    if not config.PLG_SELF_SERVE_ENABLED or org is None or org.plan != "freemium":
        return 0, limit, False
    used = count_freemium_bids(session, org.id)
    return used, limit, used >= limit
