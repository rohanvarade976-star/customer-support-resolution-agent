"""An independent check that the money records are sound (ADR-015).

It reads the tables (ours, and the mock payment provider's when it is in the same database)
and reports every broken rule as a `Violation`: a code plus the ids involved, never any
customer data. It changes nothing and calls no service, so a bug in the refund code cannot
hide itself from it. The crash, race and chaos tests run it after every scenario, and
scripts/check_invariants.py runs it against a real database.

The rules:

- REFUNDS_EXCEED_PAID: per order, refunds that did not fail add up to at most the paid amount.
- REFUND_NOT_APPROVED: every refund has an approved approval whose hash matches the hash of
  its proposal's payload, recomputed here (so an edited payload is caught too).
- REFUND_DIFFERS_FROM_PROPOSAL: the refund's amount and order are the approved ones.
- DUPLICATE_REFUND_FOR_PROPOSAL: at most one refund per proposal.
- PROVIDER_*: our refunds and the provider's agree by idempotency key, reference, amount and
  outcome, with no orphans on either side.
- TICKET_RESOLVED_WITHOUT_REFUND: a refund ticket is resolved only if its refund succeeded,
  at the provider too.
- TICKET_BEHIND_REFUND: a succeeded refund's ticket is resolved, unless a person owns the run.
- REFUND_UNWATCHED / RUN_STUCK_EXECUTING: nothing unsettled is left without someone (the
  reconciler, a job, or a person) responsible for finishing it.

The last three are about time ("left without"), so they skip rows changed within the grace
period (`INVARIANT_GRACE_SECONDS`): on a live system those may simply be in flight.
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.seed import HISTORY_KEY_PREFIX
from support_agent.domain.enums import RefundOperationStatus
from support_agent.domain.proposals import payload_hash
from support_agent.domain.refund_outcomes import ACCEPTED_STATUSES
from support_agent.repositories import invariants as queries
from support_agent.repositories.invariants import ProviderRefund, RefundFacts


class InvariantCode(StrEnum):
    REFUNDS_EXCEED_PAID = "REFUNDS_EXCEED_PAID"
    REFUND_NOT_APPROVED = "REFUND_NOT_APPROVED"
    REFUND_DIFFERS_FROM_PROPOSAL = "REFUND_DIFFERS_FROM_PROPOSAL"
    DUPLICATE_REFUND_FOR_PROPOSAL = "DUPLICATE_REFUND_FOR_PROPOSAL"
    PROVIDER_REFUND_MISSING = "PROVIDER_REFUND_MISSING"
    PROVIDER_REFUND_ORPHAN = "PROVIDER_REFUND_ORPHAN"
    PROVIDER_AMOUNT_MISMATCH = "PROVIDER_AMOUNT_MISMATCH"
    PROVIDER_REFERENCE_MISMATCH = "PROVIDER_REFERENCE_MISMATCH"
    PROVIDER_OUTCOME_MISMATCH = "PROVIDER_OUTCOME_MISMATCH"
    TICKET_RESOLVED_WITHOUT_REFUND = "TICKET_RESOLVED_WITHOUT_REFUND"
    TICKET_BEHIND_REFUND = "TICKET_BEHIND_REFUND"
    REFUND_UNWATCHED = "REFUND_UNWATCHED"
    RUN_STUCK_EXECUTING = "RUN_STUCK_EXECUTING"


@dataclass(frozen=True)
class Violation:
    code: InvariantCode
    ids: dict[str, str]  # e.g. {"refund_id": "..."}: ids and keys only, no customer data


async def check_money_invariants(session: AsyncSession, *, grace_seconds: int) -> list[Violation]:
    """Every violated rule, in a stable order. Empty: the money records are sound."""
    found: list[Violation] = []

    def add(code: InvariantCode, **ids: object) -> None:
        found.append(Violation(code, {name: str(value) for name, value in ids.items()}))

    for order_id in await queries.over_refunded_orders(session):
        add(InvariantCode.REFUNDS_EXCEED_PAID, order_id=order_id)

    facts = await queries.refund_facts(session)
    for fact in facts:
        if not _approved(fact):
            add(
                InvariantCode.REFUND_NOT_APPROVED,
                refund_id=fact.refund_id,
                proposal_id=fact.proposal_id,
            )
        if not _matches_proposal(fact):
            add(InvariantCode.REFUND_DIFFERS_FROM_PROPOSAL, refund_id=fact.refund_id)

    for proposal_id in await queries.proposals_with_several_refunds(session):
        add(InvariantCode.DUPLICATE_REFUND_FOR_PROPOSAL, proposal_id=proposal_id)

    provider = await queries.provider_refunds(session)
    at_provider: dict[str, ProviderRefund] | None = None
    if provider is not None:  # the provider's table is in this database
        at_provider = {row.idempotency_key: row for row in provider}
        found.extend(_provider_disagreements(facts, provider, at_provider))

    for resolved in await queries.resolved_ticket_refunds(session):
        if not _confirmed_success(resolved.status, resolved.idempotency_key, at_provider):
            add(
                InvariantCode.TICKET_RESOLVED_WITHOUT_REFUND,
                ticket_id=resolved.ticket_id,
                refund_id=resolved.refund_id,
            )
    for ticket_id in await queries.tickets_behind_refunds(session, grace_seconds):
        add(InvariantCode.TICKET_BEHIND_REFUND, ticket_id=ticket_id)
    for refund_id in await queries.unwatched_refunds(session, grace_seconds):
        add(InvariantCode.REFUND_UNWATCHED, refund_id=refund_id)
    for run_id in await queries.stuck_executing_runs(session, grace_seconds):
        add(InvariantCode.RUN_STUCK_EXECUTING, run_id=run_id)
    return found


def _approved(fact: RefundFacts) -> bool:
    """The stored payload still hashes to its recorded hash, and an approval approved it."""
    digest = payload_hash(fact.proposal_payload)
    return digest == fact.proposal_payload_hash and digest in fact.approved_hashes


def _matches_proposal(fact: RefundFacts) -> bool:
    payload = fact.proposal_payload
    return payload.get("amount_minor") == fact.amount_minor and payload.get("order_id") == str(
        fact.order_id
    )


def _is_history(idempotency_key: str) -> bool:
    """A seeded past refund, made before the provider existed: the provider cannot know it."""
    return idempotency_key.startswith(HISTORY_KEY_PREFIX)


def _provider_disagreements(
    facts: list[RefundFacts],
    provider: list[ProviderRefund],
    at_provider: dict[str, ProviderRefund],
) -> list[Violation]:
    found: list[Violation] = []
    ours = {fact.idempotency_key for fact in facts}
    for refund in provider:
        if refund.idempotency_key not in ours:  # money the provider holds that we never asked for
            found.append(
                Violation(
                    InvariantCode.PROVIDER_REFUND_ORPHAN,
                    {"provider_reference": refund.provider_reference},
                )
            )
    for fact in facts:
        if _is_history(fact.idempotency_key):
            continue
        ids = {"refund_id": str(fact.refund_id)}
        row = at_provider.get(fact.idempotency_key)
        if row is None:
            # Reserved or pending reconciliation may legitimately have no provider record.
            if fact.status in ACCEPTED_STATUSES or fact.provider_reference is not None:
                found.append(Violation(InvariantCode.PROVIDER_REFUND_MISSING, ids))
            continue
        if row.amount_minor != fact.amount_minor:
            found.append(Violation(InvariantCode.PROVIDER_AMOUNT_MISMATCH, ids))
        if fact.provider_reference not in (None, row.provider_reference):
            found.append(Violation(InvariantCode.PROVIDER_REFERENCE_MISMATCH, ids))
        if _outcomes_disagree(fact.status, row.final_outcome):
            found.append(Violation(InvariantCode.PROVIDER_OUTCOME_MISMATCH, ids))
    return found


def _outcomes_disagree(status: RefundOperationStatus, final_outcome: str) -> bool:
    """We say succeeded and it fails, or we say failed while the provider pays it out."""
    if status == RefundOperationStatus.SUCCEEDED:
        return final_outcome != "succeeded"
    if status == RefundOperationStatus.FAILED:
        return final_outcome == "succeeded"
    return False  # still in progress on our side: any provider outcome is possible


def _confirmed_success(
    status: RefundOperationStatus,
    idempotency_key: str,
    at_provider: dict[str, ProviderRefund] | None,
) -> bool:
    if status != RefundOperationStatus.SUCCEEDED:
        return False
    if at_provider is None or _is_history(idempotency_key):
        return True  # no provider table to ask: our own verified status is all there is
    row = at_provider.get(idempotency_key)
    return row is not None and row.final_outcome == "succeeded"
