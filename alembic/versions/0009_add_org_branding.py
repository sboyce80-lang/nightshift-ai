"""add org branding fields for estimate PDFs

Revision ID: 0009_org_branding
Revises: 0008_submission_versioning
Create Date: 2026-05-19

Adds the fields surfaced on the formal Estimate PDF (the third deliverable
alongside the full Job PDF and JSON). Logo is sourced from Clerk's user
image_url on sign-in when null; the rest live in the Org Settings page:

    logo_url        varchar(1024)  Clerk image_url or owner-supplied override
    street_address  varchar(255)
    city            varchar(128)
    state           varchar(64)
    postal_code     varchar(32)
    phone           varchar(64)
    contact_email   varchar(320)   address printed on estimates (not auth email)
    website         varchar(255)
    tax_id          varchar(64)    "Business / Tax #" on the estimate header
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009_org_branding"
down_revision: Union[str, Sequence[str], None] = "0008_submission_versioning"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("logo_url", sa.String(1024), nullable=True))
    op.add_column("organizations", sa.Column("street_address", sa.String(255), nullable=True))
    op.add_column("organizations", sa.Column("city", sa.String(128), nullable=True))
    op.add_column("organizations", sa.Column("state", sa.String(64), nullable=True))
    op.add_column("organizations", sa.Column("postal_code", sa.String(32), nullable=True))
    op.add_column("organizations", sa.Column("phone", sa.String(64), nullable=True))
    op.add_column("organizations", sa.Column("contact_email", sa.String(320), nullable=True))
    op.add_column("organizations", sa.Column("website", sa.String(255), nullable=True))
    op.add_column("organizations", sa.Column("tax_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "tax_id")
    op.drop_column("organizations", "website")
    op.drop_column("organizations", "contact_email")
    op.drop_column("organizations", "phone")
    op.drop_column("organizations", "postal_code")
    op.drop_column("organizations", "state")
    op.drop_column("organizations", "city")
    op.drop_column("organizations", "street_address")
    op.drop_column("organizations", "logo_url")
