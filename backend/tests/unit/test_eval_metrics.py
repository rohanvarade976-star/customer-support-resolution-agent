"""The eval's scoring (evals/metrics.py): pure functions over hand-built results."""

from typing import Any

from evals.cases import CaseResult, EvalConfig
from evals.metrics import (
    accepted_with_validator_codes,
    config_metrics,
    escalation,
    grounding,
    is_unsafe_refund,
    lenient_correct,
    ratio,
    strict_correct,
    used_alternative,
    variance,
)
from support_agent.domain.enums import ResolutionAction

REFUND = ResolutionAction.REFUND
EXPLAIN = ResolutionAction.EXPLAIN_STATUS
INFO = ResolutionAction.REQUEST_INFO
ESCALATE = ResolutionAction.ESCALATE


def result(
    expected: ResolutionAction,
    observed: ResolutionAction | None,
    *,
    case_id: str = "c1",
    config: EvalConfig = EvalConfig.MULTI_AGENT,
    **fields: Any,
) -> CaseResult:
    return CaseResult(
        config=config,
        case_id=case_id,
        repeat=fields.pop("repeat", 0),
        category=fields.pop("category", "normal"),
        expected_action=expected,
        expected_refund_minor=1000 if expected == REFUND else None,
        observed_action=observed,
        **fields,
    )


# ---------------------------------------------------------------- unsafe refunds


def test_a_refund_at_the_expected_amount_is_safe() -> None:
    assert not is_unsafe_refund(result(REFUND, REFUND, proposed_refunds_minor=[1000]))


def test_a_refund_at_a_different_amount_is_unsafe() -> None:
    assert is_unsafe_refund(result(REFUND, REFUND, proposed_refunds_minor=[999]))


def test_any_refund_when_none_was_expected_is_unsafe() -> None:
    # Judged on the stored proposal rows, whatever the run's final action says.
    assert is_unsafe_refund(result(EXPLAIN, ESCALATE, proposed_refunds_minor=[1000]))


def test_two_proposals_in_one_run_are_unsafe_even_at_the_right_amount() -> None:
    assert is_unsafe_refund(result(REFUND, REFUND, proposed_refunds_minor=[1000, 1000]))


def test_no_proposal_is_never_unsafe() -> None:
    assert not is_unsafe_refund(result(REFUND, ESCALATE))


# ---------------------------------------------------------------- strict vs lenient


def test_strict_needs_the_exact_action_lenient_also_takes_an_alternative() -> None:
    alt = result(EXPLAIN, ESCALATE, acceptable_alternatives=[ESCALATE])

    assert not strict_correct(alt)
    assert lenient_correct(alt)
    assert used_alternative(alt)


def test_an_action_outside_the_alternatives_is_wrong_both_ways() -> None:
    wrong = result(EXPLAIN, INFO, acceptable_alternatives=[ESCALATE])

    assert not strict_correct(wrong)
    assert not lenient_correct(wrong)
    assert not used_alternative(wrong)


def test_a_run_with_no_action_is_wrong_both_ways() -> None:
    failed = result(EXPLAIN, None, error="RUN_FAILED", acceptable_alternatives=[ESCALATE])

    assert not lenient_correct(failed)


def test_alternatives_are_listed_and_never_raise_strict_accuracy() -> None:
    results = [
        result(EXPLAIN, EXPLAIN, case_id="a"),
        result(EXPLAIN, ESCALATE, case_id="b", acceptable_alternatives=[ESCALATE]),
        result(REFUND, REFUND, case_id="c", proposed_refunds_minor=[1000]),
        result(ESCALATE, EXPLAIN, case_id="d"),
    ]

    m = config_metrics(EvalConfig.MULTI_AGENT, results)

    assert (m.accuracy.strict_correct, m.accuracy.lenient_correct, m.accuracy.runs) == (2, 3, 4)
    assert m.accuracy.strict == 0.5
    assert m.accuracy.lenient == 0.75
    assert [(a.case_id, a.expected, a.observed) for a in m.alternatives_used] == [
        ("b", EXPLAIN, ESCALATE)
    ]


# ---------------------------------------------------------------- escalation


def test_escalation_precision_and_recall() -> None:
    results = [
        result(ESCALATE, ESCALATE, case_id="tp"),
        result(ESCALATE, EXPLAIN, case_id="fn"),
        result(EXPLAIN, ESCALATE, case_id="fp1"),
        result(INFO, ESCALATE, case_id="fp2"),
        result(EXPLAIN, EXPLAIN, case_id="tn"),
    ]

    e = escalation(results)

    assert (e.true_positives, e.predicted, e.expected) == (1, 3, 2)
    assert e.precision == 0.333
    assert e.recall == 0.5


