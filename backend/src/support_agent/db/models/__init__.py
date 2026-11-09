"""All database models.

Importing this package registers every table on `Base.metadata`, which is what
Alembic compares against the database when generating migrations.
"""

from support_agent.db.models.audit import AuditEvent
from support_agent.db.models.customers import Customer
from support_agent.db.models.jobs import Job
from support_agent.db.models.orders import Order, OrderItem, Return
from support_agent.db.models.policies import PolicyChunk, PolicyDocument
from support_agent.db.models.proposals import ActionProposal, Approval
from support_agent.db.models.refunds import RefundOperation
from support_agent.db.models.support import CustomerMemory, Ticket, WorkflowRun

__all__ = [
    "ActionProposal",
    "Approval",
    "AuditEvent",
    "Customer",
    "CustomerMemory",
    "Job",
    "Order",
    "OrderItem",
    "PolicyChunk",
    "PolicyDocument",
    "RefundOperation",
    "Return",
    "Ticket",
    "WorkflowRun",
]
