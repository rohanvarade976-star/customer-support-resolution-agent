"""Run the whole workflow in both modes with the real models and compare them.

For Priya, Rahul, Vikram, Meera and Arjun it starts a real run (a ticket, a workflow run,
audit events, and for a refund a pending approval) twice: once in multi-agent mode (supervisor,
parallel investigator and policy agent, resolution agent, critic) and once in the
single-agent baseline (one agent with every read tool). It prints, side by side: the run's
outcome, the resolution action, the reason codes, the number of LLM calls, tokens and the
latency.

Before EVERY run the demo database is reset to its starting state (the same code as
scripts/reset_demo.py), so each scenario starts as in the PRD and the first mode's pending
refund proposal cannot block the second mode's. That deletes the seeded customers' tickets,
runs, proposals, approvals and audit events, and empties the mock payment and ticketing
tables. It refuses to run when APP_ENV=production. The database is left in the state of the
last run: run scripts/reset_demo.py afterwards for a clean demo.

Needs, running locally:
- Postgres with the policies ingested  (uv run python scripts/ingest_policies.py)
- the mock shipping provider running (docker compose up -d)
- GROQ_API_KEY and DEMO_PASSWORD in .env
The first run downloads the local embedding model (fastembed).

A refund run stops paused, waiting for a reviewer (its checkpoint is kept in Postgres); this
script never decides, so no money moves.

    uv run python scripts/run_workflow.py
    uv run python scripts/run_workflow.py --only meera --mode multi_agent

Not run in CI or pytest: it calls the real LLM. The model is not fully deterministic, so
results differ a little between runs. Only codes, counts and the reply on the ticket are
printed (the reply is the validated message the customer would see).
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from support_agent.api.deps import get_branch_sessions
from support_agent.core.config import Settings, get_settings
from support_agent.core.demo_anchor import resolve_demo_now
from support_agent.core.event_loop import run
from support_agent.core.logging import configure_logging
from support_agent.core.security import hash_password
from support_agent.db.demo_reset import reset_demo
from support_agent.db.models import AuditEvent, Customer, Ticket, WorkflowRun
from support_agent.db.seed import load_scenarios
from support_agent.db.session import get_engine, get_sessionmaker
from support_agent.domain.enums import Role, RunMode
from support_agent.integrations.payments import PaymentClient
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.errors import LLMError
from support_agent.llm.providers import build_llm_client
from support_agent.rag.embeddings import get_embedder
from support_agent.schemas.auth import Principal
from support_agent.schemas.tickets import CreateTicketRequest
from support_agent.services import runs as runs_service
from support_agent.services import tickets as ticket_service
from support_agent.tools.read_tools import build_read_registry
from support_agent.workflow.checkpointer import open_checkpointer

MODES = (RunMode.MULTI_AGENT, RunMode.BASELINE)


@dataclass(frozen=True)
class Scenario:
    name: str
    email: str
    order_number: str
    complaint: str


# The same made-up complaints as scripts/run_resolution.py, matching data/seed/scenarios.json.
SCENARIOS = [
    Scenario(
        "priya",
        "priya@example.com",
        "ORD-1001",
        "I sent my headphones back over a week ago and the refund has not arrived. "
        "Order ORD-1001. Am I entitled to a refund?",
    ),
    Scenario(
        "rahul",
        "rahul@example.com",
        "ORD-1004",
        "I bought wired earphones two months ago (ORD-1004) and I would like my money back.",
    ),
    Scenario(
        "vikram",
        "vikram@example.com",
        "ORD-1006",
        "I returned my Bluetooth speaker (ORD-1006) and nobody has refunded me. Why not?",
    ),
    Scenario(
        "meera",
        "meera@example.com",
        "ORD-1003",
        "The phone from order ORD-1003 arrived yesterday with a cracked screen.",
    ),
    Scenario(
        "arjun",
        "arjun@example.com",
        "ORD-1002",
        "My laptop (ORD-1002) has said 'in transit' for 8 days. Where is it?",
    ),
]


@dataclass(frozen=True)
class Result:
    mode: RunMode
    run_status: str
    action: str
    reason_codes: str
    llm_calls: int
    tokens: str  # "input/output"
    latency_s: float
    reply: str


async def reset_database(password_hash: str, settings: Settings) -> None:
    now = resolve_demo_now(settings.demo_anchor_date, datetime.now(UTC))
    async with get_sessionmaker()() as session, session.begin():
        await reset_demo(session, load_scenarios(), now, password_hash)


async def principal_for(email: str) -> Principal:
    async with get_sessionmaker()() as session:
        customer = await session.scalar(select(Customer).where(Customer.email == email))
    if customer is None:
        sys.exit(f"No customer {email} in the database after the reset.")
    return Principal(id=customer.id, role=Role.CUSTOMER, name=customer.name, email=customer.email)


async def read_result(mode: RunMode, run_id: UUID, latency_s: float) -> Result:
    """What the run decided, from the database: statuses, audit codes, usage, the saved reply."""
    async with get_sessionmaker()() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:  # we just created it
            sys.exit(f"Run {run_id} is missing from the database.")
        ticket = await session.get(Ticket, run.ticket_id)
        events = list(await session.scalars(select(AuditEvent).where(AuditEvent.run_id == run_id)))
        resolution = next((e for e in events if e.event_type == "resolution.completed"), None)
        codes = [] if resolution is None else resolution.redacted_payload["reason_codes"]
        errors = [
            code
            for e in events
            if e.event_type in ("run.escalated", "run.failed")
            for code in e.redacted_payload.get("error_codes", [])
        ]
        action = "(none)" if resolution is None else resolution.redacted_payload["action"]
        reasons = ", ".join(codes) or "-"
        if errors:
            reasons = f"{reasons} | errors: {', '.join(errors)}"
        return Result(
            mode=mode,
            run_status=run.status.value,
            action=action,
            reason_codes=reasons,
            llm_calls=run.llm_calls,
            tokens=f"{run.input_tokens}/{run.output_tokens}",
            latency_s=latency_s,
            reply="" if ticket is None else (ticket.customer_reply or ""),
        )


async def run_one(
    scenario: Scenario,
    mode: RunMode,
    *,
    env: runs_service.WorkflowEnv,
    password_hash: str,
) -> Result:
    await reset_database(password_hash, env.settings)
    principal = await principal_for(scenario.email)
    async with get_sessionmaker()() as session:
        ticket = await ticket_service.create_ticket(
            session,
            principal,
            CreateTicketRequest(message=scenario.complaint, order_number=scenario.order_number),
        )
        started = time.perf_counter()
        response = await runs_service.start_run(session, principal, ticket.id, env, mode)
        latency = time.perf_counter() - started
    return await read_result(mode, response.run_id, latency)


def show(scenario: Scenario, results: list[Result]) -> None:
    print(f"\n{'=' * 78}\n{scenario.name.upper()}  {scenario.order_number}\n{'=' * 78}")
    print(f"Complaint: {scenario.complaint}\n")
    labels = ("run status", "action", "reason codes", "LLM calls", "tokens in/out", "latency")
    width = 34
    print(f"{'':<16}" + "".join(f"{r.mode.value:<{width}}" for r in results))
    rows = [
        [r.run_status for r in results],
        [r.action for r in results],
        [r.reason_codes[: width - 2] for r in results],
        [str(r.llm_calls) for r in results],
        [r.tokens for r in results],
        [f"{r.latency_s:.1f}s" for r in results],
    ]
    for label, cells in zip(labels, rows, strict=True):
        print(f"{label:<16}" + "".join(f"{cell:<{width}}" for cell in cells))
    for r in results:
        print(f"\nreply saved on the ticket ({r.mode.value}): {r.reply or '(none)'}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--only", choices=[s.name for s in SCENARIOS], help="run one scenario")
    parser.add_argument("--mode", choices=[m.value for m in MODES], help="run one mode")
    args = parser.parse_args()

    # Model text may hold characters the Windows console code page cannot print.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    configure_logging("WARNING")  # hide the per-call log lines; the table below shows the totals
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    if settings.groq_api_key is None:
        sys.exit("GROQ_API_KEY is not set in .env. Add it and run again.")
    if settings.demo_password is None:
        sys.exit("DEMO_PASSWORD is not set in .env (the reset reseeds the demo accounts).")
    password_hash = hash_password(settings.demo_password.get_secret_value())
    modes = [m for m in MODES if args.mode in (None, m.value)]
    database_url = settings.database_url.get_secret_value()

    try:
        async with (
            ShippingClient(settings) as shipping,
            PaymentClient(settings) as payments,  # never called: nothing is approved here
            open_checkpointer(database_url, build_read_registry()) as checkpointer,
        ):
            env = runs_service.WorkflowEnv(
                settings=settings,
                llm=build_llm_client(settings),
                shipping_client=shipping,
                payments=payments,
                embedder=get_embedder(settings),
                clock=lambda: datetime.now(UTC),
                branch_sessions=get_branch_sessions(),
                checkpointer=checkpointer,
            )
            for scenario in [s for s in SCENARIOS if args.only in (None, s.name)]:
                results = [
                    await run_one(scenario, mode, env=env, password_hash=password_hash)
                    for mode in modes
                ]
                show(scenario, results)
    except LLMError as exc:
        # Our error messages never contain prompts, responses or keys.
        sys.exit(f"LLM call failed: {type(exc).__name__}: {exc}")
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    run(main())  # a selector event loop, which psycopg needs on Windows
