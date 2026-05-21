"""add contract_end_date and per-account ROI overrides to crm_accounts

Revision ID: 0014_account_renewal_roi
Revises: 0013_crm_account_notes
Create Date: 2026-05-20

Three additive columns on crm_accounts:

    contract_end_date     date when the current contract expires; powers
                          the "upcoming renewals" view on the dashboard.
    hourly_wage           per-account override of the wage assumption used
                          in the ROI formula. NULL falls through to the
                          linked Knightshift org's usage_settings, then to
                          the global default ($36).
    hours_per_estimate    per-account override of hours-per-bid assumption.
                          NULL falls through (same chain as hourly_wage).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0014_account_renewal_roi"
down_revision: Union[str, Sequence[str], None] = "0013_crm_account_notes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "crm_accounts",
        sa.Column("contract_end_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "crm_accounts",
        sa.Column("hourly_wage", sa.Numeric(8, 2), nullable=True),
    )
    op.add_column(
        "crm_accounts",
        sa.Column("hours_per_estimate", sa.Numeric(6, 2), nullable=True),
    )
    op.create_index(
        "ix_crm_accounts_contract_end_date",
        "crm_accounts", ["contract_end_date"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_crm_accounts_contract_end_date", table_name="crm_accounts",
    )
    op.drop_column("crm_accounts", "hours_per_estimate")
    op.drop_column("crm_accounts", "hourly_wage")
    op.drop_column("crm_accounts", "contract_end_date")
