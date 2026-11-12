"""All SQL for policy documents and policy chunks."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import PolicyChunk, PolicyDocument


async def get_document(
    session: AsyncSession, policy_key: str, version: str
) -> PolicyDocument | None:
    stmt = select(PolicyDocument).where(
        PolicyDocument.policy_key == policy_key, PolicyDocument.version == version
    )
    return await session.scalar(stmt)


async def add_document(session: AsyncSession, document: PolicyDocument) -> PolicyDocument:
    session.add(document)
    await session.flush()  # assigns nothing new (id is set in Python) but surfaces errors early
    return document


async def list_chunks(session: AsyncSession, document_id: UUID) -> list[PolicyChunk]:
    """Every chunk of one document version. Never returns chunks of another version."""
    stmt = select(PolicyChunk).where(PolicyChunk.document_id == document_id)
    return list(await session.scalars(stmt))


def add_chunk(session: AsyncSession, chunk: PolicyChunk) -> None:
    session.add(chunk)


async def delete_chunk(session: AsyncSession, chunk: PolicyChunk) -> None:
    await session.delete(chunk)
