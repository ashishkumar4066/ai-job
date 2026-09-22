"""generated_documents.chat — the résumé refinement conversation

One JSON column holding the chat in `app/resume_chat.py`: each message, and on
assistant turns the fact-checked proposal it made and whether it was applied.
A column rather than a table because the conversation is only ever read and
written whole, with its document, and dies with it on regenerate.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("generated_documents") as batch:
        batch.add_column(sa.Column("chat", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("generated_documents") as batch:
        batch.drop_column("chat")
