"""add submissions.emailed_at email-idempotency claim

Revision ID: 0021_emailed_at
Revises: 0020_message_settings
Create Date: 2026-06-11

Adds submissions.emailed_at — claimed via
    UPDATE submissions SET emailed_at = now()
    WHERE id = :id AND emailed_at IS NULL
before the worker sends the customer estimate / manual-review email.

Background: the estimate email used to be sent BEFORE the row was marked
completed, so a warm-shutdown requeue (deploy) or retry re-ran the whole
job and sent a second estimate — and because multi-pass extraction is
non-deterministic, the second email could carry different numbers. The
claim makes the send exactly-once across retries; a failed send releases
the claim (emailed_at -> NULL) so a manual resend can still go out.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0021_emailed_at"
down_revision: Union[str, Sequence[str], None] = "0020_message_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("submissions", "emailed_at")
