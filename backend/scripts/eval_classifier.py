"""Measure the classifier with the real LLM (Groq, with Gemini fallback if configured).

Runs every complaint in evals/datasets/classification.json through the classifier agent
and prints:
- routing accuracy: does the run go where it should? A prediction below the confidence
  threshold, or "unclear", counts as "unclear" (the run would ask the customer).
- label accuracy: is the raw category right, ignoring confidence?
- order number extraction accuracy
- a confusion matrix (expected rows x routed columns)
- the low-confidence count
- calibration: label accuracy at or above the threshold vs below it. If confidence is
  trustworthy, confident answers should be right far more often than unsure ones.
- every routing miss.

A JSON report is written to evals/reports/.

    uv run python scripts/eval_classifier.py
    uv run python scripts/eval_classifier.py --zero-shot   # prompt without the examples

Not run in CI: it calls the real LLM (needs GROQ_API_KEY). No database is used.
The model is not fully deterministic, so expect small differences between runs.
"""

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from support_agent.agents.classifier import Classification, ClassifierCategory, classify
from support_agent.core.config import get_settings
from support_agent.core.logging import configure_logging
from support_agent.llm.client import LLMClient
from support_agent.llm.errors import LLMError
from support_agent.llm.providers import build_llm_client
from support_agent.workflow.nodes.classify import normalize_order_number

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = BACKEND_DIR / "evals" / "datasets" / "classification.json"
REPORTS_DIR = BACKEND_DIR / "evals" / "reports"
LABELS: list[str] = ["order_status", "refund", "damaged_item", "unclear"]
ERROR = "error"  # column for calls that failed outright


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    complaint: str
    expected_category: ClassifierCategory
    expected_order_number: str | None
    note: str = ""


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    cases: list[Case]


class Outcome(BaseModel):
    case: Case
    prediction: Classification | None = None
    error: str | None = None  # the error class name if the call failed

    def routed(self, threshold: float) -> str:
        """Where the workflow would send this case, as a label."""
        p = self.prediction
        if p is None:
            return ERROR
        return "unclear" if p.category == "unclear" or p.confidence < threshold else p.category

    @property
    def label_correct(self) -> bool:
        p = self.prediction
        return p is not None and p.category == self.case.expected_category

    @property
    def order_correct(self) -> bool:
        p = self.prediction
        got = None if p is None else normalize_order_number(p.order_number)
        return p is not None and got == self.case.expected_order_number


async def run(llm: LLMClient, cases: list[Case], *, few_shot: bool) -> list[Outcome]:
    outcomes = []
    for case in cases:  # one at a time: kind to free-tier rate limits
        try:
            prediction = await classify(llm, case.complaint, few_shot=few_shot)
        except LLMError as error:
            outcomes.append(Outcome(case=case, error=type(error).__name__))
            continue
        outcomes.append(Outcome(case=case, prediction=prediction))
    return outcomes


class Bucket(BaseModel):
    """Calibration for one confidence band."""

    count: int
    correct: int  # raw label right
    accuracy: float | None
    mean_confidence: float | None


class CaseResult(BaseModel):
    id: str
    expected: str
    routed: str
    prediction: Classification | None
    error: str | None


class Summary(BaseModel):
    """Everything the report prints, also saved as JSON."""

    prompt: str  # "few_shot" or "zero_shot"
    threshold: float
    total: int
    routing_correct: int
    label_correct: int
    order_number_correct: int
    low_confidence: int
    errors: int
    calibration: dict[str, Bucket]  # "at_or_above" / "below" the threshold
    confusion_matrix: dict[str, dict[str, int]]  # expected -> routed -> count
    cases: list[CaseResult]


def _bucket(group: list[Outcome]) -> Bucket:
    correct = sum(o.label_correct for o in group)
    confidences = [o.prediction.confidence for o in group if o.prediction is not None]
    return Bucket(
        count=len(group),
        correct=correct,
        accuracy=round(correct / len(group), 3) if group else None,
        mean_confidence=round(sum(confidences) / len(confidences), 3) if confidences else None,
    )


