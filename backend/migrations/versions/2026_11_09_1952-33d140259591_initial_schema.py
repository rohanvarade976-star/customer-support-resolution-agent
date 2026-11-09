"""initial schema

Revision ID: 33d140259591
Revises:
Create Date: 2026-11-09 19:52:07.749639

"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "33d140259591"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector must exist before policy_chunks.embedding (a VECTOR column) is created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("external_auth_subject", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint("email", name=op.f("uq_customers_email")),
        sa.UniqueConstraint(
            "external_auth_subject", name=op.f("uq_customers_external_auth_subject")
        ),
    )
    op.create_table(
        "policy_documents",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("policy_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("source_uri", sa.String(length=500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name=op.f("ck_policy_documents_valid_effective_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_documents")),
        sa.UniqueConstraint(
            "policy_key", "version", name=op.f("uq_policy_documents_policy_key_version")
        ),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("order_number", sa.String(length=20), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "placed",
                "shipped",
                "delivered",
                "cancelled",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("paid_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "placed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_orders_currency_iso_code")),
        sa.CheckConstraint(
            "order_number ~ '^ORD-[0-9]+$'", name=op.f("ck_orders_order_number_format")
        ),
        sa.CheckConstraint(
            "paid_amount_minor >= 0", name=op.f("ck_orders_paid_amount_minor_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_orders_customer_id_customers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("id", "customer_id", name=op.f("uq_orders_id_customer_id")),
        sa.UniqueConstraint("order_number", name=op.f("uq_orders_order_number")),
    )
    op.create_index(op.f("ix_orders_customer_id"), "orders", ["customer_id"], unique=False)
    op.create_table(
        "policy_chunks",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("section", sa.String(length=100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["policy_documents.id"],
            name=op.f("fk_policy_chunks_document_id_policy_documents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_chunks")),
    )
    op.create_index(
        op.f("ix_policy_chunks_document_id"), "policy_chunks", ["document_id"], unique=False
    )
    op.create_table(
        "order_items",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("product_name", sa.String(length=200), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("paid_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "paid_amount_minor >= 0", name=op.f("ck_order_items_paid_amount_minor_non_negative")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_order_items_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_order_items_order_id_orders")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
    )
    op.create_index(op.f("ix_order_items_order_id"), "order_items", ["order_id"], unique=False)
    op.create_table(
        "returns",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "requested",
                "in_transit",
                "received",
                "cancelled",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "inspection_status",
            sa.Enum(
                "pending",
                "passed",
                "failed",
                name="inspection_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "inspection_status = 'pending' OR received_at IS NOT NULL",
            name=op.f("ck_returns_inspected_only_after_received"),
        ),
        sa.CheckConstraint(
            "status <> 'received' OR received_at IS NOT NULL",
            name=op.f("ck_returns_received_has_timestamp"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_returns_order_id_orders")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_returns")),
    )
    op.create_index(op.f("ix_returns_order_id"), "returns", ["order_id"], unique=False)
    op.create_table(
        "tickets",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column(
            "category",
            sa.Enum(
                "order_status",
                "refund",
                "damaged_item",
                name="category",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "in_progress",
                "awaiting_customer",
                "resolved",
                "escalated",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="open",
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_tickets_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "customer_id"],
            ["orders.id", "orders.customer_id"],
            name=op.f("fk_tickets_order_id_customer_id_orders"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
        sa.UniqueConstraint("id", "customer_id", name=op.f("uq_tickets_id_customer_id")),
    )
    op.create_index(op.f("ix_tickets_customer_id"), "tickets", ["customer_id"], unique=False)
    op.create_index(op.f("ix_tickets_order_id"), "tickets", ["order_id"], unique=False)
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
    op.create_index(
        op.f("ix_customer_memory_customer_id"), "customer_memory", ["customer_id"], unique=False
    )
    op.create_index(
        op.f("ix_customer_memory_ticket_id"), "customer_memory", ["ticket_id"], unique=False
    )
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column(
            "mode",
            sa.Enum(
                "multi_agent",
                "baseline",
                name="mode",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="multi_agent",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "created",
                "investigating",
                "awaiting_customer",
                "awaiting_approval",
                "executing",
                "completed",
                "escalated",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="created",
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_workflow_runs_completed_after_started"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name=op.f("fk_workflow_runs_ticket_id_tickets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_runs")),
        sa.UniqueConstraint("thread_id", name=op.f("uq_workflow_runs_thread_id")),
    )
    op.create_index(
        op.f("ix_workflow_runs_ticket_id"), "workflow_runs", ["ticket_id"], unique=False
    )
    op.create_table(
        "action_proposals",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "action_type",
            sa.Enum(
                "request_refund_approval",
                name="action_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending_approval",
                "approved",
                "rejected",
                "expired",
                "invalidated",
                "executed",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending_approval",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_action_proposals_payload_hash_sha256")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], name=op.f("fk_action_proposals_run_id_workflow_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_proposals")),
        sa.UniqueConstraint("id", "payload_hash", name=op.f("uq_action_proposals_id_payload_hash")),
    )
    op.create_index(
        op.f("ix_action_proposals_run_id"), "action_proposals", ["run_id"], unique=False
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "redacted_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], name=op.f("fk_audit_events_run_id_workflow_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(op.f("ix_audit_events_run_id"), "audit_events", ["run_id"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_jobs_attempts_non_negative")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], name=op.f("fk_jobs_run_id_workflow_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(op.f("ix_jobs_run_id"), "jobs", ["run_id"], unique=False)
    op.create_index("ix_jobs_status_available_at", "jobs", ["status", "available_at"], unique=False)
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("reviewer_id", sa.String(length=255), nullable=True),
        sa.Column(
            "decision",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                "expired",
                name="decision",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision NOT IN ('approved', 'rejected')"
            " OR (reviewer_id IS NOT NULL AND decided_at IS NOT NULL)",
            name=op.f("ck_approvals_decision_has_reviewer"),
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_approvals_payload_hash_sha256")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_approvals_expires_after_created")
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id", "payload_hash"],
            ["action_proposals.id", "action_proposals.payload_hash"],
            name=op.f("fk_approvals_proposal_id_payload_hash_action_proposals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    op.create_index(op.f("ix_approvals_proposal_id"), "approvals", ["proposal_id"], unique=False)
    op.create_table(
        "refund_operations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("provider_reference", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "reserved",
                "submitted",
                "processing",
                "succeeded",
                "failed",
                "pending_reconciliation",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="reserved",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_minor > 0", name=op.f("ck_refund_operations_amount_minor_positive")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_refund_operations_order_id_orders")
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["action_proposals.id"],
            name=op.f("fk_refund_operations_proposal_id_action_proposals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refund_operations")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_refund_operations_idempotency_key")),
    )
    op.create_index(
        op.f("ix_refund_operations_order_id"), "refund_operations", ["order_id"], unique=False
    )
    op.create_index(
        op.f("ix_refund_operations_proposal_id"), "refund_operations", ["proposal_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_refund_operations_proposal_id"), table_name="refund_operations")
    op.drop_index(op.f("ix_refund_operations_order_id"), table_name="refund_operations")
    op.drop_table("refund_operations")
    op.drop_index(op.f("ix_approvals_proposal_id"), table_name="approvals")
    op.drop_table("approvals")
    op.drop_index("ix_jobs_status_available_at", table_name="jobs")
    op.drop_index(op.f("ix_jobs_run_id"), table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(op.f("ix_audit_events_run_id"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index(op.f("ix_action_proposals_run_id"), table_name="action_proposals")
    op.drop_table("action_proposals")
    op.drop_index(op.f("ix_workflow_runs_ticket_id"), table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_index(op.f("ix_customer_memory_ticket_id"), table_name="customer_memory")
    op.drop_index(op.f("ix_customer_memory_customer_id"), table_name="customer_memory")
    op.drop_table("customer_memory")
    op.drop_index(op.f("ix_tickets_order_id"), table_name="tickets")
    op.drop_index(op.f("ix_tickets_customer_id"), table_name="tickets")
    op.drop_table("tickets")
    op.drop_index(op.f("ix_returns_order_id"), table_name="returns")
    op.drop_table("returns")
    op.drop_index(op.f("ix_order_items_order_id"), table_name="order_items")
    op.drop_table("order_items")
    op.drop_index(op.f("ix_policy_chunks_document_id"), table_name="policy_chunks")
    op.drop_table("policy_chunks")
    op.drop_index(op.f("ix_orders_customer_id"), table_name="orders")
    op.drop_table("orders")
    op.drop_table("policy_documents")
    op.drop_table("customers")
    # The vector extension is left installed on purpose: other objects may use it,
    # and keeping it is harmless.
