"""Two golden cases through the real eval harness: real workflow, real database, fake LLM.

Everything but the model is real: the eval seed and reset, ticket creation and `start_run` as
the API does them, the worker's `run_start_job`, the tools, required evidence, eligibility,
the validator, the in-process mock carrier with the eval shipments, and reading the outcome
back from the database. The results go through the real results file and metrics.

    n01-eligible-refund              multi-agent run -> a refund proposal for exactly 349900
    h02-no-order-number-single-order no order number -> the run asks (request_info), no refund
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from evals.cases import (
    EVAL_SEED_FILE,
    EVAL_SHIPMENTS_FILE,
    CaseResult,
    EvalCase,
    EvalConfig,
    load_cases,
)
from evals.harness import HarnessEnv, build_plan, run_case, run_plan
from evals.metrics import config_metrics
from evals.store import ResultStore
from mock_providers.shipping.app import create_app as create_shipping_app
from mock_providers.shipping.config import ShippingSettings
from support_agent.agents.fake import (
    AgentFake,
    critique_reply,
    draft_reply,
    policy_run,
    simple_investigation,
    supervisor_reply,
)
from support_agent.core.config import Settings, get_settings
from support_agent.db.models import ActionProposal, Customer, WorkflowRun
from support_agent.db.seed import load_scenarios, seed
from support_agent.domain.enums import ProposalStatus, ResolutionAction, RunStatus
from support_agent.integrations.payments import PaymentClient
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.client import RetrySettings, RoutedLLMClient
from support_agent.llm.fake import tool_reply
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.services import policy_search
from support_agent.workflow.deps import BranchSessions
from support_agent.workflow.graph import Checkpointer

# Must equal the mock carrier's instant, or order and carrier delivery times would disagree.
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
CASE_IDS = ["n01-eligible-refund", "h02-no-order-number-single-order"]

pytestmark = pytest.mark.usefixtures("ingested_policies")


@pytest.fixture
def settings() -> Settings:
    # The fake embedder's similarities are meaningless, so let every passage through.
    return get_settings().model_copy(update={"policy_min_similarity": -1.0})


async def refund_citation(session: AsyncSession, settings: Settings, password_hash: str) -> str:
    """The citation a real policy search returns for ORD-2001 (the harness reseeds later)."""
    data = load_scenarios(EVAL_SEED_FILE)
    await seed(session, data, NOW, password_hash)
    customer_id = await session.scalar(
        select(Customer.id).where(Customer.email == "eval.ananya@example.com")
    )
    assert customer_id is not None
    found = await policy_search.search_policy(
        session, FakeEmbedder(), settings, customer_id, "return window", "ORD-2001", NOW
    )
    return found.passages[0].citation


def scripted_llm(citation: str) -> RoutedLLMClient:
    classified = {"category": "refund", "confidence": 0.95, "reason": "t"}
    fake = AgentFake(
        {
            # n01, then h02 (which names no order, so it stops after classifying).
            "classifier": [
                tool_reply("classification", {**classified, "order_number": "ORD-2001"}),
                tool_reply("classification", classified),
            ],
            "supervisor": [supervisor_reply()],
            "investigator": simple_investigation("ORD-2001"),
            "policy": policy_run(citation, order="ORD-2001"),
            "resolution": [draft_reply("refund", citations=[citation])],
            "critic": [critique_reply("approve")],
        }
    )
    return RoutedLLMClient(
        [fake],
        RetrySettings(max_attempts=1, backoff_base_seconds=0, backoff_max_seconds=0),
        default_temperature=0.0,
        default_max_tokens=256,
    )


@pytest.fixture
async def harness(
    session: AsyncSession,
    settings: Settings,
    branch_sessions: BranchSessions,
    checkpointer: Checkpointer,
    demo_password_hash: str,
) -> AsyncIterator[HarnessEnv]:
    citation = await refund_citation(session, settings, demo_password_hash)
    carrier = create_shipping_app(
        ShippingSettings(app_env="test", mock_shipping_data_file=EVAL_SHIPMENTS_FILE),
        clock=lambda: NOW,
    )

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        yield session  # the rolled-back test session

    async with (
        ShippingClient(settings, transport=httpx.ASGITransport(app=carrier)) as shipping,
        PaymentClient(settings) as payments,  # never called: nothing is approved
    ):
        yield HarnessEnv(
            settings=settings,
            llm=scripted_llm(citation),
            sessions=sessions,
            shipping_client=shipping,
            payments=payments,
            embedder=FakeEmbedder(),
            branch_sessions=branch_sessions,
            checkpointer=checkpointer,
            now=NOW,
            seed_data=load_scenarios(EVAL_SEED_FILE),
            password_hash=demo_password_hash,
        )


def case(case_id: str) -> EvalCase:
    (found,) = [c for c in load_cases() if c.id == case_id]
    return found


async def test_an_eligible_refund_case_is_read_back_from_the_database(
    harness: HarnessEnv, session: AsyncSession
) -> None:
    result = await run_case(harness, EvalConfig.MULTI_AGENT, case(CASE_IDS[0]), 0)

    assert result.run_status == RunStatus.AWAITING_APPROVAL
    assert result.observed_action == ResolutionAction.REFUND
    assert result.proposed_refunds_minor == [349900]
    assert result.critic_verdicts == ["approve"]
    assert result.validation_codes == [[]]
    assert result.llm_calls > 0
    assert result.error is None
    # The database itself: one pending proposal, for the code-computed amount. (The eval seed's
    # refund history for ORD-2009 brings an executed proposal of its own; it is not this run's.)
    pending = select(ActionProposal).where(ActionProposal.status == ProposalStatus.PENDING_APPROVAL)
    proposals = (await session.scalars(pending)).all()
    assert [(p.payload["order_number"], p.payload["amount_minor"]) for p in proposals] == [
        ("ORD-2001", 349900)
    ]
    assert proposals[0].status == ProposalStatus.PENDING_APPROVAL


async def test_two_cases_through_the_plan_are_saved_scored_and_resumed(
    harness: HarnessEnv, session: AsyncSession, tmp_path: Path
) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    plan = build_plan([EvalConfig.MULTI_AGENT], [case(i) for i in CASE_IDS], repeats=1)
    calls: list[str] = []

    async def one(config: EvalConfig, c: EvalCase, repeat: int) -> CaseResult:
        calls.append(c.id)
        return await run_case(harness, config, c, repeat)

    first = await run_plan(plan, store, one)
    again = await run_plan(plan, store, one)  # a resume: everything is already finished

    assert (first.ran, again.ran, again.skipped) == (2, 0, 2)
    assert calls == CASE_IDS
    results = {r.case_id: r for r in store.load()}
    ask = results[CASE_IDS[1]]
    assert ask.run_status == RunStatus.AWAITING_CUSTOMER
    assert ask.observed_action == ResolutionAction.REQUEST_INFO
    assert ask.proposed_refunds_minor == []
    assert ask.llm_calls == 1  # the classifier only: no order, so the run asks

    m = config_metrics(EvalConfig.MULTI_AGENT, store.load())
    assert m.unsafe_refunds == 0
    assert (m.accuracy.strict_correct, m.accuracy.runs) == (2, 2)

    # Each case starts from a clean eval seed: the first case's paused run and its pending
    # proposal are gone; only the second case's run (and the seeded history) remain.
    statuses = (await session.scalars(select(WorkflowRun.status))).all()
    assert sorted(statuses) == sorted([RunStatus.AWAITING_CUSTOMER, RunStatus.COMPLETED])
    pending = select(ActionProposal).where(ActionProposal.status == ProposalStatus.PENDING_APPROVAL)
    assert (await session.scalars(pending)).all() == []
