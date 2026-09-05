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

    # Confirmed bidding conventions for this customer, consumed by
    # flag_resolver.resolve_flags(). Shape:
    #   {"interior_only": true, "wall_basis_faces": false, ...,
    #    "_conventions_confirmed_at": "2026-09-01T12:00:00+00:00"}
    # Keys are flag_resolver.CONVENTION_FLAGS profile keys. A key that is
    # absent is an OPEN QUESTION, not a "no" — the job runs on the
    # conservative default and raises a convention RFI. The confirmed-at
    # marker is a reviewer's statement that the profile is complete, and
    # silences the RFI for conventions it deliberately omits.
    # NULL → unknown customer: conservative defaults + RFI + held for
    # review, which is exactly what we want for a first job.
    convention_profile: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Inputs for the Usage / ROI tab. Shape:
    #   {"hourly_wage": <float>, "hours_per_estimate": <float>}
    # NULL → fall back to industry-average defaults in the UI.
    usage_settings: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Defaults for the "Send Estimate for Approval" modal on the Completed
    # tab. Shape:
    #   {
    #     "subject_template": "Estimate for {business_name}",
    #     "body_template":    "Hello, ... {subtotal} ...",
    #     "cc":  ["pm@example.com", ...],
    #     "bcc": ["billing@example.com", ...]
    #   }
    # Templates accept {business_name}, {subtotal}, {filename} placeholders.
    # NULL → fall back to the built-in defaults the JS shipped before this
    # column existed.
    message_settings: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Beta gate. New orgs land with is_beta_approved=False and must be
    # approved (manual SQL flip for now). Migration 0004 grandfathers
    # all pre-existing orgs to True so current users aren't locked out.
    is_beta_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )

    # Plan tier for the PLG self-serve motion. 'freemium' orgs are capped at
    # FREEMIUM_BID_LIMIT lifetime bids; 'beta' and 'paid' orgs are never
    # quota-gated. Migration 0023 backfills every pre-existing org to 'beta'
    # so the freemium gate can never fire on a current customer. The quota
    # gate additionally checks PLG_SELF_SERVE_ENABLED, so even a mistakenly
    # 'freemium' org is unaffected while the flag is off.
    plan: Mapped[str] = mapped_column(
        String(16), nullable=False, default="freemium", server_default="freemium",
    )
    # Self-reported company size from the PLG onboarding form ("1-5", "6-20", ...).
    company_size: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Idempotency claims for the freemium lifecycle emails (same UPDATE ...
    # WHERE ... IS NULL pattern as submissions.emailed_at): a requeued worker
    # run must never send a second "you're out of bids" or hot-lead email.
    freemium_exhausted_emailed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    freemium_hot_lead_emailed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
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

    # Job title from the PLG onboarding form ("Estimator", "Owner", ...).
    # Person attribute, not an org one — two users in the same org differ.
    title: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

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

    # Idempotency claim for the outbound result/manual-review email. Set
    # via UPDATE ... WHERE emailed_at IS NULL before sending; a re-run of
    # the job (warm-shutdown requeue, retry) that loses the claim must NOT
    # send a second estimate — multi-pass extraction is non-deterministic,
    # so a duplicate email can carry different numbers.
    emailed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Liveness signal: the worker's heartbeat thread touches this every
    # ~60s while the job is processing. The stuck-job watchdog reaps on
    # STALE HEARTBEAT + RQ cross-check — never on wall-clock age alone
    # (legitimate DD-scale takeoffs run 90+ minutes; the old updated_at
    # sweep would have killed healthy jobs at minute 31).
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Live progress from the engine's _update_progress checkpoints:
    # {"step", "total_steps", "label", "detail", "pct", "updated"}.
    # Surfaced via /api/jobs/<id> so the UI shows real progress instead
    # of a constant 55% bar.
    progress: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Routing decision persisted at first enqueue so every re-enqueue
    # path (requeue scripts, prioritize, resubmit) reuses the original
    # queue and timeout instead of silently shrinking a 4h DD job to the
    # 2h legacy default.
    queue_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    job_timeout: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # The flag posture this job actually ran under, from
    # flag_resolver.resolve_flags(). Shape:
    #   {"flags": {...}, "provenance": {<flag>: "engine|profile|estimate|
    #    evidence"}, "conventions": {...}, "unresolved": [...],
    #    "enabled": bool}
    # Written at enqueue so a result can always be traced back to the
    # conventions that produced it — the missing piece behind the 9/1
    # Caris smoke test, where a -44.9% prod run and a +2.6% validation
    # run were the same code under different, unrecorded flags.
    # `enabled: false` means the resolver ran in shadow: this is what it
    # WOULD have used, not what the job used.
    resolved_flags: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

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


# ---------------------------------------------------------------------------
# Internal CRM (accounts / contacts / notes)
# ---------------------------------------------------------------------------
# Schema is OWNED by the alembic migrations (0011, 0013, 0015-0019) and the
# nightshift-crm Next.js app; these mappings exist so the product can WRITE
# the tables (PLG signup auto-creates an account) and tests can create them
# on sqlite. If a migration changes a crm_* table, mirror it here.
#
# Value constraints (varchar + CHECK in Postgres, enforced here only by
# convention — sqlite tests don't get the CHECKs):
#   status:          partner / prospect / beta / client / churned
#   plan:            growth / scale / enterprise (freemium is NOT allowed;
#                    the CRM UI's PLAN_OPTIONS has no freemium entry, so
#                    PLG accounts carry plan=NULL + a timeline note)
#   contact_status:  new / contacted / do_not_contact
#   account_owner:   brian / matt / steve / elliott

class CrmAccount(Base):
    __tablename__ = "crm_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    knightshift_org_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    address_line1: Mapped[Optional[str]] = mapped_column(String(255))
    address_line2: Mapped[Optional[str]] = mapped_column(String(255))
    city: Mapped[Optional[str]] = mapped_column(String(128))
    state: Mapped[Optional[str]] = mapped_column(String(64))
    postal_code: Mapped[Optional[str]] = mapped_column(String(32))
    country: Mapped[Optional[str]] = mapped_column(String(64))

    industry: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="prospect",
    )
    plan: Mapped[Optional[str]] = mapped_column(String(32))
    contact_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="new",
    )
    account_owner: Mapped[Optional[str]] = mapped_column(String(32))

    website: Mapped[Optional[str]] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    org_size: Mapped[Optional[int]] = mapped_column(Integer)
    annual_revenue: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    estimated_roi: Mapped[Optional[float]] = mapped_column(Numeric(8, 2))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    contacts: Mapped[List["CrmContact"]] = relationship(
        back_populates="account", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<CrmAccount id={self.id} name={self.name!r} org={self.knightshift_org_id}>"


class CrmContact(Base):
    __tablename__ = "crm_contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("crm_accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    title: Mapped[Optional[str]] = mapped_column(String(255))

    address_line1: Mapped[Optional[str]] = mapped_column(String(255))
    address_line2: Mapped[Optional[str]] = mapped_column(String(255))
    city: Mapped[Optional[str]] = mapped_column(String(128))
    state: Mapped[Optional[str]] = mapped_column(String(64))
    postal_code: Mapped[Optional[str]] = mapped_column(String(32))
    country: Mapped[Optional[str]] = mapped_column(String(64))

    lead_source: Mapped[Optional[str]] = mapped_column(String(64))
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    account: Mapped["CrmAccount"] = relationship(back_populates="contacts")

    def __repr__(self) -> str:
        return f"<CrmContact id={self.id} name={self.name!r} account={self.account_id}>"


class CrmAccountNote(Base):
    __tablename__ = "crm_account_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("crm_accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_user_id: Mapped[Optional[str]] = mapped_column(String(255))
    author_email: Mapped[Optional[str]] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )

    def __repr__(self) -> str:
        return f"<CrmAccountNote id={self.id} account={self.account_id}>"
