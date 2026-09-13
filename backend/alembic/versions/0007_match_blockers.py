"""job_matches.blockers / blocked — hard blockers read from JD prose, for free

The deep read's most useful output was `blocked` (sponsorship demanded, US
work authorization, region-only remote). `app/blockers.py` reads the formulaic
cases out of the JD instead, and a blocked job never reaches the LLM
shortlist. `blocked` is a separate indexed boolean so the shortlist stays a
plain SQL predicate — "JSON list is empty" has no dialect-neutral spelling.

NULL `blockers` means "not checked yet"; the next ranking pass fills it.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job_matches") as batch:
        batch.add_column(sa.Column("blockers", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("blocked", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.create_index("ix_job_matches_blocked", ["blocked"])


def downgrade() -> None:
    with op.batch_alter_table("job_matches") as batch:
        batch.drop_index("ix_job_matches_blocked")
        batch.drop_column("blocked")
        batch.drop_column("blockers")
