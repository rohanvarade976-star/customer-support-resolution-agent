"""Long-term customer memory: written by code from verified outcomes, read as background.

The rules (ADR-013):

- **Only code writes memory, and only fixed codes.** `record_run_outcome` is called when a
  run finishes. What it stores comes from `domain.memory.memory_outcome`, which takes
  statuses and codes only. The customer is the ticket's owner (already matched to the JWT
  when the run started), never a value from text or a model.
- **Strictly per customer.** Every read and delete names one customer id taken from the
  Principal or the run's ticket. Composite foreign keys make the database refuse an item
  whose ticket or order belongs to someone else.
- **Context, never facts.** `load_for_run` renders items as plain lines; the agents get them
  inside an untrusted-data wrapper. Eligibility, amounts and validation never call this
  module: they read live records only.
- **Bounded.** Items expire after `memory_retention_days`; reads skip expired items, and the
  worker's purge job deletes them. A customer can delete all of theirs at any time.

Nothing here commits, except the two entry points that own their transaction
(`delete_my_memory`, `purge_expired`).
"""

import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.db.models import Ticket, WorkflowRun
from support_agent.domain.enums import MemoryKind, MemoryOutcome
from support_agent.domain.memory import render_memory
from support_agent.repositories import customer_memory, orders
from support_agent.schemas.auth import Principal
from support_agent.schemas.memory import MemoryDeletedResponse, MemoryItemResponse

log = logging.getLogger(__name__)


async def record_run_outcome(
    session: AsyncSession,
    settings: Settings,
    *,
    run: WorkflowRun,
    ticket: Ticket,
    kind: MemoryKind,
    outcome: MemoryOutcome,
    order_number: str | None,
    now: datetime,
) -> None:
    """Remember how `run` ended, once per run. Not committed: it joins the caller's save.

    `order_number` is the run's verified order (the ticket's own, or a hint code checked for
    ownership). It is looked up again for this customer; one that is not theirs is dropped.
    """
    customer_id = ticket.customer_id
    order_id = ticket.order_id
    if order_number is not None:
        order_id = await orders.find_id_for_customer(session, customer_id, order_number)
    inserted = await customer_memory.insert_if_absent(
        session,
        customer_id=customer_id,
        ticket_id=ticket.id,
        order_id=order_id,
        kind=kind,
        outcome=outcome,
        category=ticket.category,
        source_run_id=run.id,
        now=now,
        expires_at=now + timedelta(days=settings.memory_retention_days),
    )
    if inserted:
        log.info(
            "memory_recorded",
            extra={"run_id": str(run.id), "kind": kind.value, "outcome": outcome.value},
        )


async def load_for_run(
    session: AsyncSession, settings: Settings, customer_id: UUID, *, now: datetime
) -> str:
    """The customer's recent unexpired items as plain lines ("" when there are none)."""
    if settings.memory_max_items == 0:
        return ""
    entries = await customer_memory.list_recent(
        session, customer_id, now=now, limit=settings.memory_max_items
    )
    return render_memory(entries)


async def list_my_memory(
    session: AsyncSession, principal: Principal, *, now: datetime
) -> list[MemoryItemResponse]:
    """GET /me/memory: the signed-in customer's own unexpired items, newest first."""
    rows = await customer_memory.list_for_customer(session, principal.id, now=now)
    return [
        MemoryItemResponse(
            kind=item.kind,
            outcome_code=item.outcome_code,
            category=item.category,
            order_number=order_number,
            created_at=item.created_at,
            expires_at=item.expires_at,
        )
        for item, order_number in rows
    ]


async def delete_my_memory(session: AsyncSession, principal: Principal) -> MemoryDeletedResponse:
    """DELETE /me/memory: remove every item of the signed-in customer. Commits."""
    deleted = await customer_memory.delete_for_customer(session, principal.id)
    await session.commit()
    log.info("memory_deleted_by_customer", extra={"deleted": deleted})
    return MemoryDeletedResponse(deleted=deleted)


async def purge_expired(session: AsyncSession, *, now: datetime) -> int:
    """The worker's scheduled job: delete every expired item. Commits. Safe to run twice."""
    deleted = await customer_memory.delete_expired(session, now=now)
    await session.commit()
    log.info("memory_purged", extra={"deleted": deleted})
    return deleted
