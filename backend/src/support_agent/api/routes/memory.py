"""The signed-in customer's own long-term memory (ADR-013). Customer-only: reviewers get 403.

The customer id comes from the JWT (the Principal), never from the request.
"""

from fastapi import APIRouter

from support_agent.api.deps import ClockDep, CustomerDep, SessionDep
from support_agent.schemas.memory import MemoryDeletedResponse, MemoryItemResponse
from support_agent.services import memory as memory_service

router = APIRouter(tags=["memory"])


@router.get("/me/memory")
async def list_my_memory(
    principal: CustomerDep, session: SessionDep, clock: ClockDep
) -> list[MemoryItemResponse]:
    return await memory_service.list_my_memory(session, principal, now=clock())


@router.delete("/me/memory")
async def delete_my_memory(principal: CustomerDep, session: SessionDep) -> MemoryDeletedResponse:
    return await memory_service.delete_my_memory(session, principal)
