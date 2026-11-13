"""agents/required_evidence.py: code fetches get_order / get_shipment if the investigator did not.

Fake tools only. The handlers return canned typed records and count how often they ran.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from support_agent.agents.investigator import Evidence, Fact, InvestigationOutcome
from support_agent.agents.policy import PolicyFindings
from support_agent.agents.required_evidence import (
    build_delay_facts,
    ensure_required_evidence,
)
from support_agent.agents.resolution import ResolutionInputs
from support_agent.agents.resolve import build_context
from support_agent.domain.eligibility import EligibilityDecision
from support_agent.domain.enums import OrderStatus, TicketCategory
from support_agent.domain.validation import delivery_dates_conflict
from support_agent.integrations.shipping import Shipment
from support_agent.llm.tool_loop import OUTCOME_OK, ToolCallRecord, ToolResult
from support_agent.schemas.orders import OrderResponse
from support_agent.tools.base import Tool, ToolContext, ToolFailureError, ToolRegistry
from support_agent.tools.fake import fake_tool_context
from support_agent.tools.read_tools import OrderInput

ORDER = "ORD-1002"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def order_record(*, delivered_at: datetime | None) -> OrderResponse:
    return OrderResponse(
        order_number=ORDER,
        status=OrderStatus.DELIVERED if delivered_at else OrderStatus.SHIPPED,
        currency="INR",
        paid_amount_minor=100000,
        placed_at=NOW - timedelta(days=20),
        delivered_at=delivered_at,
        items=[],
        returns=[],
    )


def shipment_record(*, delivered_at: datetime | None, days_late: int = 3) -> Shipment:
    return Shipment(
        order_reference=ORDER,
        status="delivered" if delivered_at else "in_transit",
        carrier="TestCarrier",
        tracking_number="TRK1",
        shipped_at=NOW - timedelta(days=15),
        expected_delivery=(NOW - timedelta(days=days_late)).date(),
        delivered_at=delivered_at,
        events=[],
    )


class FakeLookups:
    """A registry with get_order and get_shipment that count their calls."""

    def __init__(
        self,
        order: OrderResponse,
        shipment: Shipment | None,
        *,
        customer_ids: list[object] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._customer_ids = customer_ids
        self.registry = ToolRegistry()

        async def get_order(ctx: ToolContext, args: OrderInput) -> OrderResponse:
            self._seen("get_order", ctx)
            return order

        async def get_shipment(ctx: ToolContext, args: OrderInput) -> Shipment:
            self._seen("get_shipment", ctx)
            if shipment is None:
                raise ToolFailureError("SHIPMENT_NOT_FOUND", "No shipment exists for this order.")
            return shipment

        self.registry.register(Tool("get_order", "x", OrderInput, OrderResponse, get_order))
        self.registry.register(Tool("get_shipment", "x", OrderInput, Shipment, get_shipment))

    def _seen(self, tool: str, ctx: ToolContext) -> None:
        self.calls.append(tool)
        if self._customer_ids is not None:
            self._customer_ids.append(ctx.customer_id)


def investigation(
    *, trace: list[ToolCallRecord] | None = None, results: list[ToolResult] | None = None
) -> InvestigationOutcome:
    return InvestigationOutcome(
        plan=None,
        trace=trace or [],
        evidence=Evidence(
            facts=[Fact(statement="Something.", call_id="call_x")],
            dropped_facts=[],
            missing_information=[],
            plan_changes=[],
        ),
        failure=None,
        results=results or [],
    )


def trace_ids(outcome: InvestigationOutcome) -> list[str]:
    return [record.call_id for record in outcome.trace]


async def ensure(
    lookups: FakeLookups,
    outcome: InvestigationOutcome,
    category: TicketCategory,
    ctx: ToolContext | None = None,
) -> InvestigationOutcome:
    return await ensure_required_evidence(
        lookups.registry,
        ctx or fake_tool_context(),
        outcome,
        category=category,
        order_number=ORDER,
    )


# ---------------------------------------------------------------- which categories fetch what


async def test_an_order_status_case_gets_both_records_when_the_investigator_skipped_them() -> None:
    lookups = FakeLookups(order_record(delivered_at=None), shipment_record(delivered_at=None))

    result = await ensure(lookups, investigation(), TicketCategory.ORDER_STATUS)

    assert lookups.calls == ["get_order", "get_shipment"]
    assert trace_ids(result) == ["required_get_order", "required_get_shipment"]
    assert all(record.outcome == OUTCOME_OK for record in result.trace)
    assert {r.tool for r in result.results} == {"get_order", "get_shipment"}


async def test_a_damaged_item_case_gets_both_records() -> None:
    lookups = FakeLookups(
        order_record(delivered_at=NOW), shipment_record(delivered_at=NOW, days_late=0)
    )

    await ensure(lookups, investigation(), TicketCategory.DAMAGED_ITEM)

    assert lookups.calls == ["get_order", "get_shipment"]


async def test_a_refund_case_for_a_delivered_order_gets_the_shipment() -> None:
    lookups = FakeLookups(
        order_record(delivered_at=NOW), shipment_record(delivered_at=NOW, days_late=0)
    )

    await ensure(lookups, investigation(), TicketCategory.REFUND)

    assert lookups.calls == ["get_order", "get_shipment"]


async def test_a_refund_case_for_an_undelivered_order_does_not_fetch_the_shipment() -> None:
    lookups = FakeLookups(order_record(delivered_at=None), shipment_record(delivered_at=None))

    await ensure(lookups, investigation(), TicketCategory.REFUND)

    assert lookups.calls == ["get_order"]


# ---------------------------------------------------------------- nothing is fetched twice


async def test_records_the_investigator_already_fetched_are_not_fetched_again() -> None:
    order = order_record(delivered_at=None)
    shipment = shipment_record(delivered_at=None)
    done = investigation(
        trace=[
            ToolCallRecord(call_id="call_1", tool="get_order", outcome=OUTCOME_OK, latency_ms=1),
            ToolCallRecord(call_id="call_2", tool="get_shipment", outcome=OUTCOME_OK, latency_ms=1),
        ],
        results=[
            ToolResult("call_1", "get_order", {"order_number": ORDER}, order),
            ToolResult("call_2", "get_shipment", {"order_number": ORDER}, shipment),
        ],
    )
    lookups = FakeLookups(order, shipment)

    result = await ensure(lookups, done, TicketCategory.ORDER_STATUS)

    assert lookups.calls == []
    assert result == done


async def test_a_failed_investigator_lookup_is_fetched_by_code() -> None:
    failed = investigation(
        trace=[
            ToolCallRecord(
                call_id="call_2", tool="get_shipment", outcome="SHIPPING_UNAVAILABLE", latency_ms=1
            )
        ],
        results=[ToolResult("call_1", "get_order", {}, order_record(delivered_at=None))],
    )
    lookups = FakeLookups(order_record(delivered_at=None), shipment_record(delivered_at=None))

    result = await ensure(lookups, failed, TicketCategory.ORDER_STATUS)

    assert lookups.calls == ["get_shipment"]
    assert trace_ids(result) == ["call_2", "required_get_shipment"]


# ---------------------------------------------------------------- what the added calls look like


async def test_fetched_records_become_code_written_facts_that_cite_the_new_call_ids() -> None:
    lookups = FakeLookups(order_record(delivered_at=None), shipment_record(delivered_at=None))

    result = await ensure(lookups, investigation(), TicketCategory.ORDER_STATUS)

    assert result.evidence is not None
    added = {fact.call_id: fact.statement for fact in result.evidence.facts}
    assert "required_get_shipment" in added and "TestCarrier" in added["required_get_shipment"]
    assert "required_get_order" in added
    assert result.evidence.facts[0].call_id == "call_x"  # the investigator's fact is kept


async def test_a_failed_fetch_is_recorded_with_its_error_and_adds_no_fact() -> None:
    lookups = FakeLookups(order_record(delivered_at=None), shipment=None)

    result = await ensure(lookups, investigation(), TicketCategory.ORDER_STATUS)

    shipment_calls = [r for r in result.trace if r.tool == "get_shipment"]
    assert [r.outcome for r in shipment_calls] == ["SHIPMENT_NOT_FOUND"]
    assert lookups.calls.count("get_shipment") == 1  # not retried
    assert result.evidence is not None
    assert all(f.call_id != "required_get_shipment" for f in result.evidence.facts)


async def test_the_fetch_runs_as_the_authenticated_customer() -> None:
    customer_id = uuid4()
    seen: list[object] = []
    lookups = FakeLookups(
        order_record(delivered_at=None), shipment_record(delivered_at=None), customer_ids=seen
    )

    await ensure(
        lookups, investigation(), TicketCategory.ORDER_STATUS, fake_tool_context(customer_id)
    )

    assert seen == [customer_id, customer_id]


async def test_a_failed_investigation_is_returned_unchanged() -> None:
    failed = InvestigationOutcome(None, [], None, None)
    lookups = FakeLookups(order_record(delivered_at=None), shipment_record(delivered_at=None))

    result = await ensure(lookups, failed, TicketCategory.ORDER_STATUS)

    assert result is failed
    assert lookups.calls == []


# ---------------------------------------------------------------- what the checks now see


async def test_the_conflict_check_runs_even_though_the_investigator_never_called_get_shipment() -> (
    None
):
    # Meera: the order says delivered; the carrier says delivered 4 days later.
    delivered = NOW - timedelta(days=1)
    lookups = FakeLookups(
        order_record(delivered_at=delivered),
        shipment_record(delivered_at=delivered + timedelta(days=4), days_late=0),
    )
    result = await ensure(lookups, investigation(), TicketCategory.DAMAGED_ITEM)
    inputs = ResolutionInputs(
        complaint_text="x",
        order_number=ORDER,
        evidence=result.evidence
        or Evidence(facts=[], dropped_facts=[], missing_information=[], plan_changes=[]),
        findings=PolicyFindings(
            rules=[],
            dropped_rules=[],
            policy_versions=[],
            no_relevant_policy=False,
            conflicting_passages=False,
            ungrounded_citations=False,
        ),
        eligibility=EligibilityDecision(eligible=False, refundable_amount_minor=0, reasons=[]),
    )

    ctx = build_context(inputs, result.trace, result.results, timedelta(hours=24))

    assert ctx.delivery_dates is not None
    assert delivery_dates_conflict(ctx.delivery_dates, ctx.delivery_tolerance)


async def test_delay_facts_come_from_the_fetched_shipment() -> None:
    lookups = FakeLookups(
        order_record(delivered_at=None), shipment_record(delivered_at=None, days_late=3)
    )
    result = await ensure(lookups, investigation(), TicketCategory.ORDER_STATUS)

    facts = build_delay_facts(result.results, NOW, threshold_days=7)

    assert facts is not None
    assert facts.days_past_expected == 3
    assert facts.investigation_applies is False


def test_no_shipment_means_no_delay_facts() -> None:
    assert build_delay_facts([], NOW, threshold_days=7) is None
