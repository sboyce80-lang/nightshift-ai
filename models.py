#!/usr/bin/env python3
"""
Knight Shift — SQLAlchemy Models
================================
Three tables:

    users         — one row per submitter, identified by email. Linked to
                    a Clerk user_id once auth is wired up in step 4.
    submissions   — one row per /submit request. The submission_id (UUID)
                    is the same value used as the RQ job_id and the R2
                    key prefix.
    files         — one row per object in R2 attached to a submission
                    (uploads + results). Lets us list a user's history
                    cheaply without scanning R2.
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    String, Integer, BigInteger, Numeric, DateTime, ForeignKey, Index,
    UniqueConstraint, JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    clerk_user_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True)
    pricing_overrides: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    submissions: Mapped[List["Submission"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email}>"


# ---------------------------------------------------------------------------

# Status values used in submissions.status. Kept as plain strings (not an
# enum type) so adding a new state doesn't require an Alembic migration.
SUBMISSION_STATUSES = ("queued", "processing", "completed", "failed")


class Submission(Base):
    __tablename__ = "submissions"

    # Submission UUID — same value as the RQ job_id and the R2 prefix.
    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )

    # Per-submission contact details (a user may submit for different orgs).
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    business_name: Mapped[Optional[str]] = mapped_column(String(255))
    scope_notes: Mapped[Optional[str]] = mapped_column(String(4000))
    deadline: Mapped[Optional[str]] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    error: Mapped[Optional[str]] = mapped_column(String(2000))
    subtotal: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="submissions")
    files: Mapped[List["File"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_submissions_user_submitted", "user_id", "submitted_at"),
    )

    def __repr__(self) -> str:
        return f"<Submission id={self.id} status={self.status}>"


# ---------------------------------------------------------------------------

# File.kind values. 'upload' = customer-supplied PDF, 'result' = output
# JSON/PDF produced by the worker.
FILE_KINDS = ("upload", "result")


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    r2_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    content_type: Mapped[Optional[str]] = mapped_column(String(127))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )

    submission: Mapped["Submission"] = relationship(back_populates="files")

    __table_args__ = (
        UniqueConstraint("submission_id", "kind", "filename",
                         name="uq_files_submission_kind_filename"),
    )

    def __repr__(self) -> str:
        return f"<File id={self.id} kind={self.kind} key={self.r2_key}>"
