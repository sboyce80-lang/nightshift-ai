"""Product → CRM bridge for the PLG self-serve motion.

When a signup is auto-approved onto freemium, the only artifact used to be
an internal notification email — if nobody acted on it, the customer never
existed in the sales pipeline (Profeta Painting shipped a real bid without
a CRM row). This module gives every auto-approved org a CRM presence at
the moment of approval:

    crm_accounts        one account, linked via knightshift_org_id (which
                        also lights up the CRM's live product-usage JOIN)
    crm_contacts        the signup user as primary contact
    crm_account_notes   a timeline note recording the self-serve origin,
                        plan, and company size

Idempotent by org: an account already linked to the org is left completely
untouched (a founder may have enriched it by hand). If an UNLINKED account
with the same name exists — founder created it manually before the org
signed up — it is linked rather than duplicated, and only fields the CRM
doesn't already have get filled in.

crm_accounts.plan stays NULL: its CHECK (and the CRM UI's PLAN_OPTIONS)
only allow growth/scale/enterprise. Freemium status lives in the timeline
note and on the linked org itself.
"""
from __future__ import annotations

import logging

from sqlalchemy import func

import config
from models import CrmAccount, CrmAccountNote, CrmContact, Organization, User

logger = logging.getLogger(__name__)

# crm_accounts.account_owner CHECK values (0018 spelling: elliott).
_VALID_OWNERS = ("brian", "matt", "steve", "elliott")


def _default_owner() -> str | None:
    owner = config.PLG_CRM_DEFAULT_OWNER
    if owner in _VALID_OWNERS:
        return owner
    logger.warning(
        "PLG_CRM_DEFAULT_OWNER=%r is not a valid CRM owner %s; "
        "leaving account unassigned", owner, _VALID_OWNERS,
    )
    return None


def ensure_crm_account_for_org(session, org: Organization,
                               user: User) -> tuple[CrmAccount, bool]:
    """Ensure a CRM account exists for *org*; return (account, created).

    ``created`` is True only when a brand-new account row was inserted.
    Linking a pre-existing manual account or finding an already-linked one
    returns False. Caller owns the transaction (and must treat failures as
    non-fatal — CRM bookkeeping must never block a signup).
    """
    account = (
        session.query(CrmAccount)
        .filter(CrmAccount.knightshift_org_id == org.id)
        .first()
    )
    if account is not None:
        return account, False

    # Founder may have already created this company by hand during outreach.
    # Link that row instead of duplicating it; fill only fields it lacks.
    account = (
        session.query(CrmAccount)
        .filter(CrmAccount.knightshift_org_id.is_(None))
        .filter(func.lower(CrmAccount.name) == (org.name or "").lower())
        .first()
    )
    if account is not None:
        account.knightshift_org_id = org.id
        if not account.phone and org.phone:
            account.phone = org.phone
        if account.account_owner is None:
            account.account_owner = _default_owner()
        _add_signup_note(session, account, org, user, linked_existing=True)
        _ensure_contact(session, account, org, user)
        logger.info(
            "PLG CRM: linked existing account %s to org %s (%r)",
            account.id, org.id, org.name,
        )
        return account, False

    account = CrmAccount(
        name=org.name or (org.email_domain or "Unknown company"),
        knightshift_org_id=org.id,
        status="prospect",
        contact_status="new",
        account_owner=_default_owner(),
        phone=org.phone or None,
        website=(f"https://{org.email_domain}" if org.email_domain else None),
    )
    session.add(account)
    session.flush()  # need account.id for contact + note
    _ensure_contact(session, account, org, user)
    _add_signup_note(session, account, org, user, linked_existing=False)
    logger.info(
        "PLG CRM: created account %s for org %s (%r)",
        account.id, org.id, org.name,
    )
    return account, True


def _ensure_contact(session, account: CrmAccount, org: Organization,
                    user: User) -> None:
    existing = (
        session.query(CrmContact)
        .filter(CrmContact.account_id == account.id)
        .filter(func.lower(CrmContact.email) == (user.email or "").lower())
        .first()
    )
    if existing is not None:
        return
    has_primary = (
        session.query(CrmContact)
        .filter(CrmContact.account_id == account.id)
        .filter(CrmContact.is_primary.is_(True))
        .first()
    ) is not None
    session.add(CrmContact(
        account_id=account.id,
        name=user.name or user.email or "Unknown",
        email=user.email,
        phone=org.phone or None,
        title=user.title or None,
        lead_source="plg_signup",
        is_primary=not has_primary,
    ))
    # The app session runs autoflush=False; flush so a second call in the
    # same transaction sees this row instead of duplicating it.
    session.flush()


def _add_signup_note(session, account: CrmAccount, org: Organization,
                     user: User, linked_existing: bool) -> None:
    verb = ("Linked to product org" if linked_existing
            else "Auto-created from self-serve signup")
    parts = [
        f"[auto] {verb}: {user.name or user.email} signed up and was "
        f"approved onto the freemium plan "
        f"({config.FREEMIUM_BID_LIMIT} lifetime free bids).",
    ]
    if org.company_size:
        parts.append(f"Company size: {org.company_size} employees.")
    if org.email_domain:
        parts.append(f"Domain: {org.email_domain}.")
    session.add(CrmAccountNote(
        account_id=account.id,
        body=" ".join(parts),
        author_user_id=None,
        author_email="plg-autocreate@knightshiftai.com",
    ))
    session.flush()
