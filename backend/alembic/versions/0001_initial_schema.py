"""initial schema: job_postings + ingest_runs

Revision ID: 0001
Revises:
Create Date: 2026-08-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_postings",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("source_key", sa.String(length=512), nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("ats", sa.String(length=64), nullable=False),
        sa.Column("company", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("locations", sa.JSON(), nullable=False),
        sa.Column("remote", sa.Boolean(), nullable=False),
        sa.Column("department", sa.String(length=255), nullable=True),
        sa.Column("apply_url", sa.Text(), nullable=False),
        sa.Column("description_html", sa.Text(), nullable=True),
        sa.Column("description_text", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_json", sa.JSON(), nullable=True),
    )
    # Identity: dedupe and tracking key. Uniqueness is what makes ingest idempotent.
    op.create_index("ix_job_postings_source_key", "job_postings", ["source_key"], unique=True)
    op.create_index("ix_job_postings_source_id", "job_postings", ["source_id"])
    op.create_index("ix_job_postings_ats", "job_postings", ["ats"])
    op.create_index("ix_job_postings_company", "job_postings", ["company"])
    op.create_index("ix_job_postings_remote", "job_postings", ["remote"])
    op.create_index("ix_job_postings_status", "job_postings", ["status"])
    op.create_index("ix_job_postings_posted_at", "job_postings", ["posted_at"])
    op.create_index("ix_job_postings_first_seen_at", "job_postings", ["first_seen_at"])
    op.create_index("ix_job_postings_source_status", "job_postings", ["source_id", "status"])
    op.create_index("ix_job_postings_status_posted", "job_postings", ["status", "posted_at"])

    op.create_table(
        "ingest_runs",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("closed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sources", sa.JSON(), nullable=True),
    )
    op.create_index("ix_ingest_runs_started_at", "ingest_runs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_ingest_runs_started_at", table_name="ingest_runs")
    op.drop_table("ingest_runs")

    for index in (
        "ix_job_postings_status_posted",
        "ix_job_postings_source_status",
        "ix_job_postings_first_seen_at",
        "ix_job_postings_posted_at",
        "ix_job_postings_status",
        "ix_job_postings_remote",
        "ix_job_postings_company",
        "ix_job_postings_ats",
        "ix_job_postings_source_id",
        "ix_job_postings_source_key",
    ):
        op.drop_index(index, table_name="job_postings")
    op.drop_table("job_postings")
