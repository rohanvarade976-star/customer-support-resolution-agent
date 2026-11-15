"""The reviewer's escalations inbox and its actions (ADR-022). Reviewers only: a customer
gets 403 from ReviewerDep before anything runs. Thin routes: the rules are in the service.

A manual refund proposal from an escalation uses POST /tickets/{ticket_id}/refund-proposals.
"""

from uuid import UUID

from fastapi import APIRouter

from support_agent.api.deps import ClockDep, ReviewerDep, SessionDep, SettingsDep
from support_agent.schemas.escalations import (
    EscalationActionResponse,
    EscalationCase,
    EscalationListResponse,
    ResolveRequest,
    SpecialistReplyRequest,
)
from support_agent.services import escalations as escalations_service

router = APIRouter(tags=["escalations"])


@router.get("/reviewer/escalations")
async def list_escalations(
    _reviewer: ReviewerDep, session: SessionDep, settings: SettingsDep
) -> EscalationListResponse:
    return await escalations_service.list_escalations(session, settings)


@router.get("/reviewer/escalations/{ticket_id}")
async def get_escalation(
    ticket_id: UUID,
    _reviewer: ReviewerDep,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> EscalationCase:
    return await escalations_service.get_escalation(session, settings, ticket_id, clock())


@router.post("/reviewer/escalations/{ticket_id}/reply")
async def reply_to_customer(
    ticket_id: UUID, body: SpecialistReplyRequest, reviewer: ReviewerDep, session: SessionDep
) -> EscalationActionResponse:
    return await escalations_service.reply(session, reviewer, ticket_id, body)


@router.post("/reviewer/escalations/{ticket_id}/resolve")
async def resolve_ticket(
    ticket_id: UUID, body: ResolveRequest, reviewer: ReviewerDep, session: SessionDep
) -> EscalationActionResponse:
    return await escalations_service.resolve(session, reviewer, ticket_id, body)
