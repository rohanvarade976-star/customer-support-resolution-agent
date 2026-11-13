"""Workflow runs. Ownership and the workflow itself live in services/runs.py."""

from uuid import UUID

from fastapi import APIRouter

from support_agent.api.deps import (
    CustomerDep,
    PrincipalDep,
    SessionDep,
    WorkflowEnvDep,
)
from support_agent.schemas.messages import CustomerMessageRequest
from support_agent.schemas.runs import RunResponse
from support_agent.services import runs as runs_service

router = APIRouter(tags=["runs"])


# 202 per the PRD: today the segment finishes before we reply; from M7 Task 2 a worker runs it.
@router.post("/tickets/{ticket_id}/runs", status_code=202)
async def start_run(
    ticket_id: UUID, principal: CustomerDep, session: SessionDep, env: WorkflowEnvDep
) -> RunResponse:
    return await runs_service.start_run(session, principal, ticket_id, env)


@router.get("/runs/{run_id}")
async def get_run(run_id: UUID, principal: PrincipalDep, session: SessionDep) -> RunResponse:
    return await runs_service.get_run(session, principal, run_id)


# The customer answers the run's question. Owner only; the run resumes where it paused.
@router.post("/runs/{run_id}/messages", status_code=202)
async def post_message(
    run_id: UUID,
    body: CustomerMessageRequest,
    principal: CustomerDep,
    session: SessionDep,
    env: WorkflowEnvDep,
) -> RunResponse:
    return await runs_service.post_customer_message(session, env, principal, run_id, body.text)
