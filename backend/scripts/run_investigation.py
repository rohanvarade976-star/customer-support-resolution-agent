"""Run the real investigator (Groq, with Gemini fallback if configured) on three scenarios.

For Priya (refund after a return), Arjun (late delivery) and Meera (damaged phone) it runs
the investigator agent on a short complaint and prints:
- the plan the model wrote,
- the tool trace: each call's tool, call id, outcome and latency,
- the evidence: grounded facts with the call id each came from, dropped facts, and
  missing information.

It calls the agent directly. It creates no ticket or run, and writes nothing to the
database (the tools only read). The complaints are made-up examples that match the seeded
scenarios in data/seed/scenarios.json.

Needs, running locally:
- Postgres with the seeded data      (docker compose up -d; uv run python scripts/seed_db.py)
- the mock shipping provider on 8002  (see the README for the command)
- GROQ_API_KEY in .env

    uv run python scripts/run_investigation.py
    uv run python scripts/run_investigation.py --only arjun

Not run in CI or pytest: it calls the real LLM. The model is not fully deterministic, so
plans and traces differ a little between runs.
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from support_agent.agents.investigator import InvestigationOutcome, investigate
from support_agent.core.config import Settings, get_settings
from support_agent.core.logging import configure_logging
from support_agent.db.models import Customer
from support_agent.db.session import get_sessionmaker
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.client import LLMClient
from support_agent.llm.errors import LLMError
from support_agent.llm.providers import build_llm_client
from support_agent.rag.embeddings import FakeEmbedder
from support_agent.tools.base import ToolContext
from support_agent.tools.read_tools import build_read_registry


@dataclass(frozen=True)
class Scenario:
    name: str
    email: str
    order_number: str
    complaint: str


SCENARIOS = [
    Scenario(
        "priya",
        "priya@example.com",
        "ORD-1001",
        "I sent my headphones back over a week ago and the refund has not arrived. "
        "Order ORD-1001. Can you check where it stands?",
    ),
    Scenario(
        "arjun",
        "arjun@example.com",
        "ORD-1002",
        "My laptop (ORD-1002) has said 'in transit' for 8 days. Where is it?",
    ),
    Scenario(
        "meera",
        "meera@example.com",
        "ORD-1003",
        "The phone from order ORD-1003 arrived yesterday with a cracked screen.",
    ),
]


async def customer_id_for(email: str) -> UUID:
    async with get_sessionmaker()() as session:
        customer_id = await session.scalar(select(Customer.id).where(Customer.email == email))
    if customer_id is None:
        sys.exit(f"No customer {email} in the database. Run: uv run python scripts/seed_db.py")
    return customer_id


def show(scenario: Scenario, outcome: InvestigationOutcome) -> None:
    print(f"\n{'=' * 72}\n{scenario.name.upper()}  {scenario.order_number}\n{'=' * 72}")
    print(f"Complaint: {scenario.complaint}\n")

    print("PLAN")
    if outcome.plan is None:
        print("  (none: the planning call failed)")
    else:
        print(f"  Goal: {outcome.plan.goal}")
        for number, step in enumerate(outcome.plan.steps, start=1):
            print(f"  {number}. {step}")

    print("\nTOOL TRACE")
    for record in outcome.trace:
        print(
            f"  {record.call_id:<28} {record.tool:<26} {record.outcome:<20} {record.latency_ms} ms"
        )
    if not outcome.trace:
        print("  (no tool calls)")

    if outcome.failure is not None:
        print(f"\nSTOPPED EARLY: {outcome.failure.value} (the workflow would escalate)")
        return
    evidence = outcome.evidence
    if evidence is None:  # not reachable: no failure means there is evidence
        return
    print("\nEVIDENCE (grounded facts)")
    for fact in evidence.facts:
        print(f"  [{fact.call_id}] {fact.statement}")
    for fact in evidence.dropped_facts:
        print(f"  DROPPED (ungrounded) [{fact.call_id}] {fact.statement}")
    if evidence.missing_information:
        print("\nMISSING INFORMATION")
        for item in evidence.missing_information:
            print(f"  - {item}")
    if evidence.plan_changes:
        print("\nPLAN CHANGES")
        for item in evidence.plan_changes:
            print(f"  - {item}")


async def run_scenario(
    scenario: Scenario, llm: LLMClient, settings: Settings, shipping: ShippingClient
) -> None:
    async with get_sessionmaker()() as session:
        ctx = ToolContext(
            session,
            await customer_id_for(scenario.email),
            settings,
            shipping,
            FakeEmbedder(),  # the investigator has no policy tool, so no real embedder is needed
            clock=lambda: datetime.now(UTC),
        )
        outcome = await investigate(
            llm,
            build_read_registry(),
            ctx,
            complaint_text=scenario.complaint,
            order_number=scenario.order_number,
            max_tool_calls=settings.investigator_max_tool_calls,
        )
    show(scenario, outcome)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--only", choices=[s.name for s in SCENARIOS], help="run one scenario")
    args = parser.parse_args()

    configure_logging("WARNING")  # hide the per-call log lines; the trace below shows them
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    if settings.groq_api_key is None:
        sys.exit("GROQ_API_KEY is not set in .env. Add it and run again.")
    llm = build_llm_client(settings)

    chosen = [s for s in SCENARIOS if args.only in (None, s.name)]
    try:
        async with ShippingClient(settings) as shipping:
            for scenario in chosen:
                await run_scenario(scenario, llm, settings, shipping)
    except LLMError as exc:
        # Our error messages never contain prompts, responses or keys.
        sys.exit(f"LLM call failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
