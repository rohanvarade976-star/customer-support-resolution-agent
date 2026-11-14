"""The reviewer's AI analysis of a run, and the automation summary (ADR-021). Reviewers only.

Ids, codes, counts and times, computed by code from the audit trail (domain/run_analysis.py),
plus one piece of model text: the accepted draft's `rationale`, which is AI-generated and
unverified. It is never sent to customers, is rendered as plain text, and nothing reads it
to decide an amount or an action.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from support_agent.domain.enums import RunMode, RunStatus
from support_agent.domain.run_analysis import RiskCode

Detail = str | int | float | bool | list[str]


class _FromDomain(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # built from the domain's dataclasses


class AiRecommendation(_FromDomain):
    action: str | None  # the resolution agent's latest action code; None if it never finished
    accepted: bool  # a draft passed every code check; False means code escalated the case
    reason_codes: list[str]
    # The accepted draft's rationale, as the model wrote it. None when no draft was accepted.
    rationale: str | None
    source: Literal["ai_generated"] = "ai_generated"
    verified: Literal[False] = False  # never checked against records: context, not evidence


class AgentActivity(_FromDomain):
    agent: str
    at: datetime
    duration_ms: int | None
    details: dict[str, Detail]


class ToolUse(_FromDomain):
    call_id: str
    tool: str
    agent: str
    outcome: str
    latency_ms: int | None
    by_code: bool


class QualityChecks(_FromDomain):
    attempts: int
    revisions: int
    validator_passed: bool | None
    validation_codes: list[str]
    critic_verdict: str | None
    facts_grounded: int
    facts_dropped: int
    citations_verified: int
    citations_dropped: int
    reply_replaced: bool


class RiskFlag(_FromDomain):
    code: RiskCode
    codes: list[str]
    count: int


class RunCost(_FromDomain):
    llm_calls: int
    input_tokens: int
    output_tokens: int
    active_ms: int | None
    fallback_calls: int


class RunAnalysisResponse(BaseModel):
    run_id: UUID
    mode: RunMode
    status: RunStatus
    recommendation: AiRecommendation
    agents: list[AgentActivity]  # in the order their results were recorded
    tools: list[ToolUse]
    quality: QualityChecks
    risk_flags: list[RiskFlag]
    cost: RunCost


class AutomationSummaryResponse(BaseModel):
    """What the system did on its own, for runs started in the last `days` days."""

    days: int
    since: datetime
    runs: int
    resolved_automatically: int  # completed with no refund proposal: no person involved
    sent_for_approval: int  # refund proposals made (each waits for a person)
    escalated: int
    # From the run's start to its first outcome: completed, escalated, failed or approval
    # requested. None when no run has reached one yet.
    median_seconds_to_outcome: float | None
