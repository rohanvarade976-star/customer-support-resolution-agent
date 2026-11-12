"""Workflow runs. Ownership and the workflow itself live in services/runs.py."""

from uuid import UUID

from fastapi import APIRouter

from support_agent.api.deps import (
    ClockDep,
    CustomerDep,
    LLMClientDep,
    PrincipalDep,
    SessionDep,
    SettingsDep,
)
from support_agent.schemas.runs import RunResponse
from support_agent.services import runs as runs_service

router = APIRouter(tags=["runs"])


# 202 per the PRD: today the run finishes before we reply; from M7 a worker runs it.
@router.post("/tickets/{ticket_id}/runs", status_code=202)
async def start_run(
    ticket_id: UUID,
    principal: CustomerDep,
    session: SessionDep,
    llm: LLMClientDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> RunResponse:
    return await runs_service.start_run(
        session,
        principal,
        ticket_id,
        llm=llm,
        confidence_threshold=settings.classifier_confidence_threshold,
        clock=clock,
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: UUID, principal: PrincipalDep, session: SessionDep) -> RunResponse:
    return await runs_service.get_run(session, principal, run_id)
