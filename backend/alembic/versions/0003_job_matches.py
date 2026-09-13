"""job_matches — deterministic fit scores per (job, profile_version)

A fit score is a fact about a *pairing*, not about a posting, so it gets its
own table rather than columns on `job_postings`. Re-scoring against an edited
résumé writes new rows under a new `profile_version` and leaves the old ones
readable, which is what makes "why did this fall from 78 to 61?" answerable.

The LLM columns (`llm_used`, `llm_content_hash`, `llm_verdict`) ship empty.
The deep-read pass fills them; the free ranker never touches them. Keeping the
two cache keys separate — `profile_version` for fit, `llm_content_hash` for the
JD text — is what stops a résumé edit from re-billing validation for jobs whose
description never changed.

Dialect-neutral per the stack rules: JSON not JSONB, no ARRAY, UtcDateTime.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.models import UtcDateTime

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_matches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("job_postings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("profile_version", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("subscores", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("match_reasons", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("confident", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("matched_skills", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("missing_stacks", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("years_required", sa.Integer(), nullable=True),
        sa.Column("llm_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("llm_content_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("llm_verdict", sa.JSON(), nullable=True),
        sa.Column("scored_at", UtcDateTime(), nullable=False),
        sa.UniqueConstraint("job_id", "profile_version", name="uq_job_matches_job_profile"),
    )
    op.create_index("ix_job_matches_job_id", "job_matches", ["job_id"])
    op.create_index("ix_job_matches_profile_version", "job_matches", ["profile_version"])
    op.create_index("ix_job_matches_score", "job_matches", ["score"])
    op.create_index("ix_job_matches_confident", "job_matches", ["confident"])
    op.create_index("ix_job_matches_llm_used", "job_matches", ["llm_used"])
    # The list view's hot path: newest profile version, ordered by score.
    op.create_index(
        "ix_job_matches_profile_score", "job_matches", ["profile_version", "score"]
    )


def downgrade() -> None:
    op.drop_index("ix_job_matches_profile_score", table_name="job_matches")
    op.drop_index("ix_job_matches_llm_used", table_name="job_matches")
    op.drop_index("ix_job_matches_confident", table_name="job_matches")
    op.drop_index("ix_job_matches_score", table_name="job_matches")
    op.drop_index("ix_job_matches_profile_version", table_name="job_matches")
    op.drop_index("ix_job_matches_job_id", table_name="job_matches")
    op.drop_table("job_matches")
