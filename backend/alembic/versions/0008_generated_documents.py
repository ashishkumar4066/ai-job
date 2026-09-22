"""generated_documents — Stage 3 tailored résumés, stored as LaTeX

The table CLAUDE.md's Phase 2C Stage 3 schema calls for, with the columns that
turned out to be needed to make it honest:

* `tex` rather than a PDF. The LaTeX is the editable source of truth; the PDF
  is a pure function of it and recompiles in ~3.5s (`app/latex.py`).
* `tailoring` (what the model said) beside `edits` (what was applied), because
  the fact check discards rewrites and "why is this bullet unchanged?" has to
  stay answerable.
* `issues` — the rejected metrics and flagged terms from `app/factcheck.py`.
* `content_hash` + `prompt_version`, so a document generated against an older
  JD or an older prompt can be shown as stale instead of current.

Unique on (job_id, profile_version, kind): a résumé tailored against a
different profile is a different document, exactly as a score is.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generated_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("profile_version", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="resume"),
        sa.Column("tex", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("edits", sa.JSON(), nullable=True),
        sa.Column("tailoring", sa.JSON(), nullable=True),
        sa.Column("issues", sa.JSON(), nullable=True),
        sa.Column("llm_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("llm_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hand_edited", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_draft", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id", "profile_version", "kind", name="uq_generated_documents_job_profile_kind"
        ),
    )
    op.create_index("ix_generated_documents_job_id", "generated_documents", ["job_id"])
    op.create_index(
        "ix_generated_documents_profile_version", "generated_documents", ["profile_version"]
    )


def downgrade() -> None:
    op.drop_index("ix_generated_documents_profile_version", table_name="generated_documents")
    op.drop_index("ix_generated_documents_job_id", table_name="generated_documents")
    op.drop_table("generated_documents")
