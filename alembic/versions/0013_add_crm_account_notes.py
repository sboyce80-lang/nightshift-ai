"""add crm_account_notes table for activity timeline

Revision ID: 0013_crm_account_notes
Revises: 0012_opp_status_files
Create Date: 2026-05-20

Simple append-only timeline per CRM account. One row per posted note.
Captures who wrote it (Clerk user id + email at time of write so we don't
have to live-query Clerk for display), when, and the body.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0013_crm_account_notes"
down_revision: Union[str, Sequence[str], None] = "0012_opp_status_files"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "crm_account_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.String(length=255), nullable=True),
        sa.Column("author_email", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),

        sa.ForeignKeyConstraint(
            ["account_id"], ["crm_accounts.id"],
            ondelete="CASCADE", name="fk_crm_account_notes_account_id",
        ),
    )
    op.create_index(
        "ix_crm_account_notes_account_created",
        "crm_account_notes", ["account_id", "created_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_crm_account_notes_account_created", table_name="crm_account_notes",
    )
    op.drop_table("crm_account_notes")
