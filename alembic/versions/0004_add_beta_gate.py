"""add beta gate fields to organizations

Revision ID: 0004_beta_gate
Revises: 0003_organizations
Create Date: 2026-04-28

Adds two columns to organizations to support a beta-access gate:
  - is_beta_approved (bool, default false)
  - daily_submission_cap (int, nullable; NULL means env default)

All existing orgs are grandfathered to is_beta_approved=true so the
current users (Rider Painting, etc.) are not locked out.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004_beta_gate"
down_revision: Union[str, Sequence[str], None] = "0003_organizations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "is_beta_approved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "organizations",
        sa.Column("daily_submission_cap", sa.Integer(), nullable=True),
    )

    # Grandfather every existing org. New orgs created after this migration
    # will land with is_beta_approved=false (the column default).
    op.execute("UPDATE organizations SET is_beta_approved = true")


def downgrade() -> None:
    op.drop_column("organizations", "daily_submission_cap")
    op.drop_column("organizations", "is_beta_approved")
