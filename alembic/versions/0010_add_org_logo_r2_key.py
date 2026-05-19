"""add logo_r2_key column for org-uploaded logos

Revision ID: 0010_org_logo_r2_key
Revises: 0009_org_branding
Create Date: 2026-05-19

When the user uploads a logo file (drag-drop on /account/organization),
the bytes go to R2 under orgs/<org_id>/logo.<ext> and the key is stored
here. The Estimate PDF generator prefers logo_r2_key over logo_url when
both are set (R2-hosted bytes are inlined as a data URI so the PDF is
self-contained without depending on a presigned URL that might expire).

    logo_r2_key  varchar(1024)  nullable — R2 object key for the upload
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010_org_logo_r2_key"
down_revision: Union[str, Sequence[str], None] = "0009_org_branding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("logo_r2_key", sa.String(1024), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "logo_r2_key")
