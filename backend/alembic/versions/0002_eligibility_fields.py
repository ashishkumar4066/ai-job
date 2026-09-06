"""eligibility + currency fields on job_postings

Adds the inputs and the verdict for the ingestion filter stage:
candidate-location eligibility, timezone restrictions, structured salary, the
derived US-employer flag, and `eligibility_pass` + its reasons.

Existing rows predate the filter, so they are backfilled as not-passing with an
explicit `not_evaluated` reason rather than a silent `false` — the next ingest
run re-decides every posting it fetches.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite cannot add a NOT NULL column without a default, and the JSON
    # columns need a literal one; batch_alter_table handles the table rebuild.
    with op.batch_alter_table("job_postings") as batch:
        batch.add_column(
            sa.Column(
                "location_eligibility", sa.JSON(), nullable=False, server_default="[]"
            )
        )
        batch.add_column(
            sa.Column(
                "timezone_restrictions", sa.JSON(), nullable=False, server_default="[]"
            )
        )
        batch.add_column(sa.Column("salary_min", sa.Float(), nullable=True))
        batch.add_column(sa.Column("salary_max", sa.Float(), nullable=True))
        batch.add_column(sa.Column("salary_currency", sa.String(length=8), nullable=True))
        batch.add_column(sa.Column("is_us_employer", sa.Boolean(), nullable=True))
        batch.add_column(
            sa.Column(
                "eligibility_pass",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "eligibility_reasons", sa.JSON(), nullable=False, server_default="[]"
            )
        )

    op.create_index(
        "ix_job_postings_salary_currency", "job_postings", ["salary_currency"]
    )
    op.create_index(
        "ix_job_postings_eligibility_pass", "job_postings", ["eligibility_pass"]
    )
    op.create_index(
        "ix_job_postings_eligible_status",
        "job_postings",
        ["eligibility_pass", "status"],
    )

    # Say why, rather than leaving pre-filter rows looking deliberately rejected.
    op.execute(
        "UPDATE job_postings SET eligibility_reasons = '[\"not_evaluated:ingested before "
        "the eligibility filter existed\"]'"
    )

    with op.batch_alter_table("ingest_runs") as batch:
        batch.add_column(
            sa.Column("eligible", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    op.drop_index("ix_job_postings_eligible_status", table_name="job_postings")
    op.drop_index("ix_job_postings_eligibility_pass", table_name="job_postings")
    op.drop_index("ix_job_postings_salary_currency", table_name="job_postings")

    with op.batch_alter_table("job_postings") as batch:
        batch.drop_column("eligibility_reasons")
        batch.drop_column("eligibility_pass")
        batch.drop_column("is_us_employer")
        batch.drop_column("salary_currency")
        batch.drop_column("salary_max")
        batch.drop_column("salary_min")
        batch.drop_column("timezone_restrictions")
        batch.drop_column("location_eligibility")

    with op.batch_alter_table("ingest_runs") as batch:
        batch.drop_column("eligible")