def summarize(outcomes: list[Outcome], threshold: float, few_shot: bool) -> Summary:
    answered = [o for o in outcomes if o.prediction is not None]
    confident = [o for o in answered if o.prediction and o.prediction.confidence >= threshold]
    unsure = [o for o in answered if o not in confident]
    confusion: Counter[tuple[str, str]] = Counter(
        (o.case.expected_category, o.routed(threshold)) for o in outcomes
    )
    return Summary(
        prompt="few_shot" if few_shot else "zero_shot",
        threshold=threshold,
        total=len(outcomes),
        routing_correct=sum(o.routed(threshold) == o.case.expected_category for o in outcomes),
        label_correct=sum(o.label_correct for o in outcomes),
        order_number_correct=sum(o.order_correct for o in outcomes),
        low_confidence=len(unsure),
        errors=len(outcomes) - len(answered),
        calibration={"at_or_above": _bucket(confident), "below": _bucket(unsure)},
        confusion_matrix={
            expected: {routed: confusion[(expected, routed)] for routed in [*LABELS, ERROR]}
            for expected in LABELS
        },
        cases=[
            CaseResult(
                id=o.case.id,
                expected=o.case.expected_category,
                routed=o.routed(threshold),
                prediction=o.prediction,
                error=o.error,
            )
            for o in outcomes
        ],
    )


def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({part / whole:.0%})" if whole else "0/0 (n/a)"


def print_report(s: Summary, outcomes: list[Outcome]) -> None:
    print(f"Prompt: {s.prompt}   threshold: {s.threshold}\n")
    print(f"Routing accuracy:       {_pct(s.routing_correct, s.total)}")
    print(f"Label accuracy:         {_pct(s.label_correct, s.total)}")
    print(f"Order number accuracy:  {_pct(s.order_number_correct, s.total)}")
    print(f"Low confidence:         {s.low_confidence}")
    print(f"Errors:                 {s.errors}")

    print("\nCalibration (label accuracy by confidence):")
    for key, title in (("at_or_above", f">= {s.threshold}"), ("below", f"<  {s.threshold}")):
        b = s.calibration[key]
        mean = "n/a" if b.mean_confidence is None else f"{b.mean_confidence:.2f}"
        print(f"  {title:<8} {_pct(b.correct, b.count):<14} mean confidence {mean}")

    print("\nConfusion matrix (rows = expected, columns = where the run would go):")
    columns = [*LABELS, ERROR]
    print(" " * 14 + "".join(f"{c:>14}" for c in columns))
    for expected in LABELS:
        row = s.confusion_matrix[expected]
        print(f"{expected:<14}" + "".join(f"{row[c]:>14}" for c in columns))

    misses = [o for o in outcomes if o.routed(s.threshold) != o.case.expected_category]
    if misses:
        print("\nRouting misses:")
    for o in misses:
        p = o.prediction
        got = o.error if p is None else f"{p.category} ({p.confidence:.2f})"
        print(f"- {o.case.id}: expected {o.case.expected_category}, got {got}")
        if p is not None:
            print(f"    reason: {p.reason}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--zero-shot", action="store_true", help="use the prompt without its examples"
    )
    parser.add_argument("--threshold", type=float, default=None, help="override the setting")
    args = parser.parse_args()

    # Only warnings (retries, fallbacks) as JSON lines; per-call INFO lines would drown
    # the report.
    configure_logging("WARNING")
    settings = get_settings()
    if settings.llm_provider == "fake":
        print("Error: LLM_PROVIDER is 'fake'; this eval needs the real model.")
        return 1
    threshold = (
        settings.classifier_confidence_threshold if args.threshold is None else args.threshold
    )
    few_shot = not args.zero_shot
    dataset = Dataset.model_validate_json(args.dataset.read_text(encoding="utf-8"))

    outcomes = asyncio.run(run(build_llm_client(settings), dataset.cases, few_shot=few_shot))
    summary = summarize(outcomes, threshold, few_shot)
    print_report(summary, outcomes)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"classifier_{summary.prompt}.json"
    # newline="\n": the repo uses LF; Windows would otherwise write CRLF.
    out.write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"\nReport written to {out.relative_to(BACKEND_DIR)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
