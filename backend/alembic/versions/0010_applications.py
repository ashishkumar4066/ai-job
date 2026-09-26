"""applications — what I sent and where it got to

The tracking layer the Dashboard reads. A table rather than columns on
`job_postings` because an application is a fact about *me and a posting*, not
about the posting: the board neither knows nor cares, and a closure sweep must
never touch it.

Unique on `job_id` — applying twice to one posting is a mistake, not two
applications.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="applied"),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("history", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_applications_job"),
    )
    op.create_index("ix_applications_job_id", "applications", ["job_id"])
    op.create_index("ix_applications_status", "applications", ["status"])
    # Backs the "silent for N days" read, which scans by status and age.
    op.create_index(
        "ix_applications_status_changed", "applications", ["status", "status_changed_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_applications_status_changed", table_name="applications")
    op.drop_index("ix_applications_status", table_name="applications")
    op.drop_index("ix_applications_job_id", table_name="applications")
    op.drop_table("applications")
