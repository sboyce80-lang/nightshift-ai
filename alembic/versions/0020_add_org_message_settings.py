"""add message_settings JSON to organizations

Revision ID: 0020_message_settings
Revises: 0019_voicemail_status
Create Date: 2026-05-26

Adds organizations.message_settings — per-org JSON blob holding the defaults
the Completed-tab "Send Estimate" modal pre-fills with:
  {
    "subject_template": "Estimate for {business_name}",
    "body_template":    "Hello, ...{subtotal}...",
    "cc":  ["pm@example.com", ...],
    "bcc": ["billing@example.com", ...]
  }
NULL means "use the system default template" (rendered in the UI as a hint).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0020_message_settings"
down_revision: Union[str, Sequence[str], None] = "0019_voicemail_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("message_settings", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "message_settings")
