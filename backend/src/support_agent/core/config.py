"""Application settings, loaded once from environment variables and the `.env` file.

Every other module reads config through `get_settings()`, never from `os.environ`.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> core -> support_agent -> src -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]

MIN_JWT_SECRET_LENGTH = 32  # HS256 needs a 256-bit key to be safe


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Local dev reads the repo-root .env (shared with docker compose).
        # In CI and production the file is absent and real env vars are used.
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        # .env also holds values for docker compose (POSTGRES_*); ignore those here.
        extra="ignore",
    )

    app_env: Literal["local", "test", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Required: no default, so a missing value fails at startup instead of later.
    # SecretStr hides the password if settings are ever printed or logged.
    database_url: SecretStr
    # Separate database used only by integration tests (they wipe it). Optional:
    # without it, integration tests are skipped locally (and fail in CI).
    test_database_url: SecretStr | None = None

    # Signs and verifies login tokens. Required, and long enough to resist guessing.
    jwt_secret: SecretStr
    access_token_minutes: int = Field(default=60, ge=1, le=1440)
    # Password given to every seeded demo account. Only the seed script needs it.
    demo_password: SecretStr | None = None

    # External providers (the mocks locally). Used only by `integrations/`.
    payments_base_url: str = "http://localhost:8001"
    shipping_base_url: str = "http://localhost:8002"
    ticketing_base_url: str = "http://localhost:8003"
    provider_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=60)
    provider_read_timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    # Total tries for retried calls (1 = no retry). Refund creation always makes one try.
    provider_max_attempts: int = Field(default=3, ge=1, le=10)
    # First retry waits up to this long; each later retry doubles the limit.
    provider_backoff_base_seconds: float = Field(default=0.2, ge=0, le=10)

    # The refund window is NOT a setting: it comes from the refund policy version in
    # force on the order's placed_at date (ADR-005), via policy_documents.refund_window_days.

    # How long a reviewer has to decide a refund proposal before it needs a new one.
    approval_ttl_minutes: int = Field(default=60, ge=1, le=1440)

    # Turns policy text into vectors. "fastembed" runs the real local model;
    # "fake" is a deterministic stand-in with no download (tests and CI).
    embedding_provider: Literal["fastembed", "fake"] = "fastembed"

    # Policy search (hybrid retrieval, see rag/retrieval.py).
    # How many passages the search_policy tool returns at most.
    policy_search_top_k: int = Field(default=5, ge=1, le=20)
    # How many candidates each search (vector and keyword) contributes before fusion.
    policy_search_candidate_k: int = Field(default=20, ge=1, le=100)
    # Reciprocal Rank Fusion constant: score = sum of 1 / (k + rank). 60 is the usual value.
    policy_rrf_k: int = Field(default=60, ge=1, le=1000)
    # Cosine similarity (1 = same meaning) a passage needs to be returned at all.
    # Below it for every passage, the result says no_relevant_evidence instead.
    # Tuned for BAAI/bge-small-en-v1.5 with scripts/eval_retrieval.py.
    policy_min_similarity: float = Field(default=0.55, ge=-1.0, le=1.0)

    # LLM (see llm/providers.py). "groq": Groq first, Gemini as fallback if its key is
    # set. "fake": the scripted offline fake (tests and CI; never calls a real API).
    llm_provider: Literal["groq", "fake"] = "groq"
    # Optional here so the app and tests start without them; building a real LLM
    # client without the needed key raises a clear LLMConfigError.
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    # "small" = fast model for simple steps; "large" = stronger model for reasoning.
    # All four support tool calling. The Groq pair was checked against the models our
    # key can actually use (2026-11-12); the Llama models in Groq's docs were not listed.
    groq_small_model: str = "openai/gpt-oss-20b"
    groq_large_model: str = "openai/gpt-oss-120b"
    # gpt-oss models "think" before answering, and thinking counts as output tokens.
    # "low" keeps it short so a small max_tokens still leaves room for the answer.
    # Leave blank for models without reasoning (the setting is then not sent).
    groq_reasoning_effort: Literal["low", "medium", "high"] | None = "low"
    gemini_small_model: str = "gemini-3.5-flash-lite"
    gemini_large_model: str = "gemini-3.6-flash"
    # 0 = most predictable answers. Agents can override per call where it helps.
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=1024, ge=1, le=32768)
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    # Tries per provider (including the first) before falling back to the next one.
    llm_max_attempts: int = Field(default=3, ge=1, le=10)
    # Backoff: random wait up to base * 2^(attempt-1), never more than the max.
    llm_backoff_base_seconds: float = Field(default=1.0, ge=0, le=30)
    llm_backoff_max_seconds: float = Field(default=20.0, ge=0, le=120)

    # The classifier's confidence (0-1) needed to go straight to investigation. Below it,
    # the run asks the customer to clarify. Tune with scripts/eval_classifier.py.
    classifier_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    # The most tool calls one investigation may make (PRD "Agent specifications"). If the
    # model wants more, the run escalates instead of looping.
    investigator_max_tool_calls: int = Field(default=8, ge=1, le=20)

    @field_validator("payments_base_url", "shipping_base_url", "ticketing_base_url")
    @classmethod
    def _require_http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("provider base URLs must start with 'http://' or 'https://'")
        return value

    @field_validator("groq_api_key", "gemini_api_key", "groq_reasoning_effort", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        # `GEMINI_API_KEY=` (blank, as in .env.example) means "not configured".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("jwt_secret")
    @classmethod
    def _require_long_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_JWT_SECRET_LENGTH:
            raise ValueError(f"JWT_SECRET must be at least {MIN_JWT_SECRET_LENGTH} characters")
        return value

    @field_validator("database_url", "test_database_url")
    @classmethod
    def _require_async_driver(cls, value: SecretStr | None) -> SecretStr | None:
        # The whole app is async, so the URL must use the asyncpg driver.
        if value is not None and not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("database URLs must start with 'postgresql+asyncpg://'")
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the settings, created once and reused (cached)."""
    return Settings()  # values come from the environment / .env