def test_empty_denominators_give_none_not_a_crash() -> None:
    e = escalation([result(EXPLAIN, EXPLAIN)])

    assert e.precision is None
    assert e.recall is None
    assert ratio(0, 0) is None


# ---------------------------------------------------------------- grounding


def test_caught_validator_codes_are_counted_by_kind() -> None:
    r = result(
        REFUND,
        REFUND,
        proposed_refunds_minor=[1000],
        validation_codes=[
            ["CITATION_NOT_GROUNDED", "UNVERIFIED_CLAIM", "UNVERIFIED_AMOUNT"],
            [],
        ],
    )

    g = grounding([r, result(EXPLAIN, EXPLAIN)])

    assert g.citation_issues_caught == 1
    assert g.unverified_claims_caught == 2
    assert g.runs_with_issue_caught == 1
    assert g.accepted_with_validator_codes == 0


def test_acting_on_a_draft_that_still_has_codes_is_flagged() -> None:
    bad = result(EXPLAIN, EXPLAIN, validation_codes=[["UNVERIFIED_CLAIM"]])
    escalated = result(ESCALATE, ESCALATE, validation_codes=[["UNVERIFIED_CLAIM"]])

    assert accepted_with_validator_codes(bad)
    assert not accepted_with_validator_codes(escalated)  # escalating on codes is correct


def test_leaks_are_counted() -> None:
    g = grounding([result(INFO, INFO, leaked_terms=["Ananya"]), result(INFO, INFO)])

    assert g.runs_with_leaks == 1


# ---------------------------------------------------------------- cost, variance, exclusions


def test_cost_spread_per_config() -> None:
    results = [
        result(EXPLAIN, EXPLAIN, case_id="a", llm_calls=4, input_tokens=100, active_seconds=2.0),
        result(EXPLAIN, EXPLAIN, case_id="b", llm_calls=8, input_tokens=300, active_seconds=4.0),
        result(EXPLAIN, EXPLAIN, case_id="c", config=EvalConfig.BASELINE, llm_calls=99),
    ]

    m = config_metrics(EvalConfig.MULTI_AGENT, results)

    assert m.cost.llm_calls.total == 12
    assert m.cost.llm_calls.mean == 6
    assert m.cost.input_tokens.max == 300
    assert m.cost.active_seconds.median == 3.0


def test_variance_across_repeats() -> None:
    results = [
        result(EXPLAIN, EXPLAIN, case_id="a", repeat=0),
        result(EXPLAIN, EXPLAIN, case_id="b", repeat=0),
        result(EXPLAIN, EXPLAIN, case_id="a", repeat=1),
        result(EXPLAIN, ESCALATE, case_id="b", repeat=1),
    ]

    v = variance(results)

    assert v.repeats == 2
    assert v.strict_accuracy_per_repeat == [1.0, 0.5]
    assert v.strict_accuracy_stdev == 0.354
    assert v.inconsistent_cases == ["b"]


def test_one_repeat_has_no_stdev() -> None:
    assert variance([result(EXPLAIN, EXPLAIN)]).strict_accuracy_stdev is None


def test_llm_unavailable_runs_are_excluded_from_scoring() -> None:
    results = [
        result(EXPLAIN, EXPLAIN, case_id="a"),
        result(EXPLAIN, ESCALATE, case_id="b", error_codes=["LLM_UNAVAILABLE"]),
    ]

    m = config_metrics(EvalConfig.MULTI_AGENT, results)

    assert m.runs == 1
    assert m.excluded_infrastructure == 1
    assert m.accuracy.strict == 1.0


def test_resolution_split_and_category_breakdown() -> None:
    results = [
        result(EXPLAIN, EXPLAIN, case_id="a", category="normal"),
        result(REFUND, REFUND, case_id="b", category="hard", proposed_refunds_minor=[1000]),
        result(ESCALATE, ESCALATE, case_id="c", category="adversarial"),
        result(EXPLAIN, REFUND, case_id="d", category="adversarial", proposed_refunds_minor=[5]),
    ]

    m = config_metrics(EvalConfig.MULTI_AGENT, results)

    split = m.resolution_split
    assert (split.automated, split.human_assisted, split.handed_off) == (1, 1, 1)
    assert m.unsafe_refunds == 1
    assert m.unsafe_refund_cases == ["d"]
    assert m.unsafe_refunds_by_category == {"adversarial": 1, "hard": 0, "normal": 0}
    assert m.accuracy_by_category["adversarial"].strict == 0.5
