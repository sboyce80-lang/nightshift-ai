"""add organizations + memberships, migrate pricing_overrides off users

Revision ID: 0003_organizations
Revises: 0002_pricing_overrides
Create Date: 2026-04-28

This migration introduces the multi-tenant organization layer.

Schema changes:
  - new table:  organizations
  - new table:  organization_memberships
  - users:      + current_organization_id (FK), - pricing_overrides
  - submissions: + org_id (FK, NOT NULL after backfill)

Backfill strategy:
  - Each existing user is auto-provisioned an org based on their email:
    * free-email domains (gmail, yahoo, etc.) -> personal org per user
    * corporate domains -> one shared org per domain; first user is owner,
      subsequent same-domain users join as members
  - The user's pricing_overrides JSON moves onto the org row. If two users
    share a corporate domain and both have overrides, the most recently
    updated user wins; the loser's overrides are logged and discarded.
  - All migrated orgs are stamped verified_at = NOW() (grandfathered) so
    nobody gets locked out asking them to verify webmaster@ post-hoc.
  - Special case: riderpaintingny.com -> "Rider Painting" (canonical name).
"""

import json
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text


revision: str = "0003_organizations"
down_revision: Union[str, Sequence[str], None] = "0002_pricing_overrides"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Free-email providers that should get a personal org instead of being
# grouped under a "domain" (we'd never want every gmail user in one tenant).
FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "yahoo.co.uk", "outlook.com", "hotmail.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "live.com", "msn.com",
    "protonmail.com", "proton.me", "pm.me",
})

# Domains whose canonical org name we override. Avoids the user landing
# on "Riderpaintingny" as their org name and having to fix it manually.
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


# ---------------------------------------------------------------------------

