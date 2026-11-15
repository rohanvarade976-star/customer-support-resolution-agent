# Tracing and metrics

How to look at a run in Jaeger and read the Prometheus metrics. Design and privacy rules: ADR-018 in
[decisions.md](decisions.md).

## Tracing and metrics (ADR-018)

**Traces** show one run step by step: the API request, the worker job it queued (the same
trace, even though they are two processes), each workflow node, every model call and every
tool call. Spans hold ids, codes, counts, model names and timings, never customer text.

To see a run's trace in Jaeger:

```powershell
# 1. Start Jaeger (it only starts when you ask for the "tracing" profile)
docker compose --profile tracing up -d jaeger

# 2. In .env set:  OTEL_EXPORTER=otlp   then restart the API and the worker

# 3. Start a run (see "Run the background worker" in [running-locally.md](running-locally.md)), wait for the worker to finish it

# 4. Open http://localhost:16686, choose service "support-agent-api", click Find Traces.
#    Open the newest "api.request POST /tickets/{ticket_id}/runs" trace: the worker's
#    "worker.job" span and everything under it are in the same trace.
```

`OTEL_EXPORTER=console` prints spans in the terminal instead (no Jaeger needed). Every JSON
log line written inside a span has a `trace_id`, so you can search the logs for a trace id
copied from Jaeger. `docker compose --profile tracing down` stops Jaeger.

**Metrics** are in Prometheus format. The API serves `GET /metrics` only with a token: set
`METRICS_TOKEN` in `.env` (without one the route answers 404), then:

```powershell
$t = @{ Authorization = "Bearer <your METRICS_TOKEN>" }
(Invoke-WebRequest http://localhost:8000/metrics -Headers $t).Content
```

The worker serves its own counters on loopback when `WORKER_METRICS_PORT` is set
(`http://127.0.0.1:<port>/metrics`, no token). Queue depth, dead jobs, reconciliation backlog
and refunds by status come from the database and are served by the API only, so adding the
API's and the worker's numbers together never counts anything twice.
