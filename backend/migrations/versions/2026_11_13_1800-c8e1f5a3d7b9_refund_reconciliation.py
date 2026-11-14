"""refund reconciliation

Revision ID: c8e1f5a3d7b9
Revises: a6c2e8d4f1b7
Create Date: 2026-11-13 18:00:00

- refund_operations gains the reconciler's bookkeeping (ADR-014): how many times it was
  checked (also the fencing token), when the next check is due, whether the refund request
  was re-sent, and the last check's error code. Operations that are not final yet get a
  check due straight away, so refunds left behind before this migration are reconciled too.
- jobs gains the scheduled `reconcile_refunds` sweep type.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8e1f5a3d7b9"
down_revision: str | Sequence[str] | None = "a6c2e8d4f1b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_JOB_TYPES = (
    "start_run",
    "resume_after_decision",
    "resume_after_customer_reply",
    "expire_approvals",
    "purge_expired_memory",
)
NEW_JOB_TYPES = (*OLD_JOB_TYPES, "reconcile_refunds")
OLD_SWEEPS_SQL = "job_type IN ('expire_approvals', 'purge_expired_memory')"
NEW_SWEEPS_SQL = "job_type IN ('expire_approvals', 'purge_expired_memory', 'reconcile_refunds')"
OPEN_SQL = "status IN ('queued', 'running')"


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _set_sweep_types(job_types: Sequence[str], sweeps_sql: str) -> None:
    op.drop_constraint(op.f("ck_jobs_job_type"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_job_type"), "jobs", f"job_type IN ({_in(job_types)})")
    op.drop_constraint(op.f("ck_jobs_run_required"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_run_required"), "jobs", f"run_id IS NOT NULL OR {sweeps_sql}"
    )
    op.drop_index("uq_jobs_one_open_sweep_per_type", table_name="jobs")
    op.create_index(
        "uq_jobs_one_open_sweep_per_type",
        "jobs",
        ["job_type"],
        unique=True,
        postgresql_where=sa.text(f"{sweeps_sql} AND {OPEN_SQL}"),
    )


def upgrade() -> None:
    # --- refund_operations: reconciliation bookkeeping
    op.add_column(
        "refund_operations",
        sa.Column("reconcile_checks", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "refund_operations",
        sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "refund_operations", sa.Column("resent_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "refund_operations",
        sa.Column("last_reconcile_error", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_refund_operations_reconcile_checks_non_negative"),
        "refund_operations",
        "reconcile_checks >= 0",
    )
    op.create_index(
        "ix_refund_operations_next_reconcile_at",
        "refund_operations",
        ["next_reconcile_at"],
        postgresql_where=sa.text("next_reconcile_at IS NOT NULL"),
    )
    # Unfinished refunds from before this migration: check them at the next sweep.
    op.execute(
        "UPDATE refund_operations SET next_reconcile_at = now() "
        "WHERE status IN ('reserved', 'submitted', 'processing', 'pending_reconciliation')"
    )

    # --- jobs: the reconciliation sweep
    _set_sweep_types(NEW_JOB_TYPES, NEW_SWEEPS_SQL)


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE job_type = 'reconcile_refunds'")
    _set_sweep_types(OLD_JOB_TYPES, OLD_SWEEPS_SQL)

    op.drop_index("ix_refund_operations_next_reconcile_at", table_name="refund_operations")
    op.drop_constraint(
        op.f("ck_refund_operations_reconcile_checks_non_negative"),
        "refund_operations",
        type_="check",
    )
    op.drop_column("refund_operations", "last_reconcile_error")
    op.drop_column("refund_operations", "resent_at")
    op.drop_column("refund_operations", "next_reconcile_at")
    op.drop_column("refund_operations", "reconcile_checks")
