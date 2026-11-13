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


class ResolutionAction(StrEnum):
    """What the resolution agent may propose (PRD "Agent specifications").
    Proposing a refund is not executing one: only a workflow code node moves money."""

    REFUND = "refund"
    EXPLAIN_STATUS = "explain_status"
    REQUEST_INFO = "request_info"
    ESCALATE = "escalate"


class RunMode(StrEnum):
    """Multi-agent is the real system; baseline is the single-agent comparison (FR-23).
    Manual runs are started by a reviewer through the API, with no LLM involved."""

    MULTI_AGENT = "multi_agent"
    BASELINE = "baseline"
    MANUAL = "manual"


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
    """Background job queue (worker/queue.py).

    A retry puts a job back to QUEUED with a later available_at. A job that has used all its
    attempts is DEAD: it is never tried again, and its run is escalated to a person.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD = "dead"


class JobType(StrEnum):
    """What a background job does (worker/handlers.py has one handler per type)."""

    START_RUN = "start_run"
    RESUME_AFTER_DECISION = "resume_after_decision"
    RESUME_AFTER_CUSTOMER_REPLY = "resume_after_customer_reply"
    EXPIRE_APPROVALS = "expire_approvals"  # scheduled, no run
    PURGE_EXPIRED_MEMORY = "purge_expired_memory"  # scheduled, no run


# The scheduled sweeps: they work on no run, and at most one of each may be open at a time.
SWEEP_JOB_TYPES = frozenset({JobType.EXPIRE_APPROVALS, JobType.PURGE_EXPIRED_MEMORY})


class MemoryKind(StrEnum):
    """How a run ended, as stored in long-term customer memory (ADR-013)."""

    CASE_COMPLETED = "case_completed"  # answered, or the reviewer rejected the refund
    CASE_ESCALATED = "case_escalated"  # passed to a person
    REFUND_EXECUTED = "refund_executed"  # the payment provider gave a definite answer


class MemoryOutcome(StrEnum):
    """The verified outcome of a past run. Fixed codes only: memory holds no free text."""

    STATUS_EXPLAINED = "status_explained"  # the validated reply explained the order's status
    REFUND_REJECTED = "refund_rejected"  # a reviewer rejected the proposed refund
    REFUND_SUBMITTED = "refund_submitted"  # the provider accepted the refund
    REFUND_PROCESSING = "refund_processing"  # the provider is processing it
    REFUND_SUCCEEDED = "refund_succeeded"  # the provider confirmed the money was paid out
    REFUND_FAILED = "refund_failed"  # the provider confirmed the refund failed
    ESCALATED = "escalated"  # a person took over


class StaffRole(StrEnum):
    """Roles stored on staff_users. Customers are not staff, so they are not listed."""

    REVIEWER = "reviewer"
    ADMIN = "admin"


class Role(StrEnum):
    """Every role a logged-in user can have (the `role` claim in the JWT)."""

    CUSTOMER = "customer"
    REVIEWER = "reviewer"
    ADMIN = "admin"
