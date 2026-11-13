"""All SQL for customers' replies to runs. The body is untrusted text: never log it."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import CustomerMessage


async def create(
    session: AsyncSession, *, run_id: UUID, ticket_id: UUID, customer_id: UUID, body: str
) -> CustomerMessage:
    message = CustomerMessage(
        run_id=run_id, ticket_id=ticket_id, customer_id=customer_id, body=body
    )
    session.add(message)
    await session.flush()
    return message


async def get(session: AsyncSession, message_id: UUID) -> CustomerMessage | None:
    return await session.get(CustomerMessage, message_id)
