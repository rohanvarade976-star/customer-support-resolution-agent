"""Run the end-to-end resolution eval with the real models and write the reports.

Every golden case in evals/datasets/resolution_cases.json goes through the real workflow
(the worker's path, real models) in each configuration:

    multi_agent             the real system
    multi_agent_no_critic   the same, with the critic off (CRITIC_ENABLED=false for these runs)
    baseline                the single-agent baseline

Results are saved to evals/reports/resolution_results.jsonl as each run finishes. Run the
same command again after an interruption (Ctrl+C, a crash, the daily quota) and it resumes,
skipping finished runs. --fresh starts over. Reports (git-ignored):

    evals/reports/resolution_report.json   every number
    evals/reports/resolution_report.md     for people; the ADR-010 caveat sits under every
                                           multi-agent vs baseline table

Usage (from backend/):
    uv run python scripts/run_eval.py                       # every case, every config
    uv run python scripts/run_eval.py --config multi_agent --only adversarial --limit 3
    uv run python scripts/run_eval.py --only n01-eligible-refund,h04-conflicting-delivery-dates
    uv run python scripts/run_eval.py --repeat 3            # variance across repeats

Needs: Postgres with migrations and ingested policies (scripts/ingest_policies.py) and
GROQ_API_KEY (GEMINI_API_KEY optional, as the fallback). No mock provider needs to be
running: the eval runs the mock carrier in-process. Only the eval-only customers
(eval.*@example.com) are reset and seeded; the demo data is never touched. Refuses to run
when APP_ENV=production.

A full run (40 cases x 3 configs) makes well over a thousand model calls: more than one
day of Groq's free tier. Expect to resume it across days (ADR-016). Not run in CI or pytest.
"""

import argparse
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

# Scripts run with scripts/ on sys.path; the eval package lives next to it in backend/.
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from evals.cases import CaseResult, EvalCase, EvalConfig, load_cases  # noqa: E402
from evals.harness import build_plan, open_harness, run_case, run_plan  # noqa: E402
from evals.report import RunInfo, build_report, render_markdown  # noqa: E402
from evals.store import ResultStore  # noqa: E402
from support_agent.core.config import Settings, get_settings  # noqa: E402
from support_agent.core.demo_anchor import resolve_demo_now  # noqa: E402
from support_agent.core.event_loop import run  # noqa: E402
from support_agent.core.logging import configure_logging  # noqa: E402
from support_agent.db.models import PolicyDocument  # noqa: E402
from support_agent.db.session import get_engine, get_sessionmaker  # noqa: E402

REPORTS_DIR = BACKEND_DIR / "evals" / "reports"
RESULTS_FILE = REPORTS_DIR / "resolution_results.jsonl"
REPORT_JSON = REPORTS_DIR / "resolution_report.json"
REPORT_MD = REPORTS_DIR / "resolution_report.md"
CATEGORIES = ("normal", "hard", "adversarial")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--config",
        action="append",
        choices=[c.value for c in EvalConfig],
        help="a configuration to run (repeat the flag for several; default: all three)",
    )
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated case ids and/or categories (normal, hard, adversarial)",
    )
    parser.add_argument("--limit", type=int, default=None, help="the first N selected cases")
    parser.add_argument("--repeat", type=int, default=1, help="runs per case (default 1)")
    parser.add_argument("--fresh", action="store_true", help="discard saved results first")
    parser.add_argument(
        "--rpm", type=float, default=20.0, help="requests per minute per provider (default 20)"
    )
    parser.add_argument(
        "--tpm", type=float, default=6000.0, help="tokens per minute per provider (default 6000)"
    )
    args = parser.parse_args()
    if args.repeat < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--repeat and --limit must be at least 1")
    if args.rpm <= 0 or args.tpm <= 0:
        parser.error("--rpm and --tpm must be positive")
    return args


def select_cases(cases: list[EvalCase], only: list[str], limit: int | None) -> list[EvalCase]:
    if only:
        known = {c.id for c in cases} | set(CATEGORIES)
        unknown = [o for o in only if o not in known]
        if unknown:
            sys.exit(f"Unknown case id or category: {', '.join(unknown)}")
        cases = [c for c in cases if c.id in only or c.category in only]
    return cases if limit is None else cases[:limit]


