"""add pricing_overrides JSON column to users

Revision ID: 0002_pricing_overrides
Revises: 0001_initial
Create Date: 2026-04-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002_pricing_overrides"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("pricing_overrides", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "pricing_overrides")
