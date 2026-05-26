"""add 'voicemail' to the contact_status set

Revision ID: 0019_voicemail_status
Revises: 0018_owner_elliott
Create Date: 2026-05-21

Widens crm_accounts.contact_status to allow 'voicemail' (a call was
placed and a voicemail left) alongside new / contacted / do_not_contact.
Pure CHECK-constraint swap; no data change.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0019_voicemail_status"
down_revision: Union[str, Sequence[str], None] = "0018_owner_elliott"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_crm_accounts_contact_status", "crm_accounts", type_="check",
    )
    op.create_check_constraint(
        "ck_crm_accounts_contact_status",
        "crm_accounts",
        "contact_status IN ('new','contacted','voicemail','do_not_contact')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_crm_accounts_contact_status", "crm_accounts", type_="check",
    )
    op.create_check_constraint(
        "ck_crm_accounts_contact_status",
        "crm_accounts",
        "contact_status IN ('new','contacted','do_not_contact')",
    )
