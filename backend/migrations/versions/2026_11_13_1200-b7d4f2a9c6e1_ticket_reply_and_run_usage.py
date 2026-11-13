"""ticket customer reply and per-run LLM usage

Revision ID: b7d4f2a9c6e1
Revises: e5a8c3b7d912
Create Date: 2026-11-13 12:00:00.000000

- tickets gains customer_reply: the latest message a workflow run showed the customer.
- workflow_runs gains llm_calls, input_tokens and output_tokens: what the run cost, so the
  single-agent and multi-agent modes can be compared.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d4f2a9c6e1"
down_revision: str | Sequence[str] | None = "e5a8c3b7d912"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("customer_reply", sa.Text(), nullable=True))
    for column in ("llm_calls", "input_tokens", "output_tokens"):
        op.add_column(
            "workflow_runs",
            sa.Column(column, sa.Integer(), server_default="0", nullable=False),
        )
    op.create_check_constraint(
        op.f("ck_workflow_runs_usage_non_negative"),
        "workflow_runs",
        "llm_calls >= 0 AND input_tokens >= 0 AND output_tokens >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_workflow_runs_usage_non_negative"), "workflow_runs", type_="check")
    for column in ("output_tokens", "input_tokens", "llm_calls"):
        op.drop_column("workflow_runs", column)
    op.drop_column("tickets", "customer_reply")
