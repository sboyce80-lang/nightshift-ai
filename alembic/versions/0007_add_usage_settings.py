"""add usage_settings JSON to organizations

Revision ID: 0007_usage_settings
Revises: 0006_denial_tracking
Create Date: 2026-04-29

Adds organizations.usage_settings — per-org JSON blob holding the inputs
the Usage tab uses to compute estimator-time savings:
  {"hourly_wage": <float>, "hours_per_estimate": <float>}
NULL means "use industry-average defaults" (rendered in the UI as a hint).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007_usage_settings"
down_revision: Union[str, Sequence[str], None] = "0006_denial_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("usage_settings", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "usage_settings")
