"""add opportunity status + crm_opportunity_files table

Revision ID: 0012_opp_status_files
Revises: 0011_crm_tables
Create Date: 2026-05-19

Two CRM extensions:

    crm_opportunities.status      pipeline stage (discovery → closed). CHECK
                                  enforces the five allowed values; default
                                  'discovery' so existing rows backfill cleanly.

    crm_opportunity_files         attached agreements (NDAs, signed contracts,
                                  and an 'other' escape hatch). storage_path
                                  is opaque to the DB — the storage backend
                                  (local fs today, R2 later) decides how to
                                  resolve it. Cascade delete on opportunity.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012_opp_status_files"
down_revision: Union[str, Sequence[str], None] = "0011_crm_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "crm_opportunities",
        sa.Column(
            "status", sa.String(length=32),
            nullable=False, server_default="discovery",
        ),
    )
    op.create_check_constraint(
        "ck_crm_opportunities_status",
        "crm_opportunities",
        "status IN ('discovery','demo','beta','closed_won','closed_lost')",
    )
    op.create_index(
        "ix_crm_opportunities_status", "crm_opportunities", ["status"], unique=False,
    )

    op.create_table(
        "crm_opportunity_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False, unique=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_type", sa.String(length=127), nullable=True),
        sa.Column("uploaded_by_user_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),

        sa.ForeignKeyConstraint(
            ["opportunity_id"], ["crm_opportunities.id"],
            ondelete="CASCADE", name="fk_crm_opportunity_files_opportunity_id",
        ),
        sa.CheckConstraint(
            "kind IN ('nda','agreement','other')",
            name="ck_crm_opportunity_files_kind",
        ),
    )
    op.create_index(
        "ix_crm_opportunity_files_opportunity_id",
        "crm_opportunity_files", ["opportunity_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_crm_opportunity_files_opportunity_id",
        table_name="crm_opportunity_files",
    )
    op.drop_table("crm_opportunity_files")

    op.drop_index("ix_crm_opportunities_status", table_name="crm_opportunities")
    op.drop_constraint(
        "ck_crm_opportunities_status", "crm_opportunities", type_="check",
    )
    op.drop_column("crm_opportunities", "status")
