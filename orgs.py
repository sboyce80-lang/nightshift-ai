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

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models import Organization, OrganizationMembership, User


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
