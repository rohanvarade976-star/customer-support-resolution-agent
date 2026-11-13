"""What the workflow nodes are given. Nodes never build a client, a session or a service.

Everything that touches the database or money is passed in as a function, so the graph has
no SQL and unit tests can pass fakes:

- `branch_sessions`: a fresh short-lived database session for each parallel branch. One
  AsyncSession must not be used by two tasks at once, so the investigator and the policy
  agent each open their own. Everything after the join uses the main session in
  `tool_context`.
- `load_case_facts`: eligibility and the shipping threshold, computed by code.
- `propose_refund`: store the refund proposal and its pending approval (services/proposals).
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.domain.eligibility import EligibilityDecision
from support_agent.domain.enums import RunMode
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
# (order_number, amount_minor) -> proposal id. Raises AppError if code refuses the proposal.
ProposeRefund = Callable[[str, int], Awaitable[UUID]]


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
    propose_refund: ProposeRefund
    mode: RunMode = RunMode.MULTI_AGENT
