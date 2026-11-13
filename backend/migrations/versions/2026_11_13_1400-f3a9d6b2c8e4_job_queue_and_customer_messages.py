"""job queue fields, customer messages, one unfinished run per ticket

Revision ID: f3a9d6b2c8e4
Revises: b7d4f2a9c6e1
Create Date: 2026-11-13 14:00:00.000000

M7 Task 2, the background worker (ADR-012):

- jobs gains job_type, payload (ids only), max_attempts, lease_expires_at and
  last_error_code. locked_at and last_error are replaced: a lease expiry says more than a
  lock time, and an error code cannot leak customer text the way a message can. The status
  `failed` becomes `dead`. run_id becomes optional, for the scheduled approval-expiry sweep.
- customer_messages stores customer replies, so a job can carry just the message id.
- workflow_runs gets a partial unique index: at most one unfinished run per ticket. This
  fails if a database already holds two unfinished runs on one ticket; reset the demo
  data first in that case.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a9d6b2c8e4"
down_revision: str | Sequence[str] | None = "b7d4f2a9c6e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_TYPES = (
    "start_run",
    "resume_after_decision",
    "resume_after_customer_reply",
    "expire_approvals",
)
NEW_STATUSES = ("queued", "running", "succeeded", "dead")
OLD_STATUSES = ("queued", "running", "succeeded", "failed")


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # --- jobs: status `failed` -> `dead`
    op.drop_constraint(op.f("ck_jobs_status"), "jobs", type_="check")
    op.execute("UPDATE jobs SET status = 'dead' WHERE status = 'failed'")
    op.create_check_constraint(op.f("ck_jobs_status"), "jobs", f"status IN ({_in(NEW_STATUSES)})")

    # --- jobs: new columns. Rows from before this migration never ran; call them start_run.
    op.add_column("jobs", sa.Column("job_type", sa.String(length=32), nullable=True))
    op.execute("UPDATE jobs SET job_type = 'start_run'")
    op.alter_column("jobs", "job_type", nullable=False)
    op.create_check_constraint(op.f("ck_jobs_job_type"), "jobs", f"job_type IN ({_in(JOB_TYPES)})")
    op.add_column(
        "jobs",
        sa.Column(
            "payload",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "jobs", sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False)
    )
    op.add_column("jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("jobs", sa.Column("last_error_code", sa.String(length=64)))
    op.drop_column("jobs", "locked_at")
    op.drop_column("jobs", "last_error")
    op.alter_column("jobs", "run_id", nullable=True)
    op.create_check_constraint(op.f("ck_jobs_max_attempts_positive"), "jobs", "max_attempts >= 1")
    op.create_check_constraint(
        op.f("ck_jobs_run_required"), "jobs", "run_id IS NOT NULL OR job_type = 'expire_approvals'"
    )
    op.create_index(
        "uq_jobs_one_open_expiry_sweep",
        "jobs",
        ["job_type"],
        unique=True,
        postgresql_where=sa.text(
            "job_type = 'expire_approvals' AND status IN ('queued', 'running')"
        ),
    )

    # --- customer_messages
    op.create_table(
        "customer_messages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_customer_messages_customer_id_customers"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_customer_messages_run_id_workflow_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id", "customer_id"],
            ["tickets.id", "tickets.customer_id"],
            name=op.f("fk_customer_messages_ticket_id_customer_id_tickets"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_messages")),
    )
    for column in ("run_id", "ticket_id", "customer_id"):
        op.create_index(
            op.f(f"ix_customer_messages_{column}"), "customer_messages", [column], unique=False
        )

    # --- workflow_runs: one unfinished run per ticket
    op.create_index(
        "uq_workflow_runs_one_unfinished_per_ticket",
        "workflow_runs",
        ["ticket_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('completed', 'escalated', 'failed')"),
    )


def downgrade() -> None:
    op.drop_index("uq_workflow_runs_one_unfinished_per_ticket", table_name="workflow_runs")

    for column in ("customer_id", "ticket_id", "run_id"):
        op.drop_index(op.f(f"ix_customer_messages_{column}"), table_name="customer_messages")
    op.drop_table("customer_messages")

    op.drop_index("uq_jobs_one_open_expiry_sweep", table_name="jobs")
    op.drop_constraint(op.f("ck_jobs_run_required"), "jobs", type_="check")
    op.drop_constraint(op.f("ck_jobs_max_attempts_positive"), "jobs", type_="check")
    op.execute("DELETE FROM jobs WHERE run_id IS NULL")  # sweeps cannot exist without run_id
    op.alter_column("jobs", "run_id", nullable=False)
    op.add_column("jobs", sa.Column("last_error", sa.Text()))
    op.add_column("jobs", sa.Column("locked_at", sa.DateTime(timezone=True)))
    op.drop_column("jobs", "last_error_code")
    op.drop_column("jobs", "lease_expires_at")
    op.drop_column("jobs", "max_attempts")
    op.drop_column("jobs", "payload")
    op.drop_constraint(op.f("ck_jobs_job_type"), "jobs", type_="check")
    op.drop_column("jobs", "job_type")

    op.drop_constraint(op.f("ck_jobs_status"), "jobs", type_="check")
    op.execute("UPDATE jobs SET status = 'failed' WHERE status = 'dead'")
    op.create_check_constraint(op.f("ck_jobs_status"), "jobs", f"status IN ({_in(OLD_STATUSES)})")
