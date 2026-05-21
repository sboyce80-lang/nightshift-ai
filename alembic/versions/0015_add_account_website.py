"""add website column to crm_accounts

Revision ID: 0015_account_website
Revises: 0014_account_renewal_roi
Create Date: 2026-05-21

Promotes the company URL from a free-text mention inside notes to a
first-class column so it can be displayed as a link and edited on the
account form.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0015_account_website"
down_revision: Union[str, Sequence[str], None] = "0014_account_renewal_roi"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "crm_accounts",
        sa.Column("website", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("crm_accounts", "website")
