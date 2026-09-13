"""The preference verdict on job_matches — an overlay, not part of the key

Why these are plain mutable columns and NOT part of the unique key
------------------------------------------------------------------
`job_matches` is unique on `(job_id, profile_version)`, and that is the right
key for a *score*: editing a résumé produces genuinely different scores, and
keeping the old rows is what makes "why did this drop from 78 to 61?"
answerable.

A preference verdict is not like that. Preferences do not change any job's
score — they only decide which scored rows the Matches list shows. If
`prefs_version` joined the key, every tweak of "full-time only" would fork
929 new rows carrying identical scores, and the whole point of gating scoring
rather than eligibility (that changing your mind is cheap) would be lost.

So the verdict is an overlay, refreshed in place on every scoring pass:

  * `prefs_version`  — which preferences last judged this row. Lets the API
    detect a stale verdict and re-gate rather than showing a list filtered by
    yesterday's preferences.
  * `prefs_pass`     — did it match. Indexed, because it is the Matches list's
    hottest predicate.
  * `prefs_reasons`  — why, for passes as well as failures, following
    `eligibility_reasons`. "Admitted because it stated no salary" is the
    signal that tells you a preference is not doing what you meant.

Defaults admit everything
-------------------------
`prefs_pass` defaults to TRUE. A row scored before this migration, or by an
older build, must not vanish from Matches because it has never been gated —
an ungated row is unfiltered, not rejected. Same doctrine as NULL-passes in
`prefs_gate.py`.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_matches",
        sa.Column("prefs_version", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column(
        "job_matches",
        sa.Column("prefs_pass", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "job_matches",
        sa.Column("prefs_reasons", sa.JSON(), nullable=False, server_default="[]"),
    )
    # The Matches list's hot path: this profile version, matching preferences,
    # ordered by score.
    op.create_index(
        "ix_job_matches_prefs", "job_matches", ["profile_version", "prefs_pass", "score"]
    )


def downgrade() -> None:
    op.drop_index("ix_job_matches_prefs", table_name="job_matches")
    op.drop_column("job_matches", "prefs_reasons")
    op.drop_column("job_matches", "prefs_pass")
    op.drop_column("job_matches", "prefs_version")
