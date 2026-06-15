"""add submissions heartbeat_at/progress/queue_name/job_timeout

Revision ID: 0022_heartbeat_routing
Revises: 0021_emailed_at
Create Date: 2026-06-12

Phase 1(d) of the 2026-06 reliability plan:

- heartbeat_at: touched every ~60s by the worker's heartbeat thread
  while a job is processing. The stuck-job watchdog reaps on STALE
  HEARTBEAT + RQ cross-check, never wall-clock age alone (the old
  updated_at sweep would have killed healthy 90-minute jobs at minute
  31 and then emailed the customer "please resubmit").
- progress: live engine progress ({step, total_steps, label, detail,
  pct, updated}) so the job UI shows real progress instead of a
  constant 55% bar.
- queue_name / job_timeout: the routing decision persisted at first
  enqueue, so every re-enqueue path (requeue scripts, prioritize,
  resubmit) reuses the original queue + timeout instead of silently
  shrinking a 4h DD-scale job to a 2h legacy default.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0022_heartbeat_routing"
down_revision: Union[str, Sequence[str], None] = "0021_emailed_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "submissions",
        sa.Column("progress", sa.JSON(), nullable=True),
    )
    op.add_column(
        "submissions",
        sa.Column("queue_name", sa.String(64), nullable=True),
    )
    op.add_column(
        "submissions",
        sa.Column("job_timeout", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("submissions", "job_timeout")
    op.drop_column("submissions", "queue_name")
    op.drop_column("submissions", "progress")
    op.drop_column("submissions", "heartbeat_at")
