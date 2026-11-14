"""Tracing helpers (ADR-018): the attribute allowlist, trace context carrying, log trace ids,
and the spans and metrics of the LLM client and the tool registry. No real model calls."""

import json
import logging
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, ConfigDict

from support_agent.core import metrics
from support_agent.core.logging import JsonFormatter
from support_agent.core.tracing import (
    ALLOWED_ATTRIBUTES,
    current_trace_id,
    extract_traceparent,
    inject_traceparent,
    span,
    use_provider,
)
from support_agent.llm.client import RetrySettings, RoutedLLMClient
from support_agent.llm.fake import FakeProvider, rate_limited, text_reply
from support_agent.llm.types import ChatMessage

MARKER = "ZXQMARKER7731"


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    previous = use_provider(provider)
    yield memory
    use_provider(previous)


def by_name(exporter: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


def sample(name: str, **labels: str) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


# ---------------------------------------------------------------- the helper


def test_an_attribute_name_outside_the_allowlist_is_refused(
    exporter: InMemorySpanExporter,
) -> None:
    with pytest.raises(ValueError, match="ALLOWED_ATTRIBUTES"), span("x") as s:
        s.set("customer.email", "a@b.c")


def test_text_values_are_dropped_but_ids_and_numbers_are_kept(
    exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    with span("x") as s:
        s.set("error.code", f"my card says {MARKER}")  # has spaces: a sentence, not a code
        s.set("run.id", "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b")
        s.set("llm.attempt", 2)

    (finished,) = exporter.get_finished_spans()
    assert dict(finished.attributes or {}) == {
        "run.id": "3f2b8c1e-9a4d-4e6f-8b7a-1c2d3e4f5a6b",
        "llm.attempt": 2,
    }
    # The warning names the attribute, never the value.
    assert MARKER not in "\n".join(JsonFormatter().format(r) for r in caplog.records)


def test_a_failing_block_records_the_exception_class_only(
    exporter: InMemorySpanExporter,
) -> None:
    with pytest.raises(RuntimeError), span("x"):
        raise RuntimeError(f"secret text {MARKER}")

    (finished,) = exporter.get_finished_spans()
    assert MARKER not in finished.to_json()
    assert finished.status.description == "RuntimeError"
    assert not finished.events  # no recorded exception, which would carry the message


def test_tracing_is_a_no_op_when_off() -> None:
    previous = use_provider(None)
    try:
        with span("x") as s:
            s.set("llm.attempt", 1)
            assert current_trace_id() is None
            assert inject_traceparent() is None
    finally:
        use_provider(previous)


# ---------------------------------------------------------------- carrying the trace


def test_a_traceparent_links_a_span_in_another_process_to_its_trace(
    exporter: InMemorySpanExporter,
) -> None:
    with span("api.request"):
        carried = inject_traceparent()
    assert carried is not None

    with span("worker.job", parent=extract_traceparent(carried)):
        pass

    request, job = by_name(exporter, "api.request")[0], by_name(exporter, "worker.job")[0]
    assert job.context.trace_id == request.context.trace_id
    assert job.parent is not None and job.parent.span_id == request.context.span_id


@pytest.mark.parametrize("bad", [None, "", 42, "not-a-traceparent", "00-xyz-abc-01"])
def test_a_damaged_traceparent_starts_a_new_trace(bad: object) -> None:
    assert extract_traceparent(bad) is None


def test_log_lines_carry_the_trace_id_inside_a_span_only(exporter: InMemorySpanExporter) -> None:
    record = logging.LogRecord("t", logging.INFO, "f", 1, "hello", None, None)
    assert "trace_id" not in json.loads(JsonFormatter().format(record))

    with span("x"):
        line = json.loads(JsonFormatter().format(record))

    (finished,) = exporter.get_finished_spans()
    assert line["trace_id"] == f"{finished.context.trace_id:032x}"


# ---------------------------------------------------------------- LLM calls


def make_client(*providers: FakeProvider) -> RoutedLLMClient:
    async def no_wait(_seconds: float) -> None:
        return None

    return RoutedLLMClient(
        list(providers),
        RetrySettings(max_attempts=3, backoff_base_seconds=1, backoff_max_seconds=5),
        default_temperature=0.0,
        default_max_tokens=256,
        sleep=no_wait,
    )


async def test_every_llm_attempt_has_a_span_and_metrics_without_any_text(
    exporter: InMemorySpanExporter,
) -> None:
    labels = {"provider": "groq", "model": "groq-small"}
    calls_ok = sample("support_llm_calls_total", tier="small", outcome="ok", **labels)
    calls_err = sample("support_llm_calls_total", tier="small", outcome="error", **labels)
    tokens_in = sample("support_llm_tokens_total", kind="input", **labels)
    tokens_out = sample("support_llm_tokens_total", kind="output", **labels)
    latency_n = sample("support_llm_latency_seconds_count", tier="small", **labels)
    groq = FakeProvider(
        [rate_limited(), text_reply(f"answer {MARKER}", input_tokens=42, output_tokens=7)],
        name="groq",
    )
    messages = [
        ChatMessage(role="system", content="system prompt"),
        ChatMessage(role="user", content=f"my complaint {MARKER}"),
    ]

    await make_client(groq).chat(messages)

    failed, ok = sorted(by_name(exporter, "llm.call"), key=lambda s: s.start_time or 0)
    assert (
        dict(failed.attributes or {}).items()
        >= {
            "llm.provider": "groq",
            "llm.model": "groq-small",
            "llm.tier": "small",
            "llm.attempt": 1,
            "llm.outcome": "error",
            "error.type": "LLMRateLimitError",
        }.items()
    )
    assert (
        dict(ok.attributes or {}).items()
        >= {
            "llm.attempt": 2,
            "llm.outcome": "ok",
            "llm.input_tokens": 42,
            "llm.output_tokens": 7,
        }.items()
    )
    assert "llm.latency_ms" in (ok.attributes or {})
    assert all(MARKER not in s.to_json() for s in exporter.get_finished_spans())

    assert sample("support_llm_calls_total", tier="small", outcome="ok", **labels) == calls_ok + 1
    assert sample("support_llm_calls_total", tier="small", outcome="error", **labels) == (
        calls_err + 1
    )
    assert sample("support_llm_tokens_total", kind="input", **labels) == tokens_in + 42
    assert sample("support_llm_tokens_total", kind="output", **labels) == tokens_out + 7
    assert sample("support_llm_latency_seconds_count", tier="small", **labels) == latency_n + 2


# ---------------------------------------------------------------- tool calls


async def test_a_tool_span_names_only_registered_tools(exporter: InMemorySpanExporter) -> None:
    from support_agent.tools.base import Tool, ToolContext, ToolRegistry

    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")

        order_number: str

    class Out(BaseModel):
        ok: bool = True

    async def handler(_ctx: ToolContext, _args: Args) -> Out:
        return Out()

    registry = ToolRegistry()
    registry.register(Tool("get_thing", "d", Args, Out, handler))
    ctx = None  # the handler ignores it; validation fails first for the bad call

    await registry.execute(f"invented tool {MARKER}", ctx, {})  # type: ignore[arg-type]
    await registry.execute("get_thing", ctx, {"order_number": "ORD-1"})  # type: ignore[arg-type]
    await registry.execute("get_thing", ctx, {"order_number": 5})  # type: ignore[arg-type]

    unknown, ok, bad = by_name(exporter, "tool.call")
    assert dict(unknown.attributes or {}) == {
        "tool.name": "unknown",
        "tool.outcome": "error",
        "error.code": "UNKNOWN_TOOL",
    }
    assert dict(ok.attributes or {}) == {"tool.name": "get_thing", "tool.outcome": "ok"}
    assert dict(bad.attributes or {})["error.code"] == "INVALID_INPUT"
    assert all(MARKER not in s.to_json() for s in exporter.get_finished_spans())


def test_the_allowlist_has_no_free_text_names() -> None:
    # A reminder for reviewers: adding a name here means checking what it could hold.
    assert not {a for a in ALLOWED_ATTRIBUTES if any(w in a for w in ("text", "message", "body"))}
