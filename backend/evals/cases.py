"""The golden cases (evals/datasets/resolution_cases.json) and what one run of a case produced.

`EvalCase` is what we expect; `CaseResult` is what the harness observed in the database. Both
are plain data, so `evals/metrics.py` can score them without any I/O.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from support_agent.domain.enums import ResolutionAction

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"
CASES_FILE = DATASETS_DIR / "resolution_cases.json"
EVAL_SEED_FILE = DATASETS_DIR / "eval_seed.json"
EVAL_SHIPMENTS_FILE = DATASETS_DIR / "eval_shipments.json"

CaseCategory = Literal["normal", "hard", "adversarial"]


class EvalConfig(StrEnum):
    """The three configurations compared (ADR-010 item 3, ADR-016)."""

    MULTI_AGENT = "multi_agent"
    MULTI_AGENT_NO_CRITIC = "multi_agent_no_critic"
    BASELINE = "baseline"


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    category: CaseCategory
    customer_email: str  # an eval-only customer: the run's identity, as a JWT would give it
    order_number: str | None  # what the customer typed in the order field, if anything
    complaint: str
    expected_action: ResolutionAction
    expected_refund_minor: int | None
    # Other actions that are also defensible. Reported only as *lenient* accuracy.
    acceptable_alternatives: list[ResolutionAction] = []
    # Another customer's data: must never appear in the reply this customer sees.
    forbidden_reply_terms: list[str] = []
    note: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> "EvalCase":
        is_refund = self.expected_action == ResolutionAction.REFUND
        if is_refund != (self.expected_refund_minor is not None):
            raise ValueError(f"{self.id}: expected_refund_minor must be set iff refund")
        if self.expected_refund_minor is not None and self.expected_refund_minor <= 0:
            raise ValueError(f"{self.id}: expected_refund_minor must be positive")
        if ResolutionAction.REFUND in self.acceptable_alternatives:
            # A refund alternative would make an unexpected refund look acceptable.
            raise ValueError(f"{self.id}: a refund can never be an acceptable alternative")
        if self.expected_action in self.acceptable_alternatives:
            raise ValueError(f"{self.id}: the expected action is not an alternative")
        return self


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    cases: list[EvalCase]

    @model_validator(mode="after")
    def _unique_ids(self) -> "Dataset":
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate case id")
        return self


def load_cases(path: Path = CASES_FILE) -> list[EvalCase]:
    return Dataset.model_validate_json(path.read_text(encoding="utf-8")).cases


class CaseResult(BaseModel):
    """One (configuration, case, repeat) run, read back from the database.

    Codes and numbers only, plus the customer reply's forbidden-term hits (never the reply).
    """

    model_config = ConfigDict(extra="forbid")

    config: EvalConfig
    case_id: str
    repeat: int = Field(ge=0)
    category: CaseCategory
    expected_action: ResolutionAction
    expected_refund_minor: int | None
    acceptable_alternatives: list[ResolutionAction] = []

    # What happened. `observed_action` is None when the run ended without an action
    # (it failed, or the harness could not start it); `error` then says why.
    run_status: str | None = None
    observed_action: ResolutionAction | None = None
    # Every refund proposal row the run stored (amounts in paise), from action_proposals.
    proposed_refunds_minor: list[int] = []
    error: str | None = None
    error_codes: list[str] = []  # the run's workflow error codes, e.g. LLM_UNAVAILABLE

    # Grounding: validator codes per draft attempt, and the critic's verdicts.
    validation_codes: list[list[str]] = []
    critic_verdicts: list[str] = []
    leaked_terms: list[str] = []  # forbidden_reply_terms found in the customer reply

    # Cost and latency. `active_seconds` excludes time spent waiting on the rate limiter.
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    active_seconds: float = 0.0
    rate_limit_wait_seconds: float = 0.0

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.config.value, self.case_id, self.repeat)

    @property
    def infrastructure_failure(self) -> bool:
        """The models were unreachable (quota, outage): the case says nothing about the agents,
        so it does not count as finished and is run again on resume."""
        return "LLM_UNAVAILABLE" in self.error_codes
