"""The policy agent: search loop -> grounded rules, with the fake LLM and fake tools."""

from collections.abc import Awaitable, Callable
from datetime import date

from support_agent.agents.fake import findings_reply, search_call
from support_agent.agents.policy import (
    POLICY_TOOLS,
    PolicyFailure,
    PolicyOutcome,
    load_prompt,
    research_policy,
)
from support_agent.llm.client import RetrySettings, RoutedLLMClient
from support_agent.llm.fake import FakeProvider, ScriptStep, text_reply, tool_reply, transient
from support_agent.schemas.policies import PolicyPassage, PolicySearchResult
from support_agent.tools.base import Tool, ToolContext, ToolRegistry
from support_agent.tools.fake import FakeRecord, fake_tool_context
from support_agent.tools.policy_tools import SearchPolicyInput
from support_agent.tools.read_tools import OrderInput, build_read_registry

ORDER = "ORD-1001"
COMPLAINT = "I want a refund. SECRET-COMPLAINT-TEXT"
RULE_V1 = "refund_policy_v1:section_2"
RULE_V2 = "refund_policy_v2:section_3"


def passage(
    citation: str, version: str = "v2", content: str = "Returns within 30 days."
) -> PolicyPassage:
    section = citation.split(":")[1]
    return PolicyPassage(
        citation=citation,
        policy_key="refund_policy",
        policy_title="Refund Policy",
        version=version,
        section=section,
        section_title="Return window",
        effective_from=date(2026, 7, 1),
        effective_to=None,
        content=content,
        vector_similarity=0.8,
        keyword_rank=1.0,
        rrf_score=0.03,
    )


class FakePolicySearch:
    """A registry with a canned search_policy, plus tools the policy agent must not reach."""

    def __init__(self, *passages: PolicyPassage) -> None:
        self.passages = list(passages)
        self.searches: list[SearchPolicyInput] = []
        self.other_calls: list[str] = []
        self.registry = ToolRegistry()
        self.registry.register(
            Tool(
                "search_policy", "Fake search.", SearchPolicyInput, PolicySearchResult, self._search
            )
        )
        for name in ("get_order", "execute_approved_refund"):
            self.registry.register(
                Tool(name, f"Fake {name}.", OrderInput, FakeRecord, self._other(name))
            )

    async def _search(self, ctx: ToolContext, args: SearchPolicyInput) -> PolicySearchResult:
        self.searches.append(args)
        return PolicySearchResult(
            as_of_date=date(2026, 11, 1),
            order_number=args.order_number,
            versions_considered=["refund_policy v2"],
            no_relevant_evidence=not self.passages,
            passages=self.passages,
        )

    def _other(self, name: str) -> Callable[[ToolContext, OrderInput], Awaitable[FakeRecord]]:
        async def handle(ctx: ToolContext, args: OrderInput) -> FakeRecord:
            self.other_calls.append(name)
            return FakeRecord(tool=name, order_number=args.order_number, detail="x")

        return handle


async def run_agent(
    script: list[ScriptStep], search: FakePolicySearch, *, max_searches: int = 4
) -> tuple[PolicyOutcome, FakeProvider]:
    fake = FakeProvider(script)
    client = RoutedLLMClient(
        [fake],
        RetrySettings(max_attempts=1, backoff_base_seconds=0, backoff_max_seconds=0),
        default_temperature=0.0,
        default_max_tokens=256,
    )
    outcome = await research_policy(
        client,
        search.registry,
        fake_tool_context(),
        complaint_text=COMPLAINT,
        category="refund",
        order_number=ORDER,
        max_searches=max_searches,
        tier="small",
    )
    return outcome, fake


# ---------------------------------------------------------------- grounded rules


async def test_grounded_rules_are_kept_with_versions_taken_from_the_citations() -> None:
    search = FakePolicySearch(passage(RULE_V2))
    outcome, fake = await run_agent(
        [
            search_call("how long to return", ORDER, "s1"),
            text_reply("Enough."),
            findings_reply([("Returns are accepted within 30 days of delivery.", RULE_V2)]),
        ],
        search,
    )

    assert outcome.failure is None and outcome.findings is not None
    assert [(r.statement, r.citation) for r in outcome.findings.rules] == [
        ("Returns are accepted within 30 days of delivery.", RULE_V2)
    ]
    assert outcome.findings.policy_versions == ["refund_policy v2"]
    assert not outcome.findings.no_relevant_policy
    assert not outcome.findings.ungrounded_citations
    assert [s.order_number for s in search.searches] == [ORDER]
    [made] = outcome.searches
    assert (made.query, made.citations) == ("how long to return", [RULE_V2])
    assert all(request.tier == "small" for request in fake.requests)
    assert fake.requests[-1].forced_tool == "policy_findings_draft"


async def test_a_citation_no_search_returned_is_dropped_and_flagged() -> None:
    search = FakePolicySearch(passage(RULE_V2))
    outcome, _ = await run_agent(
        [
            search_call("return window", ORDER, "s1"),
            text_reply("Done."),
            findings_reply(
                [
                    ("Returns are accepted within 30 days.", RULE_V2),
                    ("Refunds are always automatic.", "refund_policy_v2:section_99"),
                ]
            ),
        ],
        search,
    )

    findings = outcome.findings
    assert findings is not None
    assert [r.citation for r in findings.rules] == [RULE_V2]
    assert [r.citation for r in findings.dropped_rules] == ["refund_policy_v2:section_99"]
    assert findings.ungrounded_citations
    assert not findings.no_relevant_policy


