"""Prometheus metrics (ADR-018).

Two registries, so the API and the worker can be summed without double counting:

- `REGISTRY`: in-process counters and histograms (runs, LLM calls, guardrail events, 429s,
  jobs). The API and the worker each expose their own; a dashboard sums them.
- `DB_REGISTRY`: gauges computed from the database at scrape time (queue depth, dead jobs,
  reconciliation backlog, refunds by status). Only the API serves these, because every
  process would report the same table and a sum would count each row once per process.

Labels hold only small fixed sets: provider, model, tier, outcome, status, scope, code.
Never a run, customer, order or job id, and never text.
"""

from wsgiref.simple_server import WSGIServer

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

REGISTRY = CollectorRegistry()
DB_REGISTRY = CollectorRegistry()

# ---------------------------------------------------------------- in-process (API and worker)

RUNS = Counter(
    "support_runs_total",
    "Workflow runs that reached a final status.",
    ["outcome"],  # completed | escalated | failed
    registry=REGISTRY,
)
LLM_CALLS = Counter(
    "support_llm_calls_total",
    "LLM provider attempts (a retry is another attempt).",
    ["provider", "model", "tier", "outcome"],  # outcome: ok | error
    registry=REGISTRY,
)
LLM_TOKENS = Counter(
    "support_llm_tokens_total",
    "Tokens reported by providers on successful attempts.",
    ["provider", "model", "kind"],  # kind: input | output
    registry=REGISTRY,
)
LLM_LATENCY = Histogram(
    "support_llm_latency_seconds",
    "Time of one LLM provider attempt.",
    ["provider", "model", "tier"],
    buckets=(0.25, 0.5, 1, 2, 4, 8, 15, 30, 60),
    registry=REGISTRY,
)
JOBS = Counter(
    "support_jobs_total",
    "Background job attempts by how they ended.",
    ["job_type", "outcome"],  # succeeded | retried | dead | released | lost_lease
    registry=REGISTRY,
)
INJECTION_SUSPECTED = Counter(
    "support_injection_suspected_total",
    "Customer texts flagged by the input guardrail (never blocked).",
    ["source"],  # complaint | reply
    registry=REGISTRY,
)
REPLY_GUARDRAIL_REPLACED = Counter(
    "support_reply_guardrail_replaced_total",
    "Replies replaced at save time by the output guardrail.",
    ["code"],
    registry=REGISTRY,
)
RATE_LIMITED = Counter(
    "support_rate_limited_total",
    "429 responses.",
    ["scope"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------- from the database (API only)

JOBS_QUEUED = Gauge("support_jobs_queued", "Jobs waiting in the queue.", registry=DB_REGISTRY)
JOBS_OLDEST_QUEUED_AGE = Gauge(
    "support_jobs_oldest_queued_age_seconds",
    "Age of the oldest queued job that is due to run (0 if none).",
    registry=DB_REGISTRY,
)
JOBS_DEAD = Gauge(
    "support_jobs_dead", "Jobs that used all attempts and were escalated.", registry=DB_REGISTRY
)
RECONCILIATION_BACKLOG = Gauge(
    "support_reconciliation_backlog",
    "Refund operations whose reconciliation check is due.",
    registry=DB_REGISTRY,
)
REFUNDS = Gauge(
    "support_refunds",
    "Refund operations by status.",
    ["status"],
    registry=DB_REGISTRY,
)


def start_worker_metrics_server(host: str, port: int) -> WSGIServer:
    """Serve `REGISTRY` over HTTP in a background thread. Returns the server (call
    `.shutdown()` to stop it). No token: the caller keeps it on loopback."""
    server, _thread = start_http_server(port, addr=host, registry=REGISTRY)
    return server
