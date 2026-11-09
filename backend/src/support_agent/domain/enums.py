"""Every status and category value used in the system.

StrEnum members behave like plain strings ("completed" == RunStatus.COMPLETED),
so they serialize cleanly to JSON and to the database. The database stores the
lowercase *value* and a CHECK constraint rejects anything not listed here.

Changing a value here needs a migration, because the CHECK constraints copy these lists.
"""

from enum import StrEnum


class OrderStatus(StrEnum):
    PLACED = "placed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class ReturnStatus(StrEnum):
    REQUESTED = "requested"
    IN_TRANSIT = "in_transit"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class InspectionStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class TicketCategory(StrEnum):
    """The three complaint types the classifier can assign (PRD FR-3)."""

    ORDER_STATUS = "order_status"
    REFUND = "refund"
    DAMAGED_ITEM = "damaged_item"


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    AWAITING_CUSTOMER = "awaiting_customer"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class RunMode(StrEnum):
    """Multi-agent is the real system; baseline is the single-agent comparison (FR-23)."""

    MULTI_AGENT = "multi_agent"
    BASELINE = "baseline"


class RunStatus(StrEnum):
    """Workflow run lifecycle, exactly as in the PRD "Workflow and state model"."""

    CREATED = "created"
    INVESTIGATING = "investigating"
    AWAITING_CUSTOMER = "awaiting_customer"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    FAILED = "failed"


class ActionType(StrEnum):
    REQUEST_REFUND_APPROVAL = "request_refund_approval"


class ProposalStatus(StrEnum):
    """The proposal payload never changes; only this status moves."""

    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"  # revalidation after approval found a material change
    EXECUTED = "executed"


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class RefundOperationStatus(StrEnum):
    """Refund lifecycle from the PRD. Unknown outcomes go to PENDING_RECONCILIATION."""

    RESERVED = "reserved"
    SUBMITTED = "submitted"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PENDING_RECONCILIATION = "pending_reconciliation"


class JobStatus(StrEnum):
    """Background job queue. A retry puts a job back to QUEUED with a later available_at."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
