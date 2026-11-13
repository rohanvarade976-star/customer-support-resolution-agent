"""What the workflow nodes are given. Nodes never build a client, a session or a service.

Everything that touches the database or money is passed in as a function, so the graph has
no SQL and unit tests can pass fakes:

- `branch_sessions`: a fresh short-lived database session for each parallel branch. One
  AsyncSession must not be used by two tasks at once, so the investigator and the policy
  agent each open their own. Everything after the join uses the main session in
  `tool_context`.
- `load_case_facts`: eligibility and the shipping threshold, computed by code.
- `load_memory`: the customer's recent long-term memory, rendered by code from structured
  fields (services/memory.py). Background for the agents only: nothing that decides
  eligibility, amounts or validation is given it.
- `propose_refund`: store the refund proposal and its pending approval (services/proposals).
- `load_approval_decision`: the reviewer's decision as stored in the database. After a pause
  for approval, this (not the value the run was resumed with) decides what happens next.
- `execute_refund`: services/refund_execution, which revalidates the approval, reserves the
  amount and calls the payment provider once. Only the `execute_refund` node calls it.
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.domain.eligibility import EligibilityDecision
from support_agent.domain.enums import ApprovalDecision, RefundOperationStatus, RunMode
from support_agent.llm.client import LLMClient
from support_agent.tools.base import ToolContext, ToolRegistry
from support_agent.workflow.nodes.classify import OwnsOrder

BranchSessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class CaseFacts:
    """Facts code computes from the database for one order."""

    eligibility: EligibilityDecision
    # The shipping policy's investigation threshold in force for the order; None: no policy.
    investigation_threshold_days: int | None


# (customer_id, order_number, now) -> facts. Raises AppError if the order is not found.
LoadCaseFacts = Callable[[UUID, str, datetime], Awaitable[CaseFacts]]
# customer_id -> the customer's recent memory as plain lines ("" when there is none).
LoadMemory = Callable[[UUID], Awaitable[str]]
# (order_number, amount_minor) -> proposal id. Raises AppError if code refuses the proposal.
ProposeRefund = Callable[[str, int], Awaitable[UUID]]
# proposal id -> the stored approval's decision (None: no approval row).
LoadApprovalDecision = Callable[[UUID], Awaitable[ApprovalDecision | None]]


@dataclass(frozen=True)
class RefundResult:
    status: RefundOperationStatus  # what the payment provider confirmed (or did not)
    message: str  # what we may tell the customer: the verified outcome only


# proposal id -> result. Raises AppError if revalidation refuses the approved refund.
ExecuteRefund = Callable[[UUID], Awaitable[RefundResult]]


@dataclass(frozen=True)
class WorkflowDeps:
    llm: LLMClient
    owns_order: OwnsOrder
    # Below this confidence the run asks the customer instead of guessing.
    confidence_threshold: float
    registry: ToolRegistry  # all read tools; each agent cuts it down to its allowlist
    tool_context: ToolContext  # main session, services and clock; branches swap the session
    max_tool_calls: int
    branch_sessions: BranchSessions
    load_case_facts: LoadCaseFacts
    load_memory: LoadMemory
    propose_refund: ProposeRefund
    load_approval_decision: LoadApprovalDecision
    execute_refund: ExecuteRefund
    max_clarification_rounds: int
    mode: RunMode = RunMode.MULTI_AGENT
