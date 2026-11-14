"""A fake "world" for testing the workflow without a database, a network or a real model.

`FakeWorld` holds canned typed records, a registry whose tools return them, and the
functions the workflow is given for the database (`load_case_facts`), for storing a refund
proposal (`propose_refund`), and for what follows a reviewer's decision
(`load_approval_decision`, `execute_refund`), plus the customer's long-term memory
(`load_memory`). Everything it was asked to do is kept for
assertions.

`run_workflow` runs a graph to its first pause (or the end) with an in-memory checkpointer.

The tools yield to the event loop (`asyncio.sleep(0)`), as real database and network calls
do. Without that, the two parallel branches would run one after the other and a test could
not tell parallel from sequential. `sessions.max_open` is the most branch sessions that were
open at once: 2 means the investigator and the policy agent really overlapped, each with
its own session.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.agents.fake import (
    critique_reply,
    draft_reply,
    policy_run,
    simple_investigation,
    supervisor_reply,
)
from support_agent.core.errors import AppError
from support_agent.domain.eligibility import EligibilityDecision
from support_agent.domain.enums import (
    ApprovalDecision,
    OrderStatus,
    RefundOperationStatus,
    RunMode,
)
from support_agent.integrations.shipping import Shipment
from support_agent.llm.client import ChatProvider, LLMClient, RetrySettings, RoutedLLMClient
from support_agent.llm.fake import ScriptStep, tool_reply
from support_agent.schemas.orders import OrderResponse
from support_agent.schemas.policies import PolicyPassage, PolicySearchResult
from support_agent.tools.base import Tool, ToolContext, ToolFailureError, ToolRegistry
from support_agent.tools.fake import FakeRecord, fake_tool_context
from support_agent.tools.policy_tools import SearchPolicyInput
from support_agent.tools.read_tools import OrderInput
from support_agent.workflow.checkpointer import checkpoint_serializer
from support_agent.workflow.deps import CaseFacts, RefundResult, WorkflowDeps
from support_agent.workflow.graph import Checkpointer, start_workflow
from support_agent.workflow.nodes.classify import OwnsOrder
from support_agent.workflow.state import WorkflowState

CITATION = "refund_policy_v2:section_3"
DELIVERED_AT = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
PRIYA_AMOUNT = 249900


def scripted_client(provider: ChatProvider) -> LLMClient:
    """The real client (retries, repair, usage counting) on top of a scripted provider."""
    return RoutedLLMClient(
        [provider],
        RetrySettings(max_attempts=1, backoff_base_seconds=0, backoff_max_seconds=0),
        default_temperature=0.0,
        default_max_tokens=256,
    )


def eligible(amount_minor: int = PRIYA_AMOUNT) -> EligibilityDecision:
    return EligibilityDecision(eligible=True, refundable_amount_minor=amount_minor, reasons=[])


def classified(category: str = "refund", confidence: float = 0.95) -> ScriptStep:
    return tool_reply(
        "classification", {"category": category, "confidence": confidence, "reason": "test"}
    )


def delivered_order(order_number: str = "ORD-1001") -> OrderResponse:
    return OrderResponse(
        order_number=order_number,
        status=OrderStatus.DELIVERED,
        currency="INR",
        paid_amount_minor=PRIYA_AMOUNT,
        placed_at=DELIVERED_AT - timedelta(days=5),
        delivered_at=DELIVERED_AT,
        items=[],
        returns=[],
    )


def delivered_shipment(order_number: str = "ORD-1001") -> Shipment:
    return Shipment(
        order_reference=order_number,
        status="delivered",
        carrier="TestCarrier",
        tracking_number="TRK1",
        shipped_at=DELIVERED_AT - timedelta(days=4),
        expected_delivery=DELIVERED_AT.date(),
        delivered_at=DELIVERED_AT,
        events=[],
    )


class BranchSessions:
    """Fake branch sessions that count how many were open at the same moment."""

    def __init__(self) -> None:
        self.opened = 0
        self.open_now = 0
        self.max_open = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[AsyncSession]:
        self.opened += 1
        self.open_now += 1
        self.max_open = max(self.max_open, self.open_now)
        try:
            yield MagicMock(spec=AsyncSession)
        finally:
            self.open_now -= 1


class FakeWorld:
    def __init__(
        self,
        *,
        decision: EligibilityDecision | None = None,
        order: OrderResponse | None = None,
        shipment: Shipment | None = None,
        citation: str = CITATION,
    ) -> None:
        self.decision = decision or eligible()
        self.order = order or delivered_order()
        self.shipment = shipment or delivered_shipment()
        self.citation = citation
        self.calls: list[tuple[str, UUID]] = []  # (tool, the customer it ran as)
        self.fail_counts: dict[str, int] = {}  # tool -> how many of its next calls fail
        self.refuse_proposal_with: str | None = None
        self.proposals: list[tuple[str, int]] = []
        self.proposal_ids: list[UUID] = []
        # What the "database" says the reviewer decided. Tests set it before resuming.
        self.approval_decision: ApprovalDecision | None = ApprovalDecision.PENDING
        self.refuse_refund_with: str | None = None
        self.executed: list[UUID] = []  # proposal ids the refund executor was called for
        # The customer's rendered long-term memory ("" = none), and whose memory was asked for.
        self.memory = ""
        self.memory_loaded_for: list[UUID] = []
        # The customer's own order numbers, which a fake reply may name.
        self.owned_orders = ["ORD-1001", "ORD-1042"]
        self.sessions = BranchSessions()
        self.registry = self._build_registry()

    # -- what the workflow is given for the database --

    async def load_case_facts(
        self, customer_id: UUID, order_number: str, now: datetime
    ) -> CaseFacts:
        return CaseFacts(self.decision, investigation_threshold_days=None)

    async def load_memory(self, customer_id: UUID) -> str:
        self.memory_loaded_for.append(customer_id)
        return self.memory

    async def load_owned_order_numbers(self, customer_id: UUID) -> list[str]:
        return list(self.owned_orders)

    async def propose_refund(self, order_number: str, amount_minor: int) -> UUID:
        if self.refuse_proposal_with is not None:
            raise AppError(409, self.refuse_proposal_with, "Refused.")
        self.proposals.append((order_number, amount_minor))
        self.proposal_ids.append(uuid4())
        return self.proposal_ids[-1]

    async def load_approval_decision(self, proposal_id: UUID) -> ApprovalDecision | None:
        return self.approval_decision

    async def execute_refund(self, proposal_id: UUID) -> RefundResult:
        if self.refuse_refund_with is not None:
            raise AppError(409, self.refuse_refund_with, "Refused.")
        self.executed.append(proposal_id)
        return RefundResult(RefundOperationStatus.SUBMITTED, "The payment service accepted it.")

    # -- tools --

    def _build_registry(self) -> ToolRegistry:
        registry = ToolRegistry()

        def maybe_fail(tool: str) -> None:
            if self.fail_counts.get(tool, 0) > 0:
                self.fail_counts[tool] -= 1
                raise ToolFailureError("UPSTREAM_DOWN", "The record is unavailable.")

        async def get_order(ctx: ToolContext, args: OrderInput) -> OrderResponse:
            await self._seen("get_order", ctx)
            maybe_fail("get_order")
            return self.order

        async def get_shipment(ctx: ToolContext, args: OrderInput) -> Shipment:
            await self._seen("get_shipment", ctx)
            maybe_fail("get_shipment")
            return self.shipment

        async def other(ctx: ToolContext, args: OrderInput) -> FakeRecord:
            await self._seen("other", ctx)
            return FakeRecord(tool="other", order_number=args.order_number, detail="canned")

        async def search_policy(ctx: ToolContext, args: SearchPolicyInput) -> PolicySearchResult:
            await self._seen("search_policy", ctx)
            return PolicySearchResult(
                as_of_date=date(2026, 11, 1),
                order_number=args.order_number,
                versions_considered=["refund_policy v2"],
                no_relevant_evidence=False,
                passages=[
                    PolicyPassage(
                        citation=self.citation,
                        policy_key="refund_policy",
                        policy_title="Refund Policy",
                        version="v2",
                        section="section_3",
                        section_title="Return window",
                        effective_from=date(2026, 7, 1),
                        effective_to=None,
                        content="Returns are accepted within 30 days.",
                        vector_similarity=0.8,
                        keyword_rank=1.0,
                        rrf_score=0.03,
                    )
                ],
            )

        registry.register(Tool("get_order", "Fake.", OrderInput, OrderResponse, get_order))
        registry.register(Tool("get_shipment", "Fake.", OrderInput, Shipment, get_shipment))
        for name in (
            "get_return_status",
            "get_payment_status",
            "get_refundable_balance",
            "check_refund_eligibility",
        ):
            registry.register(Tool(name, "Fake.", OrderInput, FakeRecord, other))
        registry.register(
            Tool("search_policy", "Fake.", SearchPolicyInput, PolicySearchResult, search_policy)
        )
        return registry

    async def _seen(self, tool: str, ctx: ToolContext) -> None:
        self.calls.append((tool, ctx.customer_id))
        await asyncio.sleep(0)  # a real call waits on I/O: let the other branch run

    def called(self, tool: str) -> int:
        return sum(1 for name, _ in self.calls if name == tool)


async def owns_everything(customer_id: UUID, order_number: str) -> bool:
    return True


def fake_deps(
    llm: LLMClient,
    world: FakeWorld,
    *,
    mode: RunMode = RunMode.MULTI_AGENT,
    confidence_threshold: float = 0.7,
    max_tool_calls: int = 3,
    customer_id: UUID | None = None,
    owns_order: OwnsOrder = owns_everything,
    max_clarification_rounds: int = 2,
) -> WorkflowDeps:
    return WorkflowDeps(
        llm=llm,
        owns_order=owns_order,
        confidence_threshold=confidence_threshold,
        registry=world.registry,
        # A context for some other customer: nodes must use the state's customer instead.
        tool_context=fake_tool_context(customer_id),
        max_tool_calls=max_tool_calls,
        branch_sessions=world.sessions,
        load_case_facts=world.load_case_facts,
        load_memory=world.load_memory,
        load_owned_order_numbers=world.load_owned_order_numbers,
        propose_refund=world.propose_refund,
        load_approval_decision=world.load_approval_decision,
        execute_refund=world.execute_refund,
        max_clarification_rounds=max_clarification_rounds,
        mode=mode,
    )


def memory_checkpointer(deps: WorkflowDeps) -> InMemorySaver:
    """An in-memory checkpointer with the same strict type allowlist as the real one."""
    return InMemorySaver(serde=checkpoint_serializer(deps.registry))


def thread_for(state: WorkflowState) -> str:
    return f"run-{state['run_id']}"


async def run_workflow(
    deps: WorkflowDeps, state: WorkflowState, checkpointer: Checkpointer | None = None
) -> WorkflowState:
    """Run a new graph to its first pause (or the end) and return the state there."""
    saver = checkpointer if checkpointer is not None else memory_checkpointer(deps)
    return (await start_workflow(deps, saver, thread_for(state), state)).state


def refund_scripts(
    *, citation: str = CITATION, draft: ScriptStep | None = None
) -> dict[str, list[ScriptStep]]:
    """Scripts for a refund that goes all the way: both specialists, an approving critic."""
    return {
        "classifier": [classified("refund")],
        "supervisor": [supervisor_reply()],
        "investigator": simple_investigation("ORD-1001"),
        "policy": policy_run(citation),
        "resolution": [draft or draft_reply("refund", citations=[citation])],
        "critic": [critique_reply("approve")],
    }