def upgrade() -> None:
    # ---- 1. Create new tables --------------------------------------------

    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email_domain", sa.String(length=255), nullable=True),
        sa.Column("is_personal", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pricing_overrides", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_organizations_email_domain", "organizations", ["email_domain"], unique=False,
    )

    op.create_table(
        "organization_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE",
            name="fk_membership_organization_id",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_membership_user_id",
        ),
        sa.UniqueConstraint(
            "organization_id", "user_id", name="uq_membership_org_user",
        ),
    )
    op.create_index(
        "ix_membership_organization_id", "organization_memberships",
        ["organization_id"], unique=False,
    )
    op.create_index(
        "ix_membership_user_id", "organization_memberships",
        ["user_id"], unique=False,
    )

    # ---- 2. Add nullable columns -----------------------------------------

    op.add_column(
        "users",
        sa.Column("current_organization_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_current_organization_id",
        "users", "organizations",
        ["current_organization_id"], ["id"], ondelete="SET NULL",
    )

    op.add_column(
        "submissions",
        sa.Column("org_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_submissions_org_id",
        "submissions", "organizations",
        ["org_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index(
        "ix_submissions_org_id", "submissions", ["org_id"], unique=False,
    )

    # ---- 3. Backfill: provision orgs + memberships, move pricing ----------

    bind = op.get_bind()
    now = datetime.now(timezone.utc)

    users = bind.execute(text(
        "SELECT id, email, name, pricing_overrides, updated_at "
        "FROM users ORDER BY updated_at DESC NULLS LAST, id ASC"
    )).fetchall()

    # domain -> org_id; only populated for corporate (non-personal) orgs
    # since personal orgs are 1:1 with the user and never reused.
    orgs_by_domain: dict[str, int] = {}

    for user in users:
        domain = _domain_of(user.email)
        is_personal = (not domain) or (domain in FREE_EMAIL_DOMAINS)

        # Existing per-user JSON to migrate. May be None.
        po = user.pricing_overrides
        po_json = json.dumps(po) if po else None

        if is_personal:
            # One personal org per user.
            display = user.name or user.email or f"user-{user.id}"
            new_org = bind.execute(text("""
                INSERT INTO organizations
                    (name, email_domain, is_personal, verified_at,
                     pricing_overrides, created_at, updated_at)
                VALUES
                    (:name, NULL, TRUE, :now,
                     CAST(:po AS JSON), :now, :now)
                RETURNING id
            """), {"name": display, "now": now, "po": po_json}).scalar_one()
            org_id = new_org
            role = "owner"

        elif domain in orgs_by_domain:
            # Joining an existing corporate org as member.
            org_id = orgs_by_domain[domain]
            role = "member"

            if po_json is not None:
                # Order is updated_at DESC, so the *first* user from each
                # domain (the owner) had the most recent overrides and
                # already populated the org. This member's overrides are
                # discarded with a log line.
                existing_po = bind.execute(text(
                    "SELECT pricing_overrides FROM organizations WHERE id = :id"
                ), {"id": org_id}).scalar_one()
                if existing_po is not None:
                    print(
                        f"  [migration 0003] WARN: discarding pricing_overrides "
                        f"from user id={user.id} email={user.email!r} — "
                        f"org for domain {domain!r} already has overrides "
                        f"from a more-recently-updated user."
                    )
                else:
                    # Owner had no overrides but this member does; promote.
                    bind.execute(text(
                        "UPDATE organizations SET pricing_overrides = CAST(:po AS JSON) "
                        "WHERE id = :id"
                    ), {"po": po_json, "id": org_id})

        else:
            # First user from this corporate domain — create the org.
            org_name = DOMAIN_TO_NAME.get(domain) or _humanize_domain(domain)
            new_org = bind.execute(text("""
                INSERT INTO organizations
                    (name, email_domain, is_personal, verified_at,
                     pricing_overrides, created_at, updated_at)
                VALUES
                    (:name, :domain, FALSE, :now,
                     CAST(:po AS JSON), :now, :now)
                RETURNING id
            """), {
                "name": org_name, "domain": domain, "now": now, "po": po_json,
            }).scalar_one()
            org_id = new_org
            orgs_by_domain[domain] = org_id
            role = "owner"

        # Membership row
        bind.execute(text("""
            INSERT INTO organization_memberships
                (organization_id, user_id, role, created_at)
            VALUES
                (:org_id, :user_id, :role, :now)
        """), {"org_id": org_id, "user_id": user.id, "role": role, "now": now})

        # Set current org context
        bind.execute(text(
            "UPDATE users SET current_organization_id = :org_id WHERE id = :user_id"
        ), {"org_id": org_id, "user_id": user.id})

        # Backfill all of this user's submissions
        bind.execute(text(
            "UPDATE submissions SET org_id = :org_id WHERE user_id = :user_id"
        ), {"org_id": org_id, "user_id": user.id})

    # ---- 4. Verify backfill, then enforce NOT NULL -----------------------

    orphan_count = bind.execute(text(
        "SELECT COUNT(*) FROM submissions WHERE org_id IS NULL"
    )).scalar_one()
    if orphan_count:
        raise RuntimeError(
            f"migration 0003 backfill incomplete: {orphan_count} submissions "
            f"have NULL org_id after backfill — aborting before dropping "
            f"users.pricing_overrides."
        )

    op.alter_column(
        "submissions", "org_id",
        existing_type=sa.Integer(), nullable=False,
    )

    # ---- 5. Drop the old per-user pricing column -------------------------

    op.drop_column("users", "pricing_overrides")


# ---------------------------------------------------------------------------

def downgrade() -> None:
    # Restore users.pricing_overrides and copy each user's current-org JSON
    # back. If multiple users share an org they all get the same JSON —
    # acceptable for downgrade since the source-of-truth was the org anyway.
    op.add_column(
        "users",
        sa.Column("pricing_overrides", sa.JSON(), nullable=True),
    )

    bind = op.get_bind()
    bind.execute(text("""
        UPDATE users u
           SET pricing_overrides = o.pricing_overrides
          FROM organizations o
         WHERE u.current_organization_id = o.id
           AND o.pricing_overrides IS NOT NULL
    """))

    # submissions.org_id off
    op.drop_index("ix_submissions_org_id", table_name="submissions")
    op.drop_constraint("fk_submissions_org_id", "submissions", type_="foreignkey")
    op.drop_column("submissions", "org_id")

    # users.current_organization_id off
    op.drop_constraint(
        "fk_users_current_organization_id", "users", type_="foreignkey",
    )
    op.drop_column("users", "current_organization_id")

    # Drop new tables
    op.drop_index("ix_membership_user_id", table_name="organization_memberships")
    op.drop_index("ix_membership_organization_id", table_name="organization_memberships")
    op.drop_table("organization_memberships")

    op.drop_index("ix_organizations_email_domain", table_name="organizations")
    op.drop_table("organizations")
