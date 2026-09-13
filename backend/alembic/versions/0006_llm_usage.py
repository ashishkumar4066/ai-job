"""llm_usage — the ledger Groq's daily limits are enforced from

Groq's free tier limits each model per minute AND per day (30 RPM, 1K RPD,
8K TPM, 200K TPD for qwen/qwen3.8-27b), but the response headers only report
requests-per-day and tokens-per-minute. Tokens-per-day is invisible on the
wire, so it is counted here, persisted so a restarted pass still knows what
today has already spent.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_llm_usage_created_at", "llm_usage", ["created_at"])
    op.create_index("ix_llm_usage_model_created", "llm_usage", ["model", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_model_created", table_name="llm_usage")
    op.drop_index("ix_llm_usage_created_at", table_name="llm_usage")
    op.drop_table("llm_usage")