async def test_a_citation_from_another_run_is_not_grounded_without_a_search() -> None:
    # The model never searched, so even a real-looking chunk_key has no passage behind it.
    outcome, _ = await run_agent(
        [text_reply("I know this one."), findings_reply([("Returns within 15 days.", RULE_V1)])],
        FakePolicySearch(passage(RULE_V2)),
    )

    findings = outcome.findings
    assert findings is not None and findings.rules == []
    assert findings.ungrounded_citations and findings.no_relevant_policy
    assert outcome.searches == []


async def test_no_relevant_policy_is_flagged_when_search_finds_nothing() -> None:
    search = FakePolicySearch()  # no passages: no_relevant_evidence is true
    outcome, _ = await run_agent(
        [
            search_call("battery swelling", ORDER, "s1"),
            text_reply("Nothing."),
            findings_reply(no_relevant_policy=True),
        ],
        search,
    )

    findings = outcome.findings
    assert findings is not None
    assert findings.no_relevant_policy and findings.rules == []
    assert findings.policy_versions == []
    assert [s.no_relevant_evidence for s in outcome.searches] == [True]


async def test_code_sets_no_relevant_policy_even_if_the_model_denies_it() -> None:
    search = FakePolicySearch()
    outcome, _ = await run_agent(
        [
            search_call("anything", ORDER, "s1"),
            text_reply("Done."),
            findings_reply([("Everything is refundable.", RULE_V2)], no_relevant_policy=False),
        ],
        search,
    )

    assert outcome.findings is not None and outcome.findings.no_relevant_policy


async def test_conflicting_passages_flag_is_passed_through() -> None:
    search = FakePolicySearch(passage(RULE_V1, "v1"), passage(RULE_V2, "v2"))
    outcome, _ = await run_agent(
        [
            search_call("return window", ORDER, "s1"),
            text_reply("Done."),
            findings_reply(
                [("Return window is 15 days.", RULE_V1), ("Return window is 30 days.", RULE_V2)],
                conflicting_passages=True,
            ),
        ],
        search,
    )

    findings = outcome.findings
    assert findings is not None and findings.conflicting_passages
    assert findings.policy_versions == ["refund_policy v1", "refund_policy v2"]


# ---------------------------------------------------------------- the allowlist


def test_the_allowlist_is_exactly_search_policy() -> None:
    assert POLICY_TOOLS == ("search_policy",)


def test_the_real_registry_gives_the_policy_agent_no_read_or_write_tools() -> None:
    full = build_read_registry()
    assert len(full.names) > 1  # the read tools exist, but not for this agent

    assert build_read_registry().restricted(POLICY_TOOLS).names == ["search_policy"]


async def test_model_is_only_offered_search_policy_and_cannot_use_other_tools() -> None:
    search = FakePolicySearch(passage(RULE_V2))
    outcome, fake = await run_agent(
        [
            search_call("return window", ORDER, "s1"),
            # Both are registered in the registry, but not in the policy agent's allowlist.
            tool_reply("get_order", {"order_number": ORDER}, call_id="r1"),
            tool_reply("execute_approved_refund", {"order_number": ORDER}, call_id="r2"),
            text_reply("Done."),
            findings_reply([("Returns within 30 days.", RULE_V2)]),
        ],
        search,
    )

    loop_request = fake.requests[0]
    assert [spec.name for spec in loop_request.tools] == ["search_policy"]
    assert search.other_calls == []
    assert [r.outcome for r in outcome.trace] == ["ok", "TOOL_NOT_ALLOWED", "TOOL_NOT_ALLOWED"]
    assert outcome.findings is not None and len(outcome.findings.rules) == 1


# ---------------------------------------------------------------- limits, failures, safety


async def test_search_limit_stops_the_run() -> None:
    search = FakePolicySearch(passage(RULE_V2))
    outcome, _ = await run_agent(
        [search_call(f"query number {n}", ORDER, f"s{n}") for n in range(1, 4)],
        search,
        max_searches=2,
    )

    assert outcome.failure is PolicyFailure.STEP_LIMIT and outcome.findings is None
    assert len(search.searches) == 2


async def test_llm_outage_comes_back_as_a_failure_not_an_exception() -> None:
    outcome, _ = await run_agent([transient()], FakePolicySearch())

    assert outcome.failure is PolicyFailure.LLM_UNAVAILABLE and outcome.findings is None


async def test_complaint_is_data_and_the_prompt_forbids_eligibility_decisions() -> None:
    _, fake = await run_agent(
        [text_reply("Nothing to search."), findings_reply(no_relevant_policy=True)],
        FakePolicySearch(),
    )

    for request in fake.requests:
        system = [m for m in request.messages if m.role == "system"]
        assert len(system) == 1 and request.messages[0].role == "system"
        assert "SECRET-COMPLAINT-TEXT" not in system[0].content
        user_text = "\n".join(m.content for m in request.messages if m.role == "user")
        assert 'trust="untrusted_data"' in user_text and "SECRET-COMPLAINT-TEXT" in user_text
        assert f"Order number for this case: {ORDER}" in user_text
    assert "Never say whether this customer is eligible" in load_prompt()
