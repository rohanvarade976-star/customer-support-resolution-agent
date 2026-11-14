"""Saving results and resuming an interrupted eval (evals/store.py, evals/harness.run_plan).

A fake case runner stands in for the workflow: no database, no model.
"""

from pathlib import Path

import pytest

from evals.cases import CaseResult, EvalCase, EvalConfig
from evals.harness import build_plan, run_plan
from evals.store import CorruptResultsError, ResultStore
from support_agent.domain.enums import ResolutionAction

CONFIGS = [EvalConfig.MULTI_AGENT, EvalConfig.BASELINE]


def case(case_id: str) -> EvalCase:
    return EvalCase(
        id=case_id,
        category="normal",
        customer_email="eval.x@example.com",
        order_number="ORD-1",
        complaint="Where is my order?",
        expected_action=ResolutionAction.EXPLAIN_STATUS,
        expected_refund_minor=None,
    )


CASES = [case("a"), case("b"), case("c")]


class SimulatedCrashError(Exception):
    """Plays Ctrl+C or a crash in the middle of a run."""


class FakeRunner:
    """Records what it was asked to run; can crash at a given call or report LLM outages."""

    def __init__(self, crash_at: int | None = None, unavailable: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.crash_at = crash_at
        self.unavailable = unavailable or set()

    async def __call__(self, config: EvalConfig, c: EvalCase, repeat: int) -> CaseResult:
        if self.crash_at is not None and len(self.calls) == self.crash_at:
            raise SimulatedCrashError
        self.calls.append((config.value, c.id, repeat))
        down = c.id in self.unavailable
        return CaseResult(
            config=config,
            case_id=c.id,
            repeat=repeat,
            category=c.category,
            expected_action=c.expected_action,
            expected_refund_minor=None,
            observed_action=ResolutionAction.ESCALATE if down else c.expected_action,
            error_codes=["LLM_UNAVAILABLE"] if down else [],
        )


def test_the_plan_runs_every_config_on_a_case_before_the_next_case() -> None:
    plan = build_plan(CONFIGS, CASES[:2], repeats=2)

    assert [item.key for item in plan] == [
        ("multi_agent", "a", 0),
        ("baseline", "a", 0),
        ("multi_agent", "b", 0),
        ("baseline", "b", 0),
        ("multi_agent", "a", 1),
        ("baseline", "a", 1),
        ("multi_agent", "b", 1),
        ("baseline", "b", 1),
    ]


async def test_an_interrupted_run_resumes_without_redoing_finished_cases(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    plan = build_plan(CONFIGS, CASES, repeats=1)  # 6 runs

    with pytest.raises(SimulatedCrashError):
        await run_plan(plan, store, FakeRunner(crash_at=4))
    assert len(store.load()) == 4  # each finished run was saved before the crash

    resumed = FakeRunner()
    outcome = await run_plan(plan, store, resumed)

    assert resumed.calls == [("multi_agent", "c", 0), ("baseline", "c", 0)]
    assert (outcome.ran, outcome.skipped) == (2, 4)
    assert {r.key for r in store.load()} == {item.key for item in plan}


async def test_a_finished_plan_runs_nothing(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    plan = build_plan(CONFIGS, CASES, repeats=1)
    await run_plan(plan, store, FakeRunner())

    again = FakeRunner()
    outcome = await run_plan(plan, store, again)

    assert again.calls == []
    assert outcome.skipped == 6


async def test_adding_repeats_later_runs_only_the_new_repeats(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    await run_plan(build_plan(CONFIGS, CASES, repeats=1), store, FakeRunner())

    more = FakeRunner()
    await run_plan(build_plan(CONFIGS, CASES, repeats=2), store, more)

    assert {repeat for _, _, repeat in more.calls} == {1}
    assert len(more.calls) == 6


async def test_llm_unavailable_runs_are_saved_but_run_again_on_resume(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    plan = build_plan([EvalConfig.MULTI_AGENT], CASES, repeats=1)
    await run_plan(plan, store, FakeRunner(unavailable={"b"}))
    assert ("multi_agent", "b", 0) not in store.finished_keys()

    retry = FakeRunner()
    await run_plan(plan, store, retry)

    assert retry.calls == [("multi_agent", "b", 0)]
    (b,) = [r for r in store.load() if r.case_id == "b"]
    assert not b.infrastructure_failure  # the newer line wins


async def test_the_run_stops_when_the_models_keep_being_unreachable(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    plan = build_plan([EvalConfig.MULTI_AGENT], CASES, repeats=1)
    runner = FakeRunner(unavailable={"a", "b", "c"})

    outcome = await run_plan(plan, store, runner, stop_after_infra_failures=2)

    assert outcome.stopped_early
    assert len(runner.calls) == 2  # "c" was never attempted


async def test_a_half_written_last_line_is_ignored(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    plan = build_plan([EvalConfig.MULTI_AGENT], CASES[:1], repeats=1)
    await run_plan(plan, store, FakeRunner())
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write('{"config": "multi_agent", "case_id": "b", "rep')  # crash mid-write

    assert [r.case_id for r in store.load()] == ["a"]


def test_a_damaged_line_in_the_middle_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    path.write_text("not json\n{}\n", encoding="utf-8")

    with pytest.raises(CorruptResultsError):
        ResultStore(path).load()


def test_clear_removes_saved_results(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    store.path.write_text("", encoding="utf-8")

    store.clear()

    assert store.load() == []
