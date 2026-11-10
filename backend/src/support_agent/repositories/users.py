"""All SQL for looking up the people who can log in."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import Customer, StaffUser


async def get_customer_by_email(session: AsyncSession, email: str) -> Customer | None:
    return await session.scalar(select(Customer).where(Customer.email == email))


async def get_customer_by_subject(session: AsyncSession, subject: str) -> Customer | None:
    return await session.scalar(select(Customer).where(Customer.external_auth_subject == subject))


async def get_staff_by_email(session: AsyncSession, email: str) -> StaffUser | None:
    return await session.scalar(select(StaffUser).where(StaffUser.email == email))


async def get_staff_by_id(session: AsyncSession, staff_id: UUID) -> StaffUser | None:
    return await session.get(StaffUser, staff_id)
