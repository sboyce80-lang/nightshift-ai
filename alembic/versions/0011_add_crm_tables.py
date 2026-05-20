"""add CRM tables (accounts, contacts, opportunities)

Revision ID: 0011_crm_tables
Revises: 0010_org_logo_r2_key
Create Date: 2026-05-19

Internal CRM lives in the same Postgres as the product. Three tables:

    crm_accounts        — one row per customer/prospect company. Optional FK
                          to organizations(id) (knightshift_org_id) is the
                          join key for live product-usage metrics (bids sent,
                          total bid value, etc.). Stored fields are only the
                          ones the CRM owns — usage metrics are *derived* via
                          JOIN against submissions and never duplicated here.
    crm_contacts        — many-to-one against crm_accounts. People we email,
                          call, send NDAs to. is_primary flags the default
                          point-of-contact per account.
    crm_opportunities   — sales-pipeline rows. owner is one of the four
                          founders (brian/matt/steve/elliot); CHECK enforced.

Enum-like fields (industry, status, plan, owner) use varchar + CHECK rather
than Postgres ENUM so we can add values in plain Alembic ops without the
ALTER TYPE dance.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011_crm_tables"
down_revision: Union[str, Sequence[str], None] = "0010_org_logo_r2_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "crm_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),

        # Optional link to the Knightshift product org. When set, the CRM
        # account-detail view JOINs against submissions for live metrics.
        sa.Column("knightshift_org_id", sa.Integer(), nullable=True),

        # Address — single set of fields, kept flat for easy form binding.
        sa.Column("address_line1", sa.String(length=255), nullable=True),
        sa.Column("address_line2", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=64), nullable=True),
        sa.Column("postal_code", sa.String(length=32), nullable=True),
        sa.Column("country", sa.String(length=64), nullable=True),

        sa.Column("industry", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="prospect"),
        sa.Column("plan", sa.String(length=32), nullable=True),

        sa.Column("org_size", sa.Integer(), nullable=True),
        sa.Column("annual_revenue", sa.Numeric(14, 2), nullable=True),
        sa.Column("estimated_roi", sa.Numeric(8, 2), nullable=True),

        sa.Column("notes", sa.Text(), nullable=True),

        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),

        sa.ForeignKeyConstraint(
            ["knightshift_org_id"], ["organizations.id"],
            ondelete="SET NULL", name="fk_crm_accounts_knightshift_org_id",
        ),
        sa.CheckConstraint(
            "industry IS NULL OR industry IN ('commercial','residential','mixed')",
            name="ck_crm_accounts_industry",
        ),
        sa.CheckConstraint(
            "status IN ('partner','prospect','beta','client','churned')",
            name="ck_crm_accounts_status",
        ),
        sa.CheckConstraint(
            "plan IS NULL OR plan IN ('growth','scale','enterprise')",
            name="ck_crm_accounts_plan",
        ),
    )
    op.create_index(
        "ix_crm_accounts_knightshift_org_id", "crm_accounts",
        ["knightshift_org_id"], unique=False,
    )
    op.create_index("ix_crm_accounts_status", "crm_accounts", ["status"], unique=False)

    op.create_table(
        "crm_contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),

        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),

        sa.Column("address_line1", sa.String(length=255), nullable=True),
        sa.Column("address_line2", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=64), nullable=True),
        sa.Column("postal_code", sa.String(length=32), nullable=True),
        sa.Column("country", sa.String(length=64), nullable=True),

        sa.Column("lead_source", sa.String(length=64), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),

        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),

        sa.ForeignKeyConstraint(
            ["account_id"], ["crm_accounts.id"],
            ondelete="CASCADE", name="fk_crm_contacts_account_id",
        ),
    )
    op.create_index(
        "ix_crm_contacts_account_id", "crm_contacts", ["account_id"], unique=False,
    )
    op.create_index("ix_crm_contacts_email", "crm_contacts", ["email"], unique=False)

    op.create_table(
        "crm_opportunities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=True),

        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("products", sa.Text(), nullable=True),
        sa.Column("acv", sa.Numeric(12, 2), nullable=True),
        sa.Column("estimated_close_date", sa.Date(), nullable=True),

        sa.Column("owner", sa.String(length=32), nullable=False),
        sa.Column("payment_terms", sa.Text(), nullable=True),
        sa.Column("contract_terms", sa.Text(), nullable=True),

        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),

        sa.ForeignKeyConstraint(
            ["account_id"], ["crm_accounts.id"],
            ondelete="CASCADE", name="fk_crm_opportunities_account_id",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"], ["crm_contacts.id"],
            ondelete="SET NULL", name="fk_crm_opportunities_contact_id",
        ),
        sa.CheckConstraint(
            "owner IN ('brian','matt','steve','elliot')",
            name="ck_crm_opportunities_owner",
        ),
    )
    op.create_index(
        "ix_crm_opportunities_account_id", "crm_opportunities",
        ["account_id"], unique=False,
    )
    op.create_index(
        "ix_crm_opportunities_owner_close",
        "crm_opportunities", ["owner", "estimated_close_date"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_crm_opportunities_owner_close", table_name="crm_opportunities")
    op.drop_index("ix_crm_opportunities_account_id", table_name="crm_opportunities")
    op.drop_table("crm_opportunities")

    op.drop_index("ix_crm_contacts_email", table_name="crm_contacts")
    op.drop_index("ix_crm_contacts_account_id", table_name="crm_contacts")
    op.drop_table("crm_contacts")

    op.drop_index("ix_crm_accounts_status", table_name="crm_accounts")
    op.drop_index("ix_crm_accounts_knightshift_org_id", table_name="crm_accounts")
    op.drop_table("crm_accounts")
