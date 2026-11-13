"""Run investigator -> policy agent -> resolution loop with the real models on five scenarios.

For Priya (refund after a return), Rahul (refund long after delivery), Vikram (return that
failed inspection), Meera (damaged phone) and Arjun (late delivery) it runs, in order:
1. the investigator (read tools), 2. the policy agent, 3. the eligibility decision, computed
by code from the database (never by a model), 4. the resolution loop: draft, deterministic
validation, critic, revision (at most RESOLUTION_MAX_REVISIONS), final validation.

It prints each draft with the validator's reason codes and the critic's verdict, then the
final outcome. It creates no ticket or run and writes nothing to the database. The complaints
are made-up examples that match the seeded scenarios in data/seed/scenarios.json.

Needs, running locally:
- Postgres with the policies ingested  (uv run python scripts/ingest_policies.py)
- Postgres with the demo data seeded   (uv run python scripts/seed_db.py)
- the mock shipping provider running (docker compose up -d)
- GROQ_API_KEY in .env
The first run downloads the local embedding model (fastembed).

    uv run python scripts/run_resolution.py
    uv run python scripts/run_resolution.py --only meera

Not run in CI or pytest: it calls the real LLM. The model is not fully deterministic, so
drafts differ a little between runs. The output holds customer text, so do not paste it
into logs.
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from support_agent.agents.investigator import investigate
from support_agent.agents.policy import research_policy
from support_agent.agents.required_evidence import build_delay_facts, ensure_required_evidence
from support_agent.agents.resolution import ResolutionInputs
from support_agent.agents.resolve import Attempt, ResolutionOutcome, resolve
from support_agent.core.config import Settings, get_settings
from support_agent.core.logging import configure_logging
from support_agent.db.models import Customer
from support_agent.db.session import get_sessionmaker
from support_agent.domain.eligibility import EligibilityDecision
from support_agent.domain.enums import TicketCategory
from support_agent.domain.shipping import ShippingDelayFacts
from support_agent.integrations.shipping import ShippingClient
from support_agent.llm.client import LLMClient
from support_agent.llm.errors import LLMError
from support_agent.llm.providers import build_llm_client
from support_agent.rag.embeddings import get_embedder
from support_agent.services import eligibility, order_status
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
    Scenario(
        "arjun",
        "arjun@example.com",
        "ORD-1002",
        "order_status",
        "My laptop (ORD-1002) has said 'in transit' for 8 days. Where is it?",
    ),
]


async def customer_id_for(email: str) -> UUID:
    async with get_sessionmaker()() as session:
        customer_id = await session.scalar(select(Customer.id).where(Customer.email == email))
    if customer_id is None:
        sys.exit(f"No customer {email} in the database. Run: uv run python scripts/seed_db.py")
    return customer_id


def show_decision(decision: EligibilityDecision) -> None:
    reasons = ", ".join(reason.value for reason in decision.reasons) or "none"
    print("\nELIGIBILITY (computed by code)")
    print(f"  eligible: {decision.eligible}   amount: {decision.refundable_amount_minor} paise")
    print(f"  blocked by: {reasons}")


def show_delay_facts(facts: ShippingDelayFacts | None) -> None:
    print("\nSHIPPING FACTS (computed by code)")
    if facts is None:
        print("  no shipment record")
        return
    print(f"  delivered: {facts.delivered}   days past expected: {facts.days_past_expected}")
    print(
        f"  threshold: {facts.investigation_threshold_days}   "
        f"investigation applies: {facts.investigation_applies}"
    )


def show_attempt(number: int, attempt: Attempt) -> None:
    draft = attempt.draft
    print(f"\n  DRAFT {number}: action={draft.action.value}")
    print(f"    rationale: {draft.rationale}")
    print(f"    cited calls: {', '.join(draft.cited_call_ids) or '(none)'}")
    print(f"    cited policy: {', '.join(draft.policy_citations) or '(none)'}")
    print(f"    message: {draft.customer_message}")
    codes = ", ".join(code.value for code in attempt.validation)
    print(f"    VALIDATOR: {codes or 'passed'}")
    if attempt.verdict is None:
        print("    CRITIC: not run")
    else:
        print(f"    CRITIC: {attempt.verdict.verdict}")
        for issue in attempt.verdict.issues:
            print(f"      - {issue}")


def show_outcome(outcome: ResolutionOutcome) -> None:
    print("\nRESOLUTION LOOP")
    for number, attempt in enumerate(outcome.attempts, start=1):
        show_attempt(number, attempt)
    print("\nFINAL OUTCOME (the validator has the last word)")
    print(f"  action: {outcome.action.value}")
    if outcome.amount_minor is not None:
        print(f"  amount: {outcome.amount_minor} paise (from the eligibility decision)")
    print(f"  reason codes: {', '.join(outcome.reason_codes) or 'none'}")
    if outcome.failure is not None:
        print(f"  LLM failure: {outcome.failure.value}")
    print(f"  message to customer: {outcome.message}")


async def run_scenario(
    scenario: Scenario, llm: LLMClient, settings: Settings, shipping: ShippingClient
) -> None:
    print(f"\n{'=' * 72}\n{scenario.name.upper()}  {scenario.order_number}\n{'=' * 72}")
    print(f"Category: {scenario.category}\nComplaint: {scenario.complaint}")
    customer_id = await customer_id_for(scenario.email)
    async with get_sessionmaker()() as session:
        ctx = ToolContext(
            session,
            customer_id,
            settings,
            shipping,
            get_embedder(settings),
            clock=lambda: datetime.now(UTC),
        )
        registry = build_read_registry()
        investigation = await investigate(
            llm,
            registry,
            ctx,
            complaint_text=scenario.complaint,
            order_number=scenario.order_number,
            max_tool_calls=settings.investigator_max_tool_calls,
        )
        if investigation.failure is not None or investigation.evidence is None:
            print(f"\nINVESTIGATION STOPPED: {investigation.failure} (the workflow would escalate)")
            return
        # Code fetches get_order / get_shipment itself if the investigator skipped them.
        investigation = await ensure_required_evidence(
            registry,
            ctx,
            investigation,
            category=TicketCategory(scenario.category),
            order_number=scenario.order_number,
        )
        if investigation.evidence is None:  # unreachable: ensure_required_evidence keeps it
            return
        print(f"\nINVESTIGATION: {len(investigation.evidence.facts)} grounded facts")
        for fact in investigation.evidence.facts:
            print(f"  [{fact.call_id}] {fact.statement}")

        policy = await research_policy(
            llm,
            registry,
            ctx,
            complaint_text=scenario.complaint,
            category=scenario.category,
            order_number=scenario.order_number,
            max_searches=settings.policy_agent_max_searches,
            search_tier=settings.policy_agent_search_tier,
            findings_tier=settings.policy_agent_findings_tier,
        )
        if policy.failure is not None or policy.findings is None:
            print(f"\nPOLICY AGENT STOPPED: {policy.failure} (the workflow would escalate)")
            return
        print(f"\nPOLICY RULES ({', '.join(policy.findings.policy_versions) or 'none'})")
        for rule in policy.findings.rules:
            print(f"  [{rule.citation}] {rule.statement}")

        decision = await eligibility.check_refund_eligibility(
            session, customer_id, scenario.order_number, ctx.clock()
        )
        show_decision(decision)

        threshold = await order_status.get_investigation_threshold(
            session, customer_id, scenario.order_number
        )
        delay_facts = build_delay_facts(investigation.results, ctx.clock(), threshold)
        show_delay_facts(delay_facts)

        outcome = await resolve(
            llm,
            ResolutionInputs(
                complaint_text=scenario.complaint,
                order_number=scenario.order_number,
                evidence=investigation.evidence,
                findings=policy.findings,
                eligibility=decision,
                delay_facts=delay_facts,
            ),
            trace=investigation.trace,
            results=investigation.results,
            max_revisions=settings.resolution_max_revisions,
            delivery_tolerance=timedelta(hours=settings.delivery_date_tolerance_hours),
            critic_tier=settings.critic_tier,
        )
    show_outcome(outcome)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--only", choices=[s.name for s in SCENARIOS], help="run one scenario")
    args = parser.parse_args()

    # Model text may hold characters the Windows console code page cannot print.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    configure_logging("WARNING")  # hide the per-call log lines; the output below shows them
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    if settings.groq_api_key is None:
        sys.exit("GROQ_API_KEY is not set in .env. Add it and run again.")
    llm = build_llm_client(settings)

    try:
        async with ShippingClient(settings) as shipping:
            for scenario in [s for s in SCENARIOS if args.only in (None, s.name)]:
                await run_scenario(scenario, llm, settings, shipping)
    except LLMError as exc:
        # Our error messages never contain prompts, responses or keys.
        sys.exit(f"LLM call failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
