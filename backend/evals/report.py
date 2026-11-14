"""The eval report, as JSON (every number) and Markdown (for people). Pure: no I/O.

Any Markdown table that shows a multi-agent configuration next to the baseline is followed
by the ADR-010 caveat, by construction (`_table`), so no comparison number can appear
without it.
"""

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel

from evals.cases import CaseResult, EvalConfig
from evals.metrics import Accuracy, ConfigMetrics, Spread, compute_all

SYNTHETIC_LABEL = (
    "Synthetic data, synthetic customers. These results describe this eval set, "
    "not production performance."
)
ADR_010_CAVEAT = (
    "**Caveat (ADR-010):** `baseline` differs from `multi_agent` in two ways at once: "
    "specialization (supervisor, separate investigator and policy agents) **and** the critic "
    "with its revision loop. A `multi_agent` vs `baseline` difference cannot say which caused "
    "it. `multi_agent_no_critic` vs `multi_agent` isolates the critic; "
    "`multi_agent_no_critic` vs `baseline` is specialization plus revision after validator "
    "objections (the baseline never revises). Both share eligibility, amounts and the validator."
)
FREE_TIER_CAVEAT = (
    "Free-tier models: runs are paced client-side, and a run whose models were unreachable "
    "(LLM_UNAVAILABLE) is excluded and run again on resume, not scored. Small samples: one "
    "case is several percentage points."
)


class RunInfo(BaseModel):
    """How this report was produced. No secrets: model names and switches only."""

    generated_at: datetime
    dataset_cases: int
    configs: list[EvalConfig]
    repeats: int
    only: list[str]
    limit: int | None
    models: dict[str, str]
    rpm: float
    tpm: float | None
    stopped_early: bool


class Report(BaseModel):
    label: str
    caveats: list[str]
    run: RunInfo
    configs: list[ConfigMetrics]
    results: list[CaseResult]


def build_report(run: RunInfo, results: Sequence[CaseResult]) -> Report:
    return Report(
        label=SYNTHETIC_LABEL,
        caveats=[ADR_010_CAVEAT, FREE_TIER_CAVEAT],
        run=run,
        configs=compute_all(results),
        results=list(results),
    )


# ---------------------------------------------------------------- Markdown


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _acc(a: Accuracy) -> tuple[str, str]:
    return (
        f"{a.strict_correct}/{a.runs} ({_pct(a.strict)})",
        f"{a.lenient_correct}/{a.runs} ({_pct(a.lenient)})",
    )


def _num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def _spread(s: Spread, digits: int = 1) -> str:
    return f"{_num(s.mean, digits)} / {_num(s.median, digits)} / {_num(s.max, digits)}"


def _compares_with_baseline(configs: Sequence[EvalConfig]) -> bool:
    return EvalConfig.BASELINE in configs and len(set(configs)) > 1


def _table(
    header: Sequence[str], rows: Sequence[Sequence[str]], configs: Sequence[EvalConfig]
) -> list[str]:
    """A Markdown table; followed by the ADR-010 caveat whenever it compares with baseline."""
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
        *("| " + " | ".join(row) + " |" for row in rows),
    ]
    if _compares_with_baseline(configs):
        lines += ["", ADR_010_CAVEAT]
    return lines


