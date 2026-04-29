"""add denied_at to organizations

Revision ID: 0006_denial_tracking
Revises: 0005_approval_request
Create Date: 2026-04-29

Adds organizations.denied_at — set when an admin denies an org's access
request from /admin/orgs. Denied orgs are hidden from the pending list
and their owners get a denied-access screen instead of the waitlist /
onboarding form.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006_denial_tracking"
down_revision: Union[str, Sequence[str], None] = "0005_approval_request"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "denied_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("organizations", "denied_at")