def check_ready(settings: Settings) -> None:
    if settings.app_env == "production":
        sys.exit("Refusing to run: APP_ENV is 'production' (the eval resets its own data).")
    if settings.llm_provider == "fake":
        sys.exit("LLM_PROVIDER is 'fake'; this eval needs the real models.")
    if settings.groq_api_key is None:
        sys.exit("GROQ_API_KEY is not set in .env. Add it and run again.")


async def check_policies() -> None:
    async with get_sessionmaker()() as session:
        count = await session.scalar(select(func.count()).select_from(PolicyDocument))
    if not count:
        sys.exit("No policies in the database. Run: uv run python scripts/ingest_policies.py")


def progress(total: int) -> Callable[[CaseResult], None]:
    done = 0

    def show(r: CaseResult) -> None:
        nonlocal done
        done += 1
        if r.infrastructure_failure:
            verdict = "LLM UNAVAILABLE (will retry on resume)"
        else:
            got = r.observed_action.value if r.observed_action else (r.error or "none")
            verdict = f"{got} (expected {r.expected_action.value})"
            verdict += "  ok" if r.observed_action == r.expected_action else "  MISS"
            if r.proposed_refunds_minor:
                verdict += f"  refund proposed {r.proposed_refunds_minor}"
        print(
            f"[{done}/{total}] {r.config.value:<22} {r.case_id:<40} {verdict}  "
            f"{r.llm_calls} calls, {r.active_seconds:.1f}s",
            flush=True,
        )

    return show


async def main() -> None:
    args = parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    configure_logging("WARNING")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    check_ready(settings)

    configs = [EvalConfig(c) for c in (args.config or [c.value for c in EvalConfig])]
    configs = list(dict.fromkeys(configs))
    only = [o.strip() for o in args.only.split(",") if o.strip()]
    all_cases = load_cases()
    cases = select_cases(all_cases, only, args.limit)
    plan = build_plan(configs, cases, args.repeat)

    store = ResultStore(RESULTS_FILE)
    if args.fresh:
        store.clear()
    finished = store.finished_keys()
    todo = sum(item.key not in finished for item in plan)
    print(f"{len(plan)} runs planned, {len(plan) - todo} already finished, {todo} to run.")

    now = resolve_demo_now(settings.demo_anchor_date, datetime.now(UTC))
    stopped_early = False
    try:
        await check_policies()
        if todo:
            async with open_harness(settings, now=now, rpm=args.rpm, tpm=args.tpm) as env:

                async def one(config: EvalConfig, case: EvalCase, repeat: int) -> CaseResult:
                    return await run_case(env, config, case, repeat)

                outcome = await run_plan(plan, store, one, on_result=progress(todo))
                stopped_early = outcome.stopped_early
    finally:
        await get_engine().dispose()

    planned = {item.key for item in plan}
    results = [r for r in store.load() if r.key in planned]
    report = build_report(
        RunInfo(
            generated_at=datetime.now(UTC),
            dataset_cases=len(cases),
            configs=configs,
            repeats=args.repeat,
            only=only,
            limit=args.limit,
            models={
                "groq small": settings.groq_small_model,
                "groq large": settings.groq_large_model,
                "gemini small": settings.gemini_small_model,
                "gemini large": settings.gemini_large_model,
            },
            rpm=args.rpm,
            tpm=args.tpm,
            stopped_early=stopped_early,
        ),
        results,
    )
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    # newline="\n": the repo uses LF; Windows would otherwise write CRLF.
    REPORT_JSON.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    REPORT_MD.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    for m in report.configs:
        print(
            f"{m.config.value:<22} unsafe refunds {m.unsafe_refunds}   strict "
            f"{m.accuracy.strict_correct}/{m.accuracy.runs}   lenient "
            f"{m.accuracy.lenient_correct}/{m.accuracy.runs}"
        )
    if stopped_early:
        print("Stopped early: the models kept being unreachable. Run the same command later.")
    print(f"Reports: {REPORT_MD.relative_to(BACKEND_DIR)}, {REPORT_JSON.relative_to(BACKEND_DIR)}")


if __name__ == "__main__":
    run(main())  # a selector event loop, which psycopg needs on Windows
