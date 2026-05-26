"""rename owner value 'elliot' -> 'elliott'

Revision ID: 0018_owner_elliott
Revises: 0017_account_contact_owner
Create Date: 2026-05-21

Corrects the spelling of the founder's name in the owner enum used by
both crm_opportunities.owner and crm_accounts.account_owner. Drops and
re-creates the two CHECK constraints with the corrected value, and
migrates any existing rows. (At migration time no rows held 'elliot',
but the UPDATEs make this safe regardless.)
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0018_owner_elliott"
down_revision: Union[str, Sequence[str], None] = "0017_account_contact_owner"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # crm_opportunities.owner
    op.drop_constraint(
        "ck_crm_opportunities_owner", "crm_opportunities", type_="check",
    )
    op.execute(
        "UPDATE crm_opportunities SET owner = 'elliott' WHERE owner = 'elliot'"
    )
    op.create_check_constraint(
        "ck_crm_opportunities_owner",
        "crm_opportunities",
        "owner IN ('brian','matt','steve','elliott')",
    )

    # crm_accounts.account_owner
    op.drop_constraint(
        "ck_crm_accounts_account_owner", "crm_accounts", type_="check",
    )
    op.execute(
        "UPDATE crm_accounts SET account_owner = 'elliott' "
        "WHERE account_owner = 'elliot'"
    )
    op.create_check_constraint(
        "ck_crm_accounts_account_owner",
        "crm_accounts",
        "account_owner IS NULL OR account_owner IN "
        "('brian','matt','steve','elliott')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_crm_accounts_account_owner", "crm_accounts", type_="check",
    )
    op.execute(
        "UPDATE crm_accounts SET account_owner = 'elliot' "
        "WHERE account_owner = 'elliott'"
    )
    op.create_check_constraint(
        "ck_crm_accounts_account_owner",
        "crm_accounts",
        "account_owner IS NULL OR account_owner IN "
        "('brian','matt','steve','elliot')",
    )

    op.drop_constraint(
        "ck_crm_opportunities_owner", "crm_opportunities", type_="check",
    )
    op.execute(
        "UPDATE crm_opportunities SET owner = 'elliot' WHERE owner = 'elliott'"
    )
    op.create_check_constraint(
        "ck_crm_opportunities_owner",
        "crm_opportunities",
        "owner IN ('brian','matt','steve','elliot')",
    )
