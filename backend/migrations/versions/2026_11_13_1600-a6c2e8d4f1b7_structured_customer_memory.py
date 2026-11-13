"""structured customer memory, memory purge job

Revision ID: a6c2e8d4f1b7
Revises: f3a9d6b2c8e4
Create Date: 2026-11-13 16:00:00.000000

M7 Task 3, long-term customer memory (ADR-013):

- customer_memory is rebuilt with structured fields only (kind, order, outcome code,
  category, source run, expiry). The old free-text `summary` column is gone, and so are any
  rows it held: free text cannot be trusted as memory, so nothing is converted.
- jobs gains the scheduled `purge_expired_memory` type. Like `expire_approvals` it has no
  run, and at most one of each sweep type may be open at a time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a6c2e8d4f1b7"
down_revision: str | Sequence[str] | None = "f3a9d6b2c8e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_JOB_TYPES = (
    "start_run",
    "resume_after_decision",
    "resume_after_customer_reply",
    "expire_approvals",
)
NEW_JOB_TYPES = (*OLD_JOB_TYPES, "purge_expired_memory")
OLD_SWEEPS_SQL = "job_type = 'expire_approvals'"
NEW_SWEEPS_SQL = "job_type IN ('expire_approvals', 'purge_expired_memory')"
OPEN_SQL = "status IN ('queued', 'running')"


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _enum(name: str, *values: str) -> sa.Enum:
    """A string column guarded by a CHECK constraint, as `str_enum` builds it in the models."""
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


def upgrade() -> None:
    _drop_memory_indexes(["customer_id", "ticket_id"])
    op.drop_table("customer_memory")
    op.create_table(
        "customer_memory",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column(
            "kind",
            _enum("kind", "case_completed", "case_escalated", "refund_executed"),
            nullable=False,
        ),
        sa.Column(
            "outcome_code",
            _enum(
                "outcome_code",
                "status_explained",
                "refund_rejected",
                "refund_submitted",
                "refund_processing",
                "refund_succeeded",
                "refund_failed",
                "escalated",
            ),
            nullable=False,
        ),
        sa.Column(
            "category", _enum("category", "order_status", "refund", "damaged_item"), nullable=True
        ),
        sa.Column("source_run_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_customer_memory_expires_after_created")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_customer_memory_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id", "customer_id"],
            ["tickets.id", "tickets.customer_id"],
            name=op.f("fk_customer_memory_ticket_id_customer_id_tickets"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "customer_id"],
            ["orders.id", "orders.customer_id"],
            name=op.f("fk_customer_memory_order_id_customer_id_orders"),
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_customer_memory_source_run_id_workflow_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_memory")),
        sa.UniqueConstraint("source_run_id", name=op.f("uq_customer_memory_source_run_id")),
    )
    for column in ("customer_id", "ticket_id", "order_id"):
        op.create_index(
            op.f(f"ix_customer_memory_{column}"), "customer_memory", [column], unique=False
        )
    op.create_index(
        "ix_customer_memory_customer_id_expires_at",
        "customer_memory",
        ["customer_id", "expires_at"],
        unique=False,
    )

    # --- jobs: the memory purge sweep
    op.drop_constraint(op.f("ck_jobs_job_type"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_job_type"), "jobs", f"job_type IN ({_in(NEW_JOB_TYPES)})"
    )
    op.drop_constraint(op.f("ck_jobs_run_required"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_run_required"), "jobs", f"run_id IS NOT NULL OR {NEW_SWEEPS_SQL}"
    )
    op.drop_index("uq_jobs_one_open_expiry_sweep", table_name="jobs")
    op.create_index(
        "uq_jobs_one_open_sweep_per_type",
        "jobs",
        ["job_type"],
        unique=True,
        postgresql_where=sa.text(f"{NEW_SWEEPS_SQL} AND {OPEN_SQL}"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE job_type = 'purge_expired_memory'")
    op.drop_index("uq_jobs_one_open_sweep_per_type", table_name="jobs")
    op.create_index(
        "uq_jobs_one_open_expiry_sweep",
        "jobs",
        ["job_type"],
        unique=True,
        postgresql_where=sa.text(f"{OLD_SWEEPS_SQL} AND {OPEN_SQL}"),
    )
    op.drop_constraint(op.f("ck_jobs_run_required"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_run_required"), "jobs", f"run_id IS NOT NULL OR {OLD_SWEEPS_SQL}"
    )
    op.drop_constraint(op.f("ck_jobs_job_type"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_job_type"), "jobs", f"job_type IN ({_in(OLD_JOB_TYPES)})"
    )

    # Structured items cannot become free-text summaries: the old table comes back empty.
    op.drop_index("ix_customer_memory_customer_id_expires_at", table_name="customer_memory")
    _drop_memory_indexes(["customer_id", "ticket_id", "order_id"])
    op.drop_table("customer_memory")
    op.create_table(
        "customer_memory",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_customer_memory_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id", "customer_id"],
            ["tickets.id", "tickets.customer_id"],
            name=op.f("fk_customer_memory_ticket_id_customer_id_tickets"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_memory")),
    )
    for column in ("customer_id", "ticket_id"):
        op.create_index(
            op.f(f"ix_customer_memory_{column}"), "customer_memory", [column], unique=False
        )


def _drop_memory_indexes(columns: list[str]) -> None:
    for column in columns:
        op.drop_index(op.f(f"ix_customer_memory_{column}"), table_name="customer_memory")
