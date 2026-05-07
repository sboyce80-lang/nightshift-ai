"""add submission versioning + merge metadata

Revision ID: 0008_submission_versioning
Revises: 0007_usage_settings
Create Date: 2026-05-07

Adds versioning to `submissions` so re-runs (revised plans, RFI responses,
amendments) can attach to a parent submission instead of starting cold:

    parent_submission_id  varchar(36)  FK→submissions.id, nullable
    version               smallint     default 1, nullable
    merge_notes           text         nullable  (user's "what changed" intent)
    merge_scope_tags      JSON         nullable  (["Basement","DoorSchedule"])

The merge worker (`jobs.merge_submission`) creates a v2+ row pointing at the
v1 parent, re-extracts only the new files, merges into the parent JSON, and
writes its own results. Original v1 results stay intact on R2 for audit and
rollback.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008_submission_versioning"
down_revision: Union[str, Sequence[str], None] = "0007_usage_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column("parent_submission_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "submissions",
        sa.Column("version", sa.SmallInteger(), nullable=False, server_default="1"),
    )
    op.add_column(
        "submissions",
        sa.Column("merge_notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "submissions",
        sa.Column("merge_scope_tags", sa.JSON(), nullable=True),
    )
    op.create_foreign_key(
        "fk_submissions_parent_submission_id",
        "submissions",
        "submissions",
        ["parent_submission_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_submissions_parent_version",
        "submissions",
        ["parent_submission_id", "version"],
    )


def downgrade() -> None:
    op.drop_index("ix_submissions_parent_version", table_name="submissions")
    op.drop_constraint(
        "fk_submissions_parent_submission_id", "submissions", type_="foreignkey"
    )
    op.drop_column("submissions", "merge_scope_tags")
    op.drop_column("submissions", "merge_notes")
    op.drop_column("submissions", "version")
    op.drop_column("submissions", "parent_submission_id")
