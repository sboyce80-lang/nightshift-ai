#!/usr/bin/env python3
"""
Knight Shift — SQLAlchemy Models
================================
Five tables:

    users                      — one row per submitter, identified by email.
                                 Linked to a Clerk user_id once auth is wired
                                 up. Each user has a current_organization_id
                                 pointing at the org context they're acting in.
    organizations              — one row per tenant. Corporate orgs are keyed
                                 by email_domain; personal orgs (free-email
                                 signups) have email_domain=NULL and
                                 is_personal=TRUE. Pricing overrides live here
                                 (moved from users in migration 0003).
    organization_memberships   — many-to-many between users and organizations
                                 with a role. Supports the multi-org context
                                 switcher.
    submissions                — one row per /submit request. Owned by a user
                                 AND scoped to an organization (the user's
                                 current org at submission time). The
                                 submission_id (UUID) is the same value used
                                 as the RQ job_id and the R2 key prefix.
    files                      — one row per object in R2 attached to a
                                 submission (uploads + results). Lets us list
                                 a user's history cheaply without scanning R2.
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    String, Integer, SmallInteger, BigInteger, Numeric, DateTime, ForeignKey,
    Index, UniqueConstraint, JSON, Boolean, Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------

class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Lowercase email domain ("riderpaintingny.com") for corporate orgs;
    # NULL for personal orgs created from free-email signups.
    email_domain: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True,
    )

    # True when this org was auto-provisioned for a single user with a
    # free-email address (gmail/yahoo/etc.). Distinguishes from a corporate
    # org that happens to have only one member at the moment.
    is_personal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )

    # Domain ownership verified via webmaster@<domain> email. NULL until the
    # first admin completes verification. Orgs created by the 0003 migration
    # are grandfathered with verified_at = migration time.
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Pricing overrides JSON, formerly on users.pricing_overrides.
    # Shape: {"rates": {<key>: <float>, ...}, "markup": <float>}
    pricing_overrides: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Inputs for the Usage / ROI tab. Shape:
    #   {"hourly_wage": <float>, "hours_per_estimate": <float>}
    # NULL → fall back to industry-average defaults in the UI.
    usage_settings: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Beta gate. New orgs land with is_beta_approved=False and must be
    # approved (manual SQL flip for now). Migration 0004 grandfathers
    # all pre-existing orgs to True so current users aren't locked out.
    is_beta_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    # Per-org rolling-24h submission cap. NULL means use the env default.
    daily_submission_cap: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )

    # When the user completed the /onboarding form (the sign-up gate that
    # captures the explicit company name and triggers the admin notification).
    # NULL = user authenticated but never submitted the access request →
    # they get pushed to /onboarding. NOT NULL = pending review or approved.
    approval_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # When an admin denied this org's access request from /admin/orgs.
    # NOT NULL → org is excluded from pending list, owners see the denied
    # screen instead of the waitlist or onboarding form.
    denied_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Branding + contact fields surfaced on the formal Estimate PDF (the
    # third deliverable alongside the full job PDF + JSON). logo_url is
    # auto-populated from the first owner's Clerk image_url on sign-in;
    # the rest are owner-editable on /account/organization.
    #
    # Two-column logo design: external URLs (Clerk CDN, user-pasted) live
    # in logo_url; bytes uploaded via the drag-drop zone live in R2 with
    # the object key stored in logo_r2_key. The PDF generator prefers the
    # R2 upload when both are set (so a fresh upload "wins" over a stale
    # Clerk avatar) and inlines those bytes as a data URI for portability.
    logo_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    logo_r2_key: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    street_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    postal_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    contact_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    website: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    tax_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    memberships: Mapped[List["OrganizationMembership"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan",
    )
    submissions: Mapped[List["Submission"]] = relationship(back_populates="organization")

    def __repr__(self) -> str:
        return f"<Organization id={self.id} name={self.name!r} domain={self.email_domain!r}>"


# ---------------------------------------------------------------------------

# Two roles for v1. Owner can edit org pricing and invite members. Member
# can run jobs and per-job overrides but not change org-level pricing.
# Add 'admin' as a middle tier later if owners need to delegate.
ORGANIZATION_ROLES = ("owner", "member")


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )

    organization: Mapped["Organization"] = relationship(back_populates="memberships")
    user: Mapped["User"] = relationship(back_populates="memberships")

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "user_id", name="uq_membership_org_user",
        ),
    )

    def __repr__(self) -> str:
        return f"<OrgMembership org={self.organization_id} user={self.user_id} role={self.role}>"


# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    clerk_user_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True)

    # Which org the user is currently acting as. Set on first sign-in to
    # their auto-provisioned org; the multi-org context switcher updates
    # this when the user picks a different org from the dropdown.
    # Nullable because a user may briefly exist between row creation and
    # org assignment; treat None as "no org context, deny pricing reads".
    current_organization_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    submissions: Mapped[List["Submission"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )
    memberships: Mapped[List["OrganizationMembership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )
    current_organization: Mapped[Optional["Organization"]] = relationship(
        foreign_keys=[current_organization_id],
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
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # Per-submission contact details (a user may submit for different orgs).
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    business_name: Mapped[Optional[str]] = mapped_column(String(255))
    scope_notes: Mapped[Optional[str]] = mapped_column(String(4000))
    deadline: Mapped[Optional[str]] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    error: Mapped[Optional[str]] = mapped_column(String(2000))
    subtotal: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))

    # Versioning for re-runs. v1 has parent_submission_id=NULL; revisions
    # (revised plans, RFI responses, amendments) point at the parent and
    # increment version. The merge worker re-extracts only the new files
    # and merges into the parent's stored result JSON.
    parent_submission_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("submissions.id", ondelete="SET NULL"),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1",
    )
    merge_notes: Mapped[Optional[str]] = mapped_column(Text)
    merge_scope_tags: Mapped[Optional[list]] = mapped_column(JSON)

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="submissions")
    organization: Mapped["Organization"] = relationship(back_populates="submissions")
    files: Mapped[List["File"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan",
    )

    parent: Mapped[Optional["Submission"]] = relationship(
        "Submission", remote_side="Submission.id", foreign_keys=[parent_submission_id],
        backref="revisions",
    )

    __table_args__ = (
        Index("ix_submissions_user_submitted", "user_id", "submitted_at"),
        Index("ix_submissions_parent_version", "parent_submission_id", "version"),
    )

    def __repr__(self) -> str:
        return f"<Submission id={self.id} v={self.version} status={self.status}>"


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
