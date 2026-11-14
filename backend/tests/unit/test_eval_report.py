"""The Markdown report (evals/report.py): the ADR-010 caveat sits under every comparison."""

from datetime import UTC, datetime

from evals.cases import CaseResult, EvalConfig
from evals.report import ADR_010_CAVEAT, RunInfo, build_report, render_markdown
from support_agent.domain.enums import ResolutionAction


def run_info(configs: list[EvalConfig]) -> RunInfo:
    return RunInfo(
        generated_at=datetime(2026, 11, 14, tzinfo=UTC),
        dataset_cases=2,
        configs=configs,
        repeats=1,
        only=[],
        limit=None,
        models={"groq large": "test-model"},
        rpm=20,
        tpm=6000,
        stopped_early=False,
    )


def result(config: EvalConfig, case_id: str, observed: ResolutionAction) -> CaseResult:
    return CaseResult(
        config=config,
        case_id=case_id,
        repeat=0,
        category="normal",
        expected_action=ResolutionAction.EXPLAIN_STATUS,
        expected_refund_minor=None,
        acceptable_alternatives=[ResolutionAction.ESCALATE],
        observed_action=observed,
    )


def tables_and_what_follows(markdown: str) -> list[str]:
    """For each table, the first non-blank line after it."""
    lines = markdown.splitlines()
    after: list[str] = []
    for i, line in enumerate(lines):
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        if line.startswith("|") and not next_line.startswith("|"):
            rest = [x for x in lines[i + 1 :] if x.strip()]
            after.append(rest[0] if rest else "")
    return after


def test_every_table_comparing_with_baseline_is_followed_by_the_caveat() -> None:
    configs = [EvalConfig.MULTI_AGENT, EvalConfig.BASELINE]
    results = [
        result(EvalConfig.MULTI_AGENT, "a", ResolutionAction.EXPLAIN_STATUS),
        result(EvalConfig.BASELINE, "a", ResolutionAction.ESCALATE),
    ]

    markdown = render_markdown(build_report(run_info(configs), results))

    following = tables_and_what_follows(markdown)
    assert len(following) >= 8
    assert all(line == ADR_010_CAVEAT for line in following)
    assert "Synthetic data" in markdown


def test_a_single_configuration_has_no_comparison_caveat_under_its_tables() -> None:
    configs = [EvalConfig.MULTI_AGENT]
    results = [result(EvalConfig.MULTI_AGENT, "a", ResolutionAction.EXPLAIN_STATUS)]

    markdown = render_markdown(build_report(run_info(configs), results))

    assert ADR_010_CAVEAT not in tables_and_what_follows(markdown)


def test_cases_passed_through_an_alternative_are_listed_by_name() -> None:
    results = [result(EvalConfig.BASELINE, "h10-asks-for-exception", ResolutionAction.ESCALATE)]

    markdown = render_markdown(build_report(run_info([EvalConfig.BASELINE]), results))

    assert (
        "`baseline` h10-asks-for-exception (repeat 0): expected `explain_status`, got `escalate`"
        in markdown
    )
    assert "| `baseline` | 0/1 (0%) | 1/1 (100%) |" in markdown


def test_results_on_different_cases_are_flagged_as_not_like_for_like() -> None:
    configs = [EvalConfig.MULTI_AGENT, EvalConfig.BASELINE]
    results = [
        result(EvalConfig.MULTI_AGENT, "a", ResolutionAction.EXPLAIN_STATUS),
        result(EvalConfig.MULTI_AGENT, "b", ResolutionAction.EXPLAIN_STATUS),
        result(EvalConfig.BASELINE, "a", ResolutionAction.EXPLAIN_STATUS),
    ]

    markdown = render_markdown(build_report(run_info(configs), results))

    assert "Not like for like" in markdown
