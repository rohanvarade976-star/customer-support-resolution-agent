"""Runs golden cases through the real workflow and reads back what happened, from the database.

For every (configuration, case, repeat):

1. Reset the eval-only data (evals/datasets/eval_seed.json): delete everything the eval
   customers own, including earlier runs, proposals and memory, then seed them again. The
   demo customers are never touched. Refused when APP_ENV=production.
2. Create the ticket and start the run exactly as the API does (`create_ticket`,
   `start_run`), as the case's customer. Identity comes from the customer row, as a JWT
   would give it, never from the complaint.
3. Do the worker's part in-process (`run_start_job`), timed. The run ends at its first pause
   (a refund waits for a reviewer, a question waits for the customer) or at its end. No
   reviewer is simulated: refunds are scored at the proposal (ADR-016), so no money moves.
4. Read the outcome from the database: run status and usage, the stored refund proposals,
   the resolution audit event (validator codes, critic verdicts), the escalation error
   codes, and whether the reply holds another customer's data.

The mock shipping provider runs in-process with the eval shipments and the same frozen "now"
as the seed, so the order's and the carrier's dates always agree as designed.

`run_plan` loops over the plan, skips what the results file says is finished, and saves each
result as soon as it exists.
"""

import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from evals.cases import EVAL_SEED_FILE, EVAL_SHIPMENTS_FILE, CaseResult, EvalCase, EvalConfig
from evals.rate_limit import RateLimitedProvider, build_limiter
from evals.store import ResultStore
from mock_providers.shipping.app import create_app as create_shipping_app
from mock_providers.shipping.config import ShippingSettings
from support_agent.api.deps import get_branch_sessions
from support_agent.core.config import Settings
from support_agent.core.errors import AppError
from support_agent.core.security import hash_password
from support_agent.db.models import ActionProposal, AuditEvent, Customer, Ticket, WorkflowRun
from support_agent.db.seed import SeedData, load_scenarios, reset, seed
from support_agent.db.session import get_sessionmaker
from support_agent.domain.enums import ActionType, ResolutionAction, Role, RunMode, RunStatus
from support_agent.integrations.payments import PaymentClient
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.client import LLMClient
from support_agent.llm.providers import build_llm_client, build_providers
from support_agent.rag.embeddings import Embedder, get_embedder
from support_agent.schemas.auth import Principal
from support_agent.schemas.tickets import CreateTicketRequest
from support_agent.services import runs as runs_service
from support_agent.services import tickets as ticket_service
from support_agent.tools.read_tools import build_read_registry
from support_agent.workflow.checkpointer import open_checkpointer
from support_agent.workflow.deps import BranchSessions
from support_agent.workflow.graph import Checkpointer

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]
RunCase = Callable[[EvalConfig, EvalCase, int], Awaitable[CaseResult]]

# A paused or finished run's status says which action it took (the router decides by action).
_ACTION_FOR_STATUS = {
    RunStatus.AWAITING_APPROVAL: ResolutionAction.REFUND,
    RunStatus.AWAITING_CUSTOMER: ResolutionAction.REQUEST_INFO,
    RunStatus.ESCALATED: ResolutionAction.ESCALATE,
}


@dataclass
class HarnessEnv:
    """Everything a case needs. Built by `open_harness` for real runs; tests build their own."""

    settings: Settings
    llm: LLMClient
    sessions: Sessions
    shipping_client: ShippingClient
    payments: PaymentClient  # never called: nothing is approved
    embedder: Embedder
    branch_sessions: BranchSessions
    checkpointer: Checkpointer
    now: datetime  # the frozen "now" of the seed, the workflow and the mock carrier
    seed_data: SeedData
    password_hash: str  # of a random throwaway password: eval accounts cannot log in
    # Total seconds the rate limiter has made calls wait so far (0 without a limiter).
    limiter_wait: Callable[[], float] = field(default=lambda: 0.0)


def settings_for(config: EvalConfig, settings: Settings) -> Settings:
    if config == EvalConfig.MULTI_AGENT_NO_CRITIC:
        return settings.model_copy(update={"critic_enabled": False})
    return settings


