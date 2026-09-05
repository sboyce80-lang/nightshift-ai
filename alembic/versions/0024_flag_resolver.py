"""Per-job flag resolver: org convention profile + resolved job posture

Revision ID: 0024_flag_resolver
Revises: 0023_plg_freemium
Create Date: 2026-09-01

Schema for the per-job flag resolver (NIGHTSHIFT_FLAG_RESOLVER, default
OFF — running this migration changes no behavior on its own):

- organizations.convention_profile: the customer's CONFIRMED bidding
  conventions (interior-only, wall basis, schedule scope, allowance
  style), keyed by flag_resolver.CONVENTION_FLAGS profile keys. NULL for
  every existing org, which is the correct starting state: an unknown
  customer runs on conservative defaults, raises a convention RFI, and
  is held for review until a reviewer confirms the answers.
- submissions.resolved_flags: the posture a job actually ran under,
  including per-flag provenance (engine / profile / estimate /
  evidence). Backfills NULL — jobs that predate the resolver have no
  recorded posture, and NULL says exactly that rather than implying
  they ran on defaults.

Deliberately no backfill of convention_profile. Guessing a customer's
conventions from past estimates is the failure this whole build exists
to end; the profile is populated by reviewer write-back only.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0024_flag_resolver"
down_revision: Union[str, Sequence[str], None] = "0023_plg_freemium"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("convention_profile", sa.JSON(), nullable=True),
    )
    op.add_column(
        "submissions",
        sa.Column("resolved_flags", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("submissions", "resolved_flags")
    op.drop_column("organizations", "convention_profile")
