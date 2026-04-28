"""add approval_requested_at to organizations

Revision ID: 0005_approval_request
Revises: 0004_beta_gate
Create Date: 2026-04-28

Adds organizations.approval_requested_at — set when a user completes the
sign-up onboarding form. Distinguishes orgs that have submitted an access
request (admin notified, user shown waitlist) from orgs whose user has
authenticated but not yet provided their company info.

Existing orgs are backfilled with NOW() so they're treated as already-applied
(grandfathered alongside is_beta_approved=true from migration 0004).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0005_approval_request"
down_revision: Union[str, Sequence[str], None] = "0004_beta_gate"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "approval_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Backfill existing orgs so they don't get pushed into onboarding on
    # next sign-in. New orgs land with NULL (column default) and must go
    # through /onboarding before the admin email fires.
    op.execute("UPDATE organizations SET approval_requested_at = NOW()")


def downgrade() -> None:
    op.drop_column("organizations", "approval_requested_at")
