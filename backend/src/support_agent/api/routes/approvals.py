"""Refund proposals, the approval queue and decisions. Reviewers only.

Thin routes: the reviewer comes from the token, the time from the clock dependency,
and every rule lives in the services.
"""

from uuid import UUID

from fastapi import APIRouter

from support_agent.api.deps import (
    ClockDep,
    DecisionEnvDep,
    ReviewerDep,
    SessionDep,
    SettingsDep,
)
from support_agent.schemas.approvals import (
    ApprovalListResponse,
    DecisionRequest,
    DecisionResponse,
    ProposalResponse,
)
from support_agent.services import approvals as approvals_service
from support_agent.services import proposals as proposals_service

router = APIRouter(tags=["approvals"])


# No request body: the amount comes only from the eligibility rules.
@router.post("/tickets/{ticket_id}/refund-proposals", status_code=201)
async def create_refund_proposal(
    ticket_id: UUID,
    reviewer: ReviewerDep,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> ProposalResponse:
    return await proposals_service.create_refund_proposal(
        session, reviewer, ticket_id, now=clock(), settings=settings
    )


@router.get("/approvals")
async def list_approvals(
    _reviewer: ReviewerDep, session: SessionDep, clock: ClockDep
) -> ApprovalListResponse:
    return await approvals_service.list_pending(session, clock())


# For a workflow run this also resumes the paused run (fixed code only, no model).
@router.post("/approvals/{approval_id}/decision")
async def decide_approval(
    approval_id: UUID,
    body: DecisionRequest,
    reviewer: ReviewerDep,
    session: SessionDep,
    env: DecisionEnvDep,
) -> DecisionResponse:
    return await approvals_service.decide(session, reviewer, approval_id, body, env)
