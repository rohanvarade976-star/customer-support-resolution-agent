"""Run the real policy agent (Groq, with Gemini fallback if configured) on four scenarios.

For Priya (refund after a return), Rahul (refund long after delivery), Vikram (return that
failed inspection) and Meera (damaged phone) it runs the policy agent on a short complaint
and prints:
- the searches the model made: its query, the policy versions searched, and the passages
  (citations) that came back,
- the rules it stated, each with its citation (rules whose citation no search returned
  are shown as DROPPED),
- the flags: no relevant policy, conflicting passages, ungrounded citations.

The agent states rules only. It never says whether the customer is eligible.

It calls the agent directly. It creates no ticket or run and writes nothing to the
database (search_policy only reads). The complaints are made-up examples that match the
seeded scenarios in data/seed/scenarios.json.

Needs, running locally:
- Postgres with the policies ingested  (uv run python scripts/ingest_policies.py)
- Postgres with the demo data seeded   (uv run python scripts/seed_db.py)
- GROQ_API_KEY in .env
The first run downloads the local embedding model (fastembed).

    uv run python scripts/run_policy_agent.py
    uv run python scripts/run_policy_agent.py --only rahul
    uv run python scripts/run_policy_agent.py --search-tier large --findings-tier small

Not run in CI or pytest: it calls the real LLM. The model is not fully deterministic, so
queries and rules differ a little between runs.
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock
from uuid import UUID

from sqlalchemy import select

from support_agent.agents.policy import PolicyOutcome, research_policy
from support_agent.core.config import Settings, get_settings
from support_agent.core.logging import configure_logging
from support_agent.db.models import Customer
from support_agent.db.session import get_sessionmaker
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.client import LLMClient
from support_agent.llm.errors import LLMError
from support_agent.llm.providers import build_llm_client
from support_agent.llm.types import ModelTier
from support_agent.rag.embeddings import get_embedder
from support_agent.tools.base import ToolContext
from support_agent.tools.read_tools import build_read_registry


@dataclass(frozen=True)
class Scenario:
    name: str
    email: str
    order_number: str
    category: str
    complaint: str


SCENARIOS = [
    Scenario(
        "priya",
        "priya@example.com",
        "ORD-1001",
        "refund",
        "I sent my headphones back over a week ago and the refund has not arrived. "
        "Order ORD-1001. Am I entitled to a refund?",
    ),
    Scenario(
        "rahul",
        "rahul@example.com",
        "ORD-1004",
        "refund",
        "I bought wired earphones two months ago (ORD-1004) and I would like my money back.",
    ),
    Scenario(
        "vikram",
        "vikram@example.com",
        "ORD-1006",
        "refund",
        "I returned my Bluetooth speaker (ORD-1006) and nobody has refunded me. Why not?",
    ),
    Scenario(
        "meera",
        "meera@example.com",
        "ORD-1003",
        "damaged_item",
        "The phone from order ORD-1003 arrived yesterday with a cracked screen.",
    ),
]


async def customer_id_for(email: str) -> UUID:
    async with get_sessionmaker()() as session:
        customer_id = await session.scalar(select(Customer.id).where(Customer.email == email))
    if customer_id is None:
        sys.exit(f"No customer {email} in the database. Run: uv run python scripts/seed_db.py")
    return customer_id


def show(scenario: Scenario, outcome: PolicyOutcome) -> None:
    print(f"\n{'=' * 72}\n{scenario.name.upper()}  {scenario.order_number}\n{'=' * 72}")
    print(f"Category: {scenario.category}\nComplaint: {scenario.complaint}\n")

    print("SEARCHES")
    for search in outcome.searches:
        print(f'  [{search.call_id}] "{search.query}"')
        print(f"      versions searched: {', '.join(search.versions_considered) or '(none)'}")
        if search.no_relevant_evidence:
            print("      no relevant passage found")
        for citation in search.citations:
            print(f"      - {citation}")
    for record in outcome.trace:
        if record.outcome != "ok":
            print(f"  [{record.call_id}] {record.tool} failed: {record.outcome}")
    if not outcome.trace:
        print("  (no searches)")

    if outcome.failure is not None:
        print(f"\nSTOPPED EARLY: {outcome.failure.value} (the workflow would escalate)")
        return
    findings = outcome.findings
    if findings is None:  # not reachable: no failure means there are findings
        return
    print("\nRULES")
    for rule in findings.rules:
        print(f"  {rule.statement}\n      [{rule.citation}]")
    for rule in findings.dropped_rules:
        print(f"  DROPPED (citation never returned by a search) {rule.statement}")
        print(f"      [{rule.citation}]")
    if not findings.rules:
        print("  (none)")
    print(f"\nPOLICY VERSIONS: {', '.join(findings.policy_versions) or '(none)'}")
    print("FLAGS")
    print(f"  no_relevant_policy:   {findings.no_relevant_policy}")
    print(f"  conflicting_passages: {findings.conflicting_passages}")
    print(f"  ungrounded_citations: {findings.ungrounded_citations}")


async def run_scenario(
    scenario: Scenario,
    llm: LLMClient,
    settings: Settings,
    search_tier: ModelTier,
    findings_tier: ModelTier,
) -> None:
    async with get_sessionmaker()() as session:
        ctx = ToolContext(
            session,
            await customer_id_for(scenario.email),
            settings,
            cast(ShippingClient, MagicMock()),  # search_policy never touches shipping
            get_embedder(settings),
            clock=lambda: datetime.now(UTC),
        )
        outcome = await research_policy(
            llm,
            build_read_registry(),
            ctx,
            complaint_text=scenario.complaint,
            category=scenario.category,
            order_number=scenario.order_number,
            max_searches=settings.policy_agent_max_searches,
            search_tier=search_tier,
            findings_tier=findings_tier,
        )
    show(scenario, outcome)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--only", choices=[s.name for s in SCENARIOS], help="run one scenario")
    parser.add_argument(
        "--search-tier", choices=["small", "large"], help="override POLICY_AGENT_SEARCH_TIER"
    )
    parser.add_argument(
        "--findings-tier", choices=["small", "large"], help="override POLICY_AGENT_FINDINGS_TIER"
    )
    args = parser.parse_args()

    configure_logging("WARNING")  # hide the per-call log lines; the output below shows them
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    if settings.groq_api_key is None:
        sys.exit("GROQ_API_KEY is not set in .env. Add it and run again.")
    llm = build_llm_client(settings)
    search_tier: ModelTier = args.search_tier or settings.policy_agent_search_tier
    findings_tier: ModelTier = args.findings_tier or settings.policy_agent_findings_tier

    try:
        for scenario in [s for s in SCENARIOS if args.only in (None, s.name)]:
            await run_scenario(scenario, llm, settings, search_tier, findings_tier)
    except LLMError as exc:
        # Our error messages never contain prompts, responses or keys.
        sys.exit(f"LLM call failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
