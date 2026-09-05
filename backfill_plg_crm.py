#!/usr/bin/env python3
"""One-off backfill: CRM accounts for freemium orgs that predate the hook.

The PLG_CRM_AUTOCREATE hook only fires at signup time, so orgs that signed
up before it shipped (Profeta Painting, org 12, is the one that exposed
the gap) never get a CRM row. This walks every plan='freemium' org and
runs the same ensure_crm_account_for_org() the live hook uses — same
idempotency rules, so re-running it is safe and orgs that already have a
linked account are skipped.

Dry-run by default; pass --apply to write.

Run (against prod, from a machine with prod DATABASE_URL in .env):
    python3 backfill_plg_crm.py            # show what would happen
    python3 backfill_plg_crm.py --apply    # do it
"""
import sys

from db import session_scope
from models import CrmAccount, Organization, OrganizationMembership, User
from crm_sync import ensure_crm_account_for_org


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    with session_scope() as session:
        orgs = (
            session.query(Organization)
            .filter(Organization.plan == "freemium")
            .filter(Organization.is_beta_approved.is_(True))
            .order_by(Organization.id)
            .all()
        )
        print(f"{len(orgs)} approved freemium org(s)")
        for org in orgs:
            owner_membership = (
                session.query(OrganizationMembership)
                .filter_by(organization_id=org.id, role="owner")
                .order_by(OrganizationMembership.id)
                .first()
            )
            user = (session.get(User, owner_membership.user_id)
                    if owner_membership else None)
            if user is None:
                print(f"  org {org.id} ({org.name!r}): SKIP — no owner user")
                continue
            existing = (
                session.query(CrmAccount)
                .filter(CrmAccount.knightshift_org_id == org.id)
                .first()
            )
            if existing is not None:
                print(f"  org {org.id} ({org.name!r}): already linked to "
                      f"CRM account {existing.id}")
                continue
            if not apply:
                print(f"  org {org.id} ({org.name!r}): WOULD create/link "
                      f"CRM account (owner contact: {user.email})")
                continue
            account, created = ensure_crm_account_for_org(session, org, user)
            verb = "created" if created else "linked"
            print(f"  org {org.id} ({org.name!r}): {verb} CRM account "
                  f"{account.id}")
        if not apply:
            session.rollback()
            print("dry-run — nothing written; pass --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
