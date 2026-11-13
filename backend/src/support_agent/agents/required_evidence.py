"""Required evidence: code fetches the records the checks depend on, if the investigator did not.

The conflict check (order vs carrier delivery time) and the shipping-delay facts both need
`get_order` and `get_shipment`. Whether the investigating model chose to call them must not
decide whether those checks can run. So, after the investigator and before validation:

- `get_order` is always required. It is needed to see whether the order was delivered.
- `get_shipment` is required for order_status and damaged_item cases, and for any order that
  was delivered.

Code runs the missing calls itself through the same tool registry and context (so ownership
checks and the customer id still apply), records them in the trace and results like any
other call, and adds one plain fact per fetched record, written by code from the structured
output, so the resolution agent can cite them. A tool is fetched again only if it has no
successful call yet; a failed fetch is recorded and not retried a second time.

The model is not involved. Nothing here is stored or logged beyond the content-free trace.
"""

import time
from dataclasses import replace
from datetime import datetime

from pydantic import BaseModel

from support_agent.agents.investigator import Evidence, Fact, InvestigationOutcome
from support_agent.domain.enums import OrderStatus, TicketCategory
from support_agent.domain.shipping import ShippingDelayFacts, compute_delay_facts
from support_agent.integrations.shipping import Shipment
from support_agent.llm.tool_loop import OUTCOME_OK, ToolCallRecord, ToolResult
from support_agent.schemas.orders import OrderResponse
from support_agent.tools.base import ToolContext, ToolError, ToolRegistry

CALL_ID_PREFIX = "required_"  # code-made call ids, easy to tell from the provider's

SHIPMENT_REQUIRED_CATEGORIES = frozenset({TicketCategory.ORDER_STATUS, TicketCategory.DAMAGED_ITEM})


def latest_output[T: BaseModel](results: list[ToolResult], model: type[T]) -> T | None:
    """The newest successful tool output of this type, or None."""
    for result in reversed(results):
        if isinstance(result.output, model):
            return result.output
    return None


def _order_fact(order: OrderResponse) -> str:
    delivered = "not delivered" if order.delivered_at is None else f"delivered {order.delivered_at}"
    return f"The order record says: status {order.status.value}, {delivered}."


def _shipment_fact(shipment: Shipment) -> str:
    delivered = "not delivered" if shipment.delivered_at is None else shipment.delivered_at
    return (
        f"The carrier ({shipment.carrier}) reports status {shipment.status}, expected delivery "
        f"{shipment.expected_delivery}, delivery: {delivered}."
    )


async def _fetch(
    registry: ToolRegistry, ctx: ToolContext, tool: str, order_number: str
) -> tuple[ToolCallRecord, ToolResult | None]:
    started = time.perf_counter()
    args = {"order_number": order_number}
    output = await registry.execute(tool, ctx, args)
    latency_ms = round((time.perf_counter() - started) * 1000)
    call_id = f"{CALL_ID_PREFIX}{tool}"
    if isinstance(output, ToolError):
        return ToolCallRecord(
            call_id=call_id, tool=tool, outcome=output.error_code, latency_ms=latency_ms
        ), None
    record = ToolCallRecord(
        call_id=call_id,
        tool=tool,
        outcome=OUTCOME_OK,
        latency_ms=latency_ms,
        order_number=order_number,
    )
    return record, ToolResult(call_id, tool, args, output)


async def ensure_required_evidence(
    registry: ToolRegistry,
    ctx: ToolContext,
    investigation: InvestigationOutcome,
    *,
    category: TicketCategory,
    order_number: str,
) -> InvestigationOutcome:
    """The investigation, plus any required lookups the investigator skipped."""
    if investigation.evidence is None:
        return investigation  # a failed investigation escalates before this step matters

    trace = list(investigation.trace)
    results = list(investigation.results)
    facts: list[Fact] = []

    async def ensure(tool: str, output_type: type[BaseModel]) -> None:
        if latest_output(results, output_type) is not None:
            return
        record, result = await _fetch(registry, ctx, tool, order_number)
        trace.append(record)
        if result is not None:
            results.append(result)
            facts.append(Fact(statement=describe_output(result.output), call_id=record.call_id))

    def describe_output(output: BaseModel) -> str:
        if isinstance(output, OrderResponse):
            return _order_fact(output)
        if isinstance(output, Shipment):
            return _shipment_fact(output)
        raise TypeError(f"no description for {type(output).__name__}")

    await ensure("get_order", OrderResponse)
    order = latest_output(results, OrderResponse)
    delivered = order is not None and (
        order.delivered_at is not None or order.status == OrderStatus.DELIVERED
    )
    if category in SHIPMENT_REQUIRED_CATEGORIES or delivered:
        await ensure("get_shipment", Shipment)

    evidence: Evidence = investigation.evidence.model_copy(
        update={"facts": [*investigation.evidence.facts, *facts]}
    )
    return replace(investigation, trace=trace, results=results, evidence=evidence)


def build_delay_facts(
    results: list[ToolResult], now: datetime, threshold_days: int | None
) -> ShippingDelayFacts | None:
    """Delay facts from the carrier's record; None if there is no shipment to compute from."""
    shipment = latest_output(results, Shipment)
    if shipment is None:
        return None
    return compute_delay_facts(
        expected_delivery=shipment.expected_delivery,
        delivered_at=shipment.delivered_at,
        carrier_status=shipment.status,
        now=now,
        threshold_days=threshold_days,
    )