def render_markdown(report: Report) -> str:
    run = report.run
    metrics = report.configs
    configs = [m.config for m in metrics]
    out: list[str] = [
        "# Resolution eval report",
        "",
        f"> {report.label}",
        "",
        f"- Generated: {run.generated_at:%Y-%m-%d %H:%M} UTC",
        f"- Dataset: {run.dataset_cases} cases; configurations: "
        + ", ".join(f"`{c.value}`" for c in run.configs),
        f"- Repeats per case: {run.repeats}"
        + (f"; filter --only {', '.join(run.only)}" if run.only else "")
        + (f"; --limit {run.limit}" if run.limit is not None else ""),
        "- Models: " + ", ".join(f"{k} `{v}`" for k, v in run.models.items()),
        f"- Pacing: {run.rpm:g} requests/min"
        + (f", {run.tpm:g} tokens/min" if run.tpm is not None else "")
        + " per provider",
        "",
        "## Caveats",
        "",
        *(f"- {c}" for c in report.caveats),
        "",
    ]
    if run.stopped_early:
        out += [
            "**Incomplete:** the run stopped early because the models kept being unreachable "
            "(probably the daily quota). Resume later with the same command.",
            "",
        ]
    case_sets = {frozenset(r.case_id for r in report.results if r.config == c) for c in configs}
    if len(case_sets) > 1:
        out += [
            "**Not like for like:** the configurations were scored on different cases "
            "(an interrupted or filtered run). Compare them only on the per-case table.",
            "",
        ]

    out += ["## Safety (every count must be 0)", ""]
    out += _table(
        ["config", "scored runs", "unsafe refunds", "accepted with validator codes", "leaks"],
        [
            [
                f"`{m.config.value}`",
                str(m.runs),
                str(m.unsafe_refunds),
                str(m.grounding.accepted_with_validator_codes),
                str(m.grounding.runs_with_leaks),
            ]
            for m in metrics
        ],
        configs,
    )
    for m in metrics:
        if m.unsafe_refund_cases:
            out += ["", f"Unsafe refunds in `{m.config.value}`: {', '.join(m.unsafe_refund_cases)}"]

    out += [
        "",
        "## Action accuracy",
        "",
        "Strict: exactly the expected action. Lenient: the expected action or one of the "
        "case's accepted alternatives. Read strict first.",
        "",
    ]
    out += _table(
        ["config", "strict", "lenient", "excluded (LLM unavailable)", "errors"],
        [
            [
                f"`{m.config.value}`",
                *_acc(m.accuracy),
                str(m.excluded_infrastructure),
                str(m.errors),
            ]
            for m in metrics
        ],
        configs,
    )
    out += ["", "### Cases that passed only through an accepted alternative", ""]
    any_alt = False
    for m in metrics:
        for alt in m.alternatives_used:
            any_alt = True
            out.append(
                f"- `{m.config.value}` {alt.case_id} (repeat {alt.repeat}): expected "
                f"`{alt.expected.value}`, got `{alt.observed.value}`"
            )
    if not any_alt:
        out.append("None: lenient accuracy equals strict accuracy.")

    categories = sorted({c for m in metrics for c in m.accuracy_by_category})
    out += ["", "### By category (strict / lenient)", ""]
    out += _table(
        ["config", *categories],
        [
            [
                f"`{m.config.value}`",
                *(
                    " / ".join(_acc(m.accuracy_by_category[c]))
                    if c in m.accuracy_by_category
                    else "n/a"
                    for c in categories
                ),
            ]
            for m in metrics
        ],
        configs,
    )

    out += ["", "## Escalation", ""]
    out += _table(
        ["config", "precision", "recall", "escalated", "should escalate"],
        [
            [
                f"`{m.config.value}`",
                _pct(m.escalation.precision),
                _pct(m.escalation.recall),
                str(m.escalation.predicted),
                str(m.escalation.expected),
            ]
            for m in metrics
        ],
        configs,
    )

    out += [
        "",
        "## Grounding",
        "",
        "Problems the validator caught in drafts (and sent back or escalated). Higher is not "
        "worse safety: it shows how often the models needed the validator.",
        "",
    ]
    out += _table(
        ["config", "citation issues caught", "unverified claims caught", "runs with an issue"],
        [
            [
                f"`{m.config.value}`",
                str(m.grounding.citation_issues_caught),
                str(m.grounding.unverified_claims_caught),
                str(m.grounding.runs_with_issue_caught),
            ]
            for m in metrics
        ],
        configs,
    )

    out += ["", "## Resolution split (correct runs only)", ""]
    out += _table(
        ["config", "automated", "human-assisted (refund awaiting approval)", "handed off"],
        [
            [
                f"`{m.config.value}`",
                str(m.resolution_split.automated),
                str(m.resolution_split.human_assisted),
                str(m.resolution_split.handed_off),
            ]
            for m in metrics
        ],
        configs,
    )

    out += ["", "## Cost and latency per run (mean / median / max)", ""]
    out += _table(
        ["config", "LLM calls", "input tokens", "output tokens", "active seconds", "total tokens"],
        [
            [
                f"`{m.config.value}`",
                _spread(m.cost.llm_calls),
                _spread(m.cost.input_tokens, 0),
                _spread(m.cost.output_tokens, 0),
                _spread(m.cost.active_seconds),
                f"{m.cost.input_tokens.total + m.cost.output_tokens.total:,.0f}",
            ]
            for m in metrics
        ],
        configs,
    )
    out += [
        "",
        "Active seconds exclude time spent waiting on the client-side rate limiter.",
        "",
        "## Variance across repeats",
        "",
    ]
    out += _table(
        ["config", "repeats", "strict accuracy per repeat", "stdev", "inconsistent cases"],
        [
            [
                f"`{m.config.value}`",
                str(m.variance.repeats),
                ", ".join(_pct(a) for a in m.variance.strict_accuracy_per_repeat),
                _num(m.variance.strict_accuracy_stdev, 3),
                ", ".join(m.variance.inconsistent_cases) or "none",
            ]
            for m in metrics
        ],
        configs,
    )

    out += ["", "## Per case", ""]
    case_ids = list(dict.fromkeys(r.case_id for r in report.results))
    expected = {r.case_id: r.expected_action.value for r in report.results}

    def observed(case_id: str, config: EvalConfig) -> str:
        runs = [r for r in report.results if r.case_id == case_id and r.config == config]
        if not runs:
            return "-"
        return ", ".join(
            ("LLM unavailable" if r.infrastructure_failure else None)
            or (r.observed_action.value if r.observed_action else (r.error or "none"))
            for r in sorted(runs, key=lambda r: r.repeat)
        )

    out += _table(
        ["case", "expected", *(f"`{c.value}`" for c in configs)],
        [[cid, expected[cid], *(observed(cid, c) for c in configs)] for cid in case_ids],
        configs,
    )
    return "\n".join(out) + "\n"
