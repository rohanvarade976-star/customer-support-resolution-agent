"""OpenTelemetry tracing, with privacy built in (ADR-018).

A trace is a tree of timed spans: API request -> job -> workflow node -> LLM call / tool call.

Privacy: a span may carry only ids, codes, counts, model names and timings. Two rules enforce it:

1. `ALLOWED_ATTRIBUTES` is the complete list of attribute names. Any other name raises, so a
   new attribute needs a deliberate edit here (and a review of what it could hold).
2. A string value must look like a token (letters, digits and `_ . : / { } -`, no spaces, at most
   80 characters). A complaint or a model answer has spaces, so it is dropped (with a warning
   that names the key, never the value). Numbers and booleans are always fine.

Nothing here instruments HTTP clients or SQL: those libraries record URLs and statements,
which can hold customer text.

Exporter (setting `OTEL_EXPORTER`): `off` (default: spans are no-ops), `console`, or `otlp`.
Tests install an in-memory exporter with `use_provider`.

Across processes: `inject_traceparent()` returns the W3C `traceparent` string (two ids and a
flag) for the current span. The API stores it in a job's payload and the worker passes it to
`span(..., parent=extract_traceparent(...))`, so one complaint is one trace.
"""

import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Literal

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from support_agent.core.config import Settings

log = logging.getLogger(__name__)

type AttributeValue = str | int | float | bool

ALLOWED_ATTRIBUTES = frozenset(
    {
        "http.method",
        "http.route",
        "http.status_code",
        "job.id",
        "job.type",
        "job.attempt",
        "job.outcome",
        "run.id",
        "workflow.node",
        "workflow.outcome",
        "llm.provider",
        "llm.model",
        "llm.tier",
        "llm.attempt",
        "llm.outcome",
        "llm.input_tokens",
        "llm.output_tokens",
        "llm.reasoning_tokens",
        "llm.latency_ms",
        "tool.name",
        "tool.outcome",
        "reconcile.refund_id",
        "reconcile.reported",
        "reconcile.checked",
        "error.type",
        "error.code",
    }
)

# What a string value may look like: a name or an id, never a sentence.
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.:/{}\-]{0,80}")

_TRACEPARENT = re.compile(r"[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")
_propagator = TraceContextTextMapPropagator()

# Our own provider, not OpenTelemetry's process-global one: the global can be set only once,
# and tests need to swap exporters.
_provider: TracerProvider | None = None


def use_provider(provider: TracerProvider | None) -> TracerProvider | None:
    """Make `provider` the one spans are created with (None: tracing off). Returns the old one."""
    global _provider
    previous, _provider = _provider, provider
    return previous


def configure_tracing(settings: Settings, role: Literal["api", "worker"]) -> None:
    """Install the exporter chosen in settings. Call once at process start."""
    if settings.otel_exporter == "off":
        use_provider(None)
        return
    resource = Resource.create({"service.name": f"{settings.otel_service_name}-{role}"})
    provider = TracerProvider(resource=resource)
    if settings.otel_exporter == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    else:
        # Imported here so the HTTP exporter is loaded only when it is used.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=settings.otel_otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    use_provider(provider)


def shutdown_tracing() -> None:
    """Send any spans still waiting in the batch, then stop. Call at process exit."""
    if _provider is not None:
        _provider.shutdown()


def _tracer() -> trace.Tracer:
    if _provider is None:
        return trace.NoOpTracer()
    return _provider.get_tracer("support_agent")


class SafeSpan:
    """The span a `with span(...)` block gives you. Only checked attributes can be set."""

    def __init__(self, raw: trace.Span) -> None:
        self._raw = raw
        # True: an exception leaving the block is normal control flow (a workflow pause).
        self.expected_exit = False

    def set(self, key: str, value: AttributeValue) -> None:
        checked = _check(key, value)
        if checked is not None:
            self._raw.set_attribute(key, checked)

    def fail(self, error_type: str) -> None:
        """Mark the span as failed. The class name only: messages can hold customer text."""
        self._raw.set_status(Status(StatusCode.ERROR, error_type))
        self.set("error.type", error_type)


def _check(key: str, value: AttributeValue) -> AttributeValue | None:
    if key not in ALLOWED_ATTRIBUTES:
        raise ValueError(f"span attribute {key!r} is not in ALLOWED_ATTRIBUTES")
    if isinstance(value, bool | int | float):
        return value
    if _SAFE_TOKEN.fullmatch(value) is None:
        log.warning("span_attribute_dropped", extra={"attribute": key})
        return None
    return value


@contextmanager
def span(
    name: str,
    attributes: Mapping[str, AttributeValue] | None = None,
    *,
    parent: Context | None = None,
) -> Iterator[SafeSpan]:
    """Run a block inside a new span, a child of the current span (or of `parent`)."""
    with _tracer().start_as_current_span(
        name, context=parent, record_exception=False, set_status_on_exception=False
    ) as raw:
        safe = SafeSpan(raw)
        for key, value in (attributes or {}).items():
            safe.set(key, value)
        try:
            yield safe
        except BaseException as exc:
            if not safe.expected_exit:
                safe.fail(type(exc).__name__)
            raise


def current_trace_id() -> str | None:
    """The current trace's id as 32 hex characters, or None outside a recorded span."""
    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else None


def inject_traceparent() -> str | None:
    """The current span as a W3C `traceparent` string, or None if there is no recorded span."""
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return carrier.get("traceparent")


def extract_traceparent(value: object) -> Context | None:
    """Turn a stored `traceparent` back into a parent context. Anything malformed gives None,
    so a damaged payload starts a new trace instead of failing the job."""
    if not isinstance(value, str) or _TRACEPARENT.fullmatch(value) is None:
        return None
    return _propagator.extract({"traceparent": value}, context=otel_context.Context())
