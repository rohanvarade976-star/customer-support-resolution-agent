"""manual runs and one refund per proposal

Revision ID: c4f1a7d2e9b3
Revises: ad32851f6dde
Create Date: 2026-11-11 10:00:00.000000

- workflow_runs.mode may now be 'manual' (a reviewer-driven run with no LLM).
- refund_operations.proposal_id becomes unique: at most one refund per proposal.
  The unique constraint brings its own index, so the old plain index is dropped.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f1a7d2e9b3"
down_revision: str | Sequence[str] | None = "ad32851f6dde"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_workflow_runs_mode"), "workflow_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_workflow_runs_mode"),
        "workflow_runs",
        "mode IN ('multi_agent', 'baseline', 'manual')",
    )

    op.drop_index(op.f("ix_refund_operations_proposal_id"), table_name="refund_operations")
    op.create_unique_constraint(
        op.f("uq_refund_operations_proposal_id"), "refund_operations", ["proposal_id"]
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("uq_refund_operations_proposal_id"), "refund_operations", type_="unique"
    )
    op.create_index(
        op.f("ix_refund_operations_proposal_id"), "refund_operations", ["proposal_id"], unique=False
    )

    # Fails if any manual run exists, which is what we want: never drop data silently.
    op.drop_constraint(op.f("ck_workflow_runs_mode"), "workflow_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_workflow_runs_mode"), "workflow_runs", "mode IN ('multi_agent', 'baseline')"
    )
