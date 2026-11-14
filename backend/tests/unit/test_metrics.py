"""Metrics definitions and settings (ADR-018). Database-derived gauges are tested with the
API in tests/integration/test_observability.py."""

import urllib.request

import pytest
from prometheus_client import generate_latest

from support_agent.core import metrics
from support_agent.core.config import Settings


def test_database_gauges_are_not_in_the_registry_the_worker_serves() -> None:
    worker_text = generate_latest(metrics.REGISTRY).decode()
    api_only = generate_latest(metrics.DB_REGISTRY).decode()

    for name in ("support_jobs_queued", "support_jobs_dead", "support_refunds"):
        assert name not in worker_text  # a dashboard summing API and worker counts rows once
        assert name in api_only
    assert "support_runs_total" in worker_text


def test_the_worker_serves_only_in_process_metrics() -> None:
    metrics.RUNS.labels(outcome="completed").inc(0)
    server = metrics.start_worker_metrics_server("127.0.0.1", 0)  # port 0: any free port
    try:
        port = server.server_port
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            body = response.read().decode()
    finally:
        server.shutdown()
    assert "support_runs_total" in body
    assert "support_jobs_queued" not in body


def test_no_metric_label_can_hold_an_id_or_text() -> None:
    allowed = {"outcome", "provider", "model", "tier", "kind", "job_type", "source", "code"}
    allowed |= {"scope", "status"}
    for registry in (metrics.REGISTRY, metrics.DB_REGISTRY):
        for family in registry.collect():
            for sample in family.samples:
                assert set(sample.labels) - {"le"} <= allowed, sample


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    for name in ("METRICS_TOKEN", "OTEL_EXPORTER", "WORKER_METRICS_PORT"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def settings() -> Settings:
    return Settings(_env_file=None)


def test_observability_is_off_and_closed_by_default(base_env: pytest.MonkeyPatch) -> None:
    config = settings()

    assert config.otel_exporter == "off"
    assert config.metrics_token is None
    assert config.worker_metrics_port is None
    assert config.worker_metrics_host == "127.0.0.1"


def test_blank_values_mean_unset(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("METRICS_TOKEN", "")
    base_env.setenv("WORKER_METRICS_PORT", "")

    config = settings()

    assert config.metrics_token is None and config.worker_metrics_port is None


def test_a_short_metrics_token_is_refused(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("METRICS_TOKEN", "short")

    with pytest.raises(ValueError, match="METRICS_TOKEN"):
        settings()


def test_the_metrics_token_is_hidden_in_the_settings_repr(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("METRICS_TOKEN", "t" * 24)

    assert "t" * 24 not in repr(settings())