def mode_for(config: EvalConfig) -> RunMode:
    return RunMode.BASELINE if config == EvalConfig.BASELINE else RunMode.MULTI_AGENT


# ---------------------------------------------------------------- one case


async def reset_eval_data(session: AsyncSession, env: HarnessEnv) -> None:
    """The eval customers back to their seeded state. `reset` refuses in production."""
    await reset(session, env.seed_data)
    await seed(session, env.seed_data, env.now, env.password_hash)
    await session.commit()


async def principal_for(session: AsyncSession, email: str) -> Principal:
    customer = await session.scalar(select(Customer).where(Customer.email == email))
    if customer is None:
        raise LookupError(f"eval customer {email} is not in evals/datasets/eval_seed.json")
    return Principal(id=customer.id, role=Role.CUSTOMER, name=customer.name, email=customer.email)


def leaked_terms(reply: str | None, forbidden: Sequence[str]) -> list[str]:
    text = (reply or "").casefold()
    return [term for term in forbidden if term.casefold() in text]


async def read_outcome(
    session: AsyncSession, run_id: UUID, case: EvalCase, base: CaseResult
) -> CaseResult:
    """What the run did, from the database only (never from model text)."""
    run = await session.get(WorkflowRun, run_id, populate_existing=True)
    if run is None:
        return base.model_copy(update={"error": "RUN_MISSING"})
    ticket = await session.get(Ticket, run.ticket_id, populate_existing=True)
    events = list(await session.scalars(select(AuditEvent).where(AuditEvent.run_id == run_id)))
    proposals = await session.scalars(
        select(ActionProposal).where(
            ActionProposal.run_id == run_id,
            ActionProposal.action_type == ActionType.REQUEST_REFUND_APPROVAL,
        )
    )
    resolution = next((e for e in events if e.event_type == "resolution.completed"), None)
    payload = {} if resolution is None else resolution.redacted_payload
    error_codes = [
        code
        for e in events
        if e.event_type in ("run.escalated", "run.failed")
        for code in e.redacted_payload.get("error_codes", [])
    ]

    action = _ACTION_FOR_STATUS.get(run.status)
    if run.status == RunStatus.COMPLETED and "action" in payload:
        action = ResolutionAction(payload["action"])
    error = None if action is not None else f"RUN_{run.status.value.upper()}"

    return base.model_copy(
        update={
            "run_status": run.status.value,
            "observed_action": action,
            "proposed_refunds_minor": [int(p.payload["amount_minor"]) for p in proposals],
            "error": error,
            "error_codes": error_codes,
            "validation_codes": payload.get("validation_codes", []),
            "critic_verdicts": payload.get("critic_verdicts", []),
            "leaked_terms": leaked_terms(
                None if ticket is None else ticket.customer_reply, case.forbidden_reply_terms
            ),
            "llm_calls": run.llm_calls,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
        }
    )


async def run_case(env: HarnessEnv, config: EvalConfig, case: EvalCase, repeat: int) -> CaseResult:
    base = CaseResult(
        config=config,
        case_id=case.id,
        repeat=repeat,
        category=case.category,
        expected_action=case.expected_action,
        expected_refund_minor=case.expected_refund_minor,
        acceptable_alternatives=case.acceptable_alternatives,
    )
    settings = settings_for(config, env.settings)

    def clock() -> datetime:
        return env.now

    workflow_env = runs_service.WorkflowEnv(
        settings=settings,
        llm=env.llm,
        shipping_client=env.shipping_client,
        payments=env.payments,
        embedder=env.embedder,
        clock=clock,
        branch_sessions=env.branch_sessions,
        checkpointer=env.checkpointer,
    )

    async with env.sessions() as session:
        await reset_eval_data(session, env)
        principal = await principal_for(session, case.customer_email)
        try:
            ticket = await ticket_service.create_ticket(
                session,
                principal,
                CreateTicketRequest(message=case.complaint, order_number=case.order_number),
            )
        except AppError as exc:
            return base.model_copy(update={"error": f"INTAKE_{exc.error_code}"})
        enqueue = runs_service.EnqueueEnv(settings, clock, env.checkpointer)
        started = await runs_service.start_run(
            session, principal, ticket.id, enqueue, mode_for(config)
        )

        waited_before = env.limiter_wait()
        began = time.perf_counter()
        try:
            await runs_service.run_start_job(session, workflow_env, started.run_id)
        except Exception as exc:  # recorded as this run's error, by class name only
            # The service has already rolled back the segment's uncommitted work.
            return base.model_copy(update={"error": f"EXCEPTION_{type(exc).__name__}"})
        elapsed = time.perf_counter() - began
        waited = env.limiter_wait() - waited_before

        result = await read_outcome(session, started.run_id, case, base)
    return result.model_copy(
        update={
            "active_seconds": round(max(0.0, elapsed - waited), 3),
            "rate_limit_wait_seconds": round(waited, 3),
        }
    )


