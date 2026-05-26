"""add contact_status and account_owner to crm_accounts

Revision ID: 0017_account_contact_owner
Revises: 0016_account_phone
Create Date: 2026-05-21

    contact_status   outreach state — new / contacted / do_not_contact.
                     NOT NULL, defaults 'new' so the existing 69 rows
                     backfill cleanly.
    account_owner    which founder owns the account. Reuses the same
                     value set as crm_opportunities.owner so a person
                     has one canonical spelling across the app. Nullable
                     — existing accounts are unassigned until set.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0017_account_contact_owner"
down_revision: Union[str, Sequence[str], None] = "0016_account_phone"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "crm_accounts",
        sa.Column("contact_status", sa.String(length=32),
                  nullable=False, server_default="new"),
    )
    op.create_check_constraint(
        "ck_crm_accounts_contact_status",
        "crm_accounts",
        "contact_status IN ('new','contacted','do_not_contact')",
    )
    op.create_index(
        "ix_crm_accounts_contact_status",
        "crm_accounts", ["contact_status"], unique=False,
    )

    op.add_column(
        "crm_accounts",
        sa.Column("account_owner", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "ck_crm_accounts_account_owner",
        "crm_accounts",
        "account_owner IS NULL OR account_owner IN "
        "('brian','matt','steve','elliot')",
    )
    op.create_index(
        "ix_crm_accounts_account_owner",
        "crm_accounts", ["account_owner"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_crm_accounts_account_owner", table_name="crm_accounts")
    op.drop_constraint(
        "ck_crm_accounts_account_owner", "crm_accounts", type_="check",
    )
    op.drop_column("crm_accounts", "account_owner")

    op.drop_index("ix_crm_accounts_contact_status", table_name="crm_accounts")
    op.drop_constraint(
        "ck_crm_accounts_contact_status", "crm_accounts", type_="check",
    )
    op.drop_column("crm_accounts", "contact_status")
