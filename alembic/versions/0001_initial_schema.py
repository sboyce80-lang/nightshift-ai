"""initial schema — users, submissions, files

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("clerk_user_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("clerk_user_id", name="uq_users_clerk_user_id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=False)
    op.create_index("ix_users_clerk_user_id", "users", ["clerk_user_id"], unique=False)

    op.create_table(
        "submissions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("business_name", sa.String(length=255), nullable=True),
        sa.Column("scope_notes", sa.String(length=4000), nullable=True),
        sa.Column("deadline", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("error", sa.String(length=2000), nullable=True),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_submissions_user_id",
        ),
    )
    op.create_index("ix_submissions_user_id", "submissions", ["user_id"], unique=False)
    op.create_index(
        "ix_submissions_user_submitted",
        "submissions",
        ["user_id", "submitted_at"],
        unique=False,
    )

    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("submission_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("r2_key", sa.String(length=1024), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_type", sa.String(length=127), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["submissions.id"], ondelete="CASCADE",
            name="fk_files_submission_id",
        ),
        sa.UniqueConstraint("r2_key", name="uq_files_r2_key"),
        sa.UniqueConstraint(
            "submission_id", "kind", "filename",
            name="uq_files_submission_kind_filename",
        ),
    )
    op.create_index("ix_files_submission_id", "files", ["submission_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_files_submission_id", table_name="files")
    op.drop_table("files")
    op.drop_index("ix_submissions_user_submitted", table_name="submissions")
    op.drop_index("ix_submissions_user_id", table_name="submissions")
    op.drop_table("submissions")
    op.drop_index("ix_users_clerk_user_id", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
