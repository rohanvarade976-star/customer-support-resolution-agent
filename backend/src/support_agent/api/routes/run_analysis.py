"""The reviewer's AI analysis of a run and the automation summary (ADR-021). Reviewers only.

Thin routes: `ReviewerDep` refuses customers with 403 before anything is read.
"""

from uuid import UUID

from fastapi import APIRouter

from support_agent.api.deps import (
    CheckpointerDep,
    ClockDep,
    ReviewerDep,
    SessionDep,
    SettingsDep,
)
from support_agent.schemas.run_analysis import AutomationSummaryResponse, RunAnalysisResponse
from support_agent.services import run_analysis

router = APIRouter(tags=["analysis"])


@router.get("/runs/{run_id}/analysis")
async def get_run_analysis(
    run_id: UUID,
    reviewer: ReviewerDep,
    session: SessionDep,
    checkpointer: CheckpointerDep,
    settings: SettingsDep,
) -> RunAnalysisResponse:
    return await run_analysis.get_analysis(session, checkpointer, settings, reviewer, run_id)


@router.get("/reviewer/automation-summary")
async def get_automation_summary(
    _reviewer: ReviewerDep, session: SessionDep, clock: ClockDep
) -> AutomationSummaryResponse:
    return await run_analysis.automation_summary(session, clock())
