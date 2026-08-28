"""PLG self-serve freemium: org plan tier + onboarding fields + email claims

Revision ID: 0023_plg_freemium
Revises: 0022_heartbeat_routing
Create Date: 2026-08-28

Adds the schema for the flag-gated PLG self-serve motion
(PLG_SELF_SERVE_ENABLED, default OFF — running this migration changes no
behavior on its own):

- organizations.plan: 'freemium' | 'beta' | 'paid'. Freemium orgs are
  capped at FREEMIUM_BID_LIMIT lifetime bids. Every PRE-EXISTING org is
  backfilled to 'beta' so the quota gate can never fire on a current
  customer; only orgs auto-approved by the PLG onboarding flow are ever
  created as 'freemium'.
- organizations.company_size: self-reported size from the PLG onboarding
  form.
- organizations.freemium_exhausted_emailed_at /
  freemium_hot_lead_emailed_at: idempotency claims for the freemium
  lifecycle emails (UPDATE ... WHERE ... IS NULL before send, same
  pattern as submissions.emailed_at).
- users.title: job title from the PLG onboarding form.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0023_plg_freemium"
down_revision: Union[str, Sequence[str], None] = "0022_heartbeat_routing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("plan", sa.String(16), nullable=False,
                  server_default="freemium"),
    )
    # Grandfather every org that exists before this migration: they were all
    # hand-approved beta customers (or manually provisioned), so the freemium
    # quota must never apply to them.
    op.execute("UPDATE organizations SET plan = 'beta'")

    op.add_column(
        "organizations",
        sa.Column("company_size", sa.String(32), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("freemium_exhausted_emailed_at",
                  sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("freemium_hot_lead_emailed_at",
                  sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("title", sa.String(128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "title")
    op.drop_column("organizations", "freemium_hot_lead_emailed_at")
    op.drop_column("organizations", "freemium_exhausted_emailed_at")
    op.drop_column("organizations", "company_size")
    op.drop_column("organizations", "plan")