# ---------------------------------------------------------------- the plan and resume


@dataclass(frozen=True)
class PlanItem:
    config: EvalConfig
    case: EvalCase
    repeat: int

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.config.value, self.case.id, self.repeat)


def build_plan(
    configs: Sequence[EvalConfig], cases: Sequence[EvalCase], repeats: int
) -> list[PlanItem]:
    """Repeat-major, then case, then configuration: an interrupted run still has every
    configuration on the same cases, so the comparison stays like for like."""
    return [
        PlanItem(config, case, repeat)
        for repeat in range(repeats)
        for case in cases
        for config in configs
    ]


@dataclass
class PlanOutcome:
    ran: int = 0
    skipped: int = 0  # already finished in an earlier, interrupted run
    stopped_early: bool = False  # the models kept being unreachable (quota)


async def run_plan(
    plan: Sequence[PlanItem],
    store: ResultStore,
    run_one: RunCase,
    *,
    stop_after_infra_failures: int = 2,
    on_result: Callable[[CaseResult], None] | None = None,
) -> PlanOutcome:
    """Run every unfinished item, saving each result as soon as it exists.

    Stops after `stop_after_infra_failures` runs in a row whose models were unreachable:
    the daily quota is probably gone, and every further case would only fail the same way.
    Those runs are saved but not finished, so the next resume runs them again.
    """
    done = store.finished_keys()
    outcome = PlanOutcome()
    failures_in_a_row = 0
    for item in plan:
        if item.key in done:
            outcome.skipped += 1
            continue
        result = await run_one(item.config, item.case, item.repeat)
        store.append(result)
        outcome.ran += 1
        if on_result is not None:
            on_result(result)
        failures_in_a_row = failures_in_a_row + 1 if result.infrastructure_failure else 0
        if failures_in_a_row >= stop_after_infra_failures:
            outcome.stopped_early = True
            break
    return outcome


# ---------------------------------------------------------------- real runs


@asynccontextmanager
async def open_harness(
    settings: Settings, *, now: datetime, rpm: float, tpm: float | None
) -> AsyncIterator[HarnessEnv]:
    """The real dependencies: Postgres (DATABASE_URL), the configured LLM providers behind
    a rate limiter each, the local embedder, and the mock carrier in-process."""
    limited = [RateLimitedProvider(p, build_limiter(rpm, tpm)) for p in build_providers(settings)]
    shipping_app = create_shipping_app(
        ShippingSettings(mock_shipping_data_file=EVAL_SHIPMENTS_FILE), clock=lambda: now
    )
    sessionmaker = get_sessionmaker()
    database_url = settings.database_url.get_secret_value()
    async with (
        ShippingClient(settings, transport=httpx.ASGITransport(app=shipping_app)) as shipping,
        PaymentClient(settings) as payments,
        open_checkpointer(database_url, build_read_registry()) as checkpointer,
    ):
        yield HarnessEnv(
            settings=settings,
            llm=build_llm_client(settings, providers=list(limited)),
            sessions=sessionmaker,
            shipping_client=shipping,
            payments=payments,
            embedder=get_embedder(settings),
            branch_sessions=get_branch_sessions(),
            checkpointer=checkpointer,
            now=now,
            seed_data=load_scenarios(EVAL_SEED_FILE),
            password_hash=hash_password(secrets.token_urlsafe(32)),
            limiter_wait=lambda: sum(p.limiter.waited_seconds for p in limited),
        )
