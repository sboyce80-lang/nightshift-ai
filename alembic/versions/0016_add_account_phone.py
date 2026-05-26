"""add phone column to crm_accounts

Revision ID: 0016_account_phone
Revises: 0015_account_website
Create Date: 2026-05-21

Account-level main phone number, distinct from per-contact phone numbers
on crm_contacts. Surfaced in the Company panel as a click-to-dial link.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0016_account_phone"
down_revision: Union[str, Sequence[str], None] = "0015_account_website"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "crm_accounts",
        sa.Column("phone", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("crm_accounts", "phone")
