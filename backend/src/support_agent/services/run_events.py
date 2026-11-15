"""A run's live status for the customer timeline and the reviewer view (FR-22, ADR-019).

`load_snapshot` builds the whole picture once (the polling fallback, and the first thing a
stream sends). `stream_run` then polls the database and sends only what changed.

Each poll opens and closes its own short session. A stream never holds a transaction or a
row lock while it waits (engineering rule 7 in docs/engineering-rules.md), and it checks
ownership again on every poll.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.db.models import AuditEvent, Ticket
from support_agent.domain.enums import OrderStatus, Role
from support_agent.domain.run_events import (
    checked_sources,
    compact_json,
    customer_refund_status,
    is_finished,
    reviewer_details,
    safe_actor,
    sse_comment,
    sse_frame,
    visible_to_customer,
)
from support_agent.integrations.errors import IntegrationError
from support_agent.integrations.shipping import ShippingClient
from support_agent.repositories import audit, orders, refunds
from support_agent.schemas.auth import Principal
from support_agent.schemas.run_events import (
    RunFacts,
    RunSnapshot,
    RunStatusEvent,
    RunStepEvent,
    ShipmentFacts,
)
from support_agent.services import order_status
from support_agent.services.runs import load_visible_run

# Opens a short-lived session: `async with sessions() as session: ...`
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
Monotonic = Callable[[], float]


def _checked(events: list[AuditEvent]) -> list[str]:
    """What kinds of records the run checked, as fixed codes (the customer's "What we checked")."""
    calls = [
        (str(event.redacted_payload.get("tool")), str(event.redacted_payload.get("outcome")))
        for event in events
        if event.event_type == "tool.called"
    ]
    return checked_sources(calls)


def _step(event: AuditEvent, reviewer: bool) -> RunStepEvent:
    return RunStepEvent(
        id=event.id,
        type=event.event_type,
        actor=safe_actor(event.actor),
        at=event.created_at,
        details=reviewer_details(event.redacted_payload) if reviewer else {},
    )


_SHIPMENT_STATUSES = ("in_transit", "delivered")


async def _shipment_facts(
    session: AsyncSession, shipping: ShippingClient, customer_id: UUID, order_number: str
) -> ShipmentFacts | None:
    """The carrier's report, or None if it cannot be had: the panel then simply omits it."""
    try:
        shipment = await order_status.get_shipment(session, shipping, customer_id, order_number)
    except IntegrationError:
        return None
    if shipment.status not in _SHIPMENT_STATUSES:
        return None  # an unknown carrier word is never shown
    return ShipmentFacts(
        status="delivered" if shipment.status == "delivered" else "in_transit",
        expected_delivery=shipment.expected_delivery,
    )


async def _facts(
    session: AsyncSession,
    ticket: Ticket,
    run_id: UUID,
    shipping: ShippingClient | None,
    known_shipment: ShipmentFacts | None,
) -> RunFacts | None:
    """Verified facts about the ticket's order. Looked up with the ticket's owner, so the
    order can only be one that customer owns (rule 3)."""
    if ticket.order_id is None:
        return None
    order = await orders.get_by_id_for_customer(session, ticket.customer_id, ticket.order_id)
    if order is None:
        return None
    shipment = known_shipment
    if shipment is None and shipping is not None and order.status == OrderStatus.SHIPPED:
        shipment = await _shipment_facts(session, shipping, ticket.customer_id, order.order_number)
    latest_return = max(order.returns, key=lambda ret: ret.created_at, default=None)
    refund = await refunds.latest_status_for_run(session, run_id)
    return RunFacts(
        order_number=order.order_number,
        order_status=order.status,
        delivered_at=order.delivered_at,
        return_status=None if latest_return is None else latest_return.status,
        refund_status=None if refund is None else customer_refund_status(refund),
        shipment=shipment,
    )


async def load_snapshot(
    session: AsyncSession,
    principal: Principal,
    run_id: UUID,
    settings: Settings,
    *,
    shipping: ShippingClient | None = None,
    known_shipment: ShipmentFacts | None = None,
) -> RunSnapshot:
    """The run's status, verified reply and steps, as this caller may see them.

    `shipping` is asked about an order in transit; a stream passes the answer it already has
    as `known_shipment`, so one open stream asks the carrier once, not every second.
    """
    run, ticket = await load_visible_run(session, principal, run_id)  # 404 if not theirs
    reviewer = principal.role in (Role.REVIEWER, Role.ADMIN)
    events = await audit.list_for_run(session, run.id)
    steps = [
        _step(event, reviewer)
        for event in events
        if reviewer or visible_to_customer(event.event_type)
    ]
    status = RunStatusEvent(
        run_id=run.id,
        ticket_id=ticket.id,
        status=run.status,
        ticket_status=ticket.status,
        reply=ticket.customer_reply,
        reply_from=ticket.reply_author,
        finished=is_finished(run.status, ticket.status),
        checked=_checked(events),
        facts=await _facts(session, ticket, run.id, shipping, known_shipment),
        specialist_reply_business_days=settings.specialist_reply_business_days,
    )
    return RunSnapshot(run=status, steps=steps)


def _status_frame(status: RunStatusEvent) -> str:
    return sse_frame("status", status.model_dump_json())


def _step_frame(step: RunStepEvent) -> str:
    return sse_frame("step", step.model_dump_json(), event_id=str(step.id))


def _end_frame(reason: str) -> str:
    # "finished": stop listening. "timeout": reconnect for a fresh stream.
    return sse_frame("end", compact_json({"reason": reason}))


async def stream_run(
    sessions: SessionFactory,
    principal: Principal,
    first: RunSnapshot,
    settings: Settings,
    monotonic: Monotonic = time.monotonic,
) -> AsyncGenerator[str]:
    """Server-Sent Events for one run: the snapshot first, then each change as it happens.

    Ends when the run is finished, or after `sse_max_stream_seconds` (the client then
    reconnects with its token). A client that disconnects stops the generator.
    """
    seen: set[UUID] = set()
    yield _status_frame(first.run)
    for step in first.steps:
        seen.add(step.id)
        yield _step_frame(step)
    if first.run.finished:
        yield _end_frame("finished")
        return

    last_status = first.run
    deadline = monotonic() + settings.sse_max_stream_seconds
    last_sent = monotonic()
    while monotonic() < deadline:
        await asyncio.sleep(settings.sse_poll_interval_seconds)
        async with sessions() as session:
            known = None if first.run.facts is None else first.run.facts.shipment
            snapshot = await load_snapshot(
                session, principal, first.run.run_id, settings, known_shipment=known
            )

        frames = [_step_frame(step) for step in snapshot.steps if step.id not in seen]
        seen.update(step.id for step in snapshot.steps)
        if snapshot.run != last_status:
            frames.append(_status_frame(snapshot.run))
            last_status = snapshot.run

        if frames:
            for frame in frames:
                yield frame
            last_sent = monotonic()
        elif monotonic() - last_sent >= settings.sse_heartbeat_seconds:
            yield sse_comment("keep-alive")
            last_sent = monotonic()

        if snapshot.run.finished:
            yield _end_frame("finished")
            return
    yield _end_frame("timeout")
