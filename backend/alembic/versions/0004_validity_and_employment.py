"""Validity columns, the LLM cache split, and two structured fields

Three changes, all on `job_postings`, all for Phase 2B.

1. Validity, as its own columns
-------------------------------
`validity_score` / `validity_reasons` / `validity_checked_at` hold the
deterministic verdict from `app/validation.py`. They live on the posting
because validity is a fact about the *posting* — unlike a fit score, which is
a fact about a (posting, profile) pairing and stays in `job_matches`.

2. The LLM cache split — the reason this migration is not optional
-----------------------------------------------------------------
One Groq call returns both halves of a screen: validity and fit. Until now
BOTH were stored in `job_matches.llm_verdict`, a row keyed by
`(job_id, profile_version)`. So editing `profile.yaml` created fresh rows with
an empty verdict and the next pass re-read *validity* too — ~440 requests and
~2 hours to recompute an answer about JD text that never changed.

CLAUDE.md specifies two cache keys for exactly this reason: `content_hash` for
the validity half, `content_hash + profile_version` for the fit half. That
needs two homes, so the validity half moves here:

  * `llm_validity`      — the validity fields of the verdict
  * `llm_validity_hash` — its cache key, the `content_hash` alone

`job_matches.llm_verdict` keeps the fit half and its existing two-part key.
A profile edit now costs zero validity calls.

3. `employment_type` and `workplace_type`
-----------------------------------------
Both are already parsed by the adapters and then dropped on the floor. They
are what the preferences layer filters on ("full-time", "remote"), and they
are recoverable for 3,283 of 5,514 open rows from `raw_json` with no re-fetch —
Ashby, Himalayas, Wellfound and Remotive at 100%, Lever at 98%. Greenhouse
publishes neither, so its rows stay NULL, and NULL must PASS a preference:
the same "unstated passes" doctrine the pay and years rules already follow.
`scripts/backfill_employment.py` fills them from stored `raw_json`.

`workplace_type` also gives `detect_remote` a real signal instead of a regex
over location strings, which is the blocker on `require_remote` in
`config/filters.yaml`.

Dialect-neutral per the stack rules: JSON not JSONB, no ARRAY, UtcDateTime.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.models import UtcDateTime

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- 1. Deterministic validity -----------------------------------------
    # `validity_score` is nullable, not defaulted to 0: NULL means "not yet
    # checked", 0 means "checked and worthless". Collapsing those would make
    # an unrun pass indistinguishable from a board full of ghosts.
    op.add_column("job_postings", sa.Column("validity_score", sa.Integer(), nullable=True))
    op.add_column(
        "job_postings",
        sa.Column("validity_reasons", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "job_postings", sa.Column("validity_checked_at", UtcDateTime(), nullable=True)
    )

    # --- 2. The LLM validity half, keyed on content_hash alone -------------
    op.add_column("job_postings", sa.Column("llm_validity", sa.JSON(), nullable=True))
    op.add_column(
        "job_postings",
        sa.Column("llm_validity_hash", sa.String(length=64), nullable=False, server_default=""),
    )

    # --- 3. Structured fields the preferences layer filters on -------------
    op.add_column(
        "job_postings", sa.Column("employment_type", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "job_postings", sa.Column("workplace_type", sa.String(length=16), nullable=True)
    )

    op.create_index("ix_job_postings_validity_score", "job_postings", ["validity_score"])
    op.create_index("ix_job_postings_employment_type", "job_postings", ["employment_type"])
    op.create_index("ix_job_postings_workplace_type", "job_postings", ["workplace_type"])

    # --- Backfill the validity half from verdicts already paid for ---------
    # 10 rows were screened before the split. Their validity answers are still
    # valid — the JD has not moved — so carry them over rather than re-billing.
    # Written as one UPDATE ... FROM-free correlated subquery so it runs on
    # SQLite and Postgres alike.
    op.execute(
        """
        UPDATE job_postings
           SET llm_validity = (
                   SELECT json_object(
                       'posting_status',      json_extract(m.llm_verdict, '$.posting_status'),
                       'status_reason',       json_extract(m.llm_verdict, '$.status_reason'),
                       'seniority',           json_extract(m.llm_verdict, '$.seniority'),
                       'tech_stack',          json_extract(m.llm_verdict, '$.tech_stack'),
                       'sponsorship_required',json_extract(m.llm_verdict, '$.sponsorship_required'),
                       'location_policy',     json_extract(m.llm_verdict, '$.location_policy'),
                       'work_mode',           json_extract(m.llm_verdict, '$.work_mode'),
                       'compensation_text',   json_extract(m.llm_verdict, '$.compensation_text'),
                       'inconsistencies',     json_extract(m.llm_verdict, '$.inconsistencies')
                   )
                     FROM job_matches m
                    WHERE m.job_id = job_postings.id
                      AND m.llm_verdict IS NOT NULL
                      AND m.llm_content_hash = job_postings.content_hash
                    LIMIT 1
               ),
               llm_validity_hash = content_hash
         WHERE EXISTS (
                   SELECT 1 FROM job_matches m
                    WHERE m.job_id = job_postings.id
                      AND m.llm_verdict IS NOT NULL
                      AND m.llm_content_hash = job_postings.content_hash
               )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_job_postings_workplace_type", table_name="job_postings")
    op.drop_index("ix_job_postings_employment_type", table_name="job_postings")
    op.drop_index("ix_job_postings_validity_score", table_name="job_postings")
    op.drop_column("job_postings", "workplace_type")
    op.drop_column("job_postings", "employment_type")
    op.drop_column("job_postings", "llm_validity_hash")
    op.drop_column("job_postings", "llm_validity")
    op.drop_column("job_postings", "validity_checked_at")
    op.drop_column("job_postings", "validity_reasons")
    op.drop_column("job_postings", "validity_score")
