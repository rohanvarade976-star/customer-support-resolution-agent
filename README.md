# Customer Support Resolution Agent

A multi-agent customer support system that investigates complaints, checks refund
policy, and proposes resolutions. The LLM proposes, code decides, and a human
approves anything that moves money.

> Status: under construction (M1 — Data foundation). Full spec: [docs/prd.html](docs/prd.html).

## Architecture

_To be written._ See `docs/architecture.md` (coming soon) and `docs/decisions.md`.

## Tech stack

Python 3.12 · FastAPI · PostgreSQL 16 + pgvector · SQLAlchemy 2 (async) · Alembic ·
LangGraph · Groq / Gemini · React + TypeScript (later milestone).

## Local setup (Windows / PowerShell)

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker Desktop, Git.

```powershell
Copy-Item .env.example .env       # then edit the values
docker compose up -d              # start Postgres
cd backend
uv sync                           # install Python 3.12 + dependencies
uv run pre-commit install         # run lint/type checks before each commit
uv run alembic upgrade head       # create the tables
uv run python scripts/ingest_policies.py  # load the policies (safe to re-run)
uv run python scripts/seed_db.py  # add the demo customers and orders (safe to re-run)
```

Don't skip the policy ingestion. Refund eligibility takes the refund window from the
refund policy version in force on the date each order was placed (ADR-005 in
`docs/decisions.md`). Without the policy rows, no policy applies to any order, so every
order is refused with `NO_APPLICABLE_POLICY`. That is safe, because no money moves, but
no refund can ever be proposed.

Before running the API or the seed, set these in `.env` (see `.env.example`):

- `JWT_SECRET`: signs login tokens, at least 32 characters. Generate one with
  `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `DEMO_PASSWORD`: the password for every seeded demo account.

To start the demo data over, run `uv run python scripts/reset_demo.py`. It deletes what demos
and runs created (tickets, runs, proposals, approvals, refunds, audit events, and the mock
payment and ticketing records), seeds the scenarios again, and refuses to run in production.
Then restart the mock shipping provider.

**Demo dates.** Scenario dates are "N days ago". The seed and the mock shipping provider
both count back from one anchor, so an order's delivery date matches its tracking page.
Set `DEMO_ANCHOR_DATE=2026-11-10` in `.env` to pin it (noon UTC on that date). Left blank,
the seed uses the moment it runs and the shipping mock the moment it started: after the
date changes, run `reset_demo.py` and restart the shipping mock. A pinned date never
drifts, but refund eligibility uses the real clock, so scenarios slowly stop matching.

## Run everything with Docker (from the repo root)

One image (`backend/Dockerfile`) runs the API, the worker and the mock payment and shipping
providers. Only the command differs. A second, small image (`frontend/Dockerfile`) serves the
React app. Postgres stays the `pgvector/pgvector:pg16` image.
You need Docker Desktop and a `.env` (copy `.env.example`). Secrets are read from `.env`
when containers start and are never copied into the image.

```powershell
# 1. Build the images (code-only changes later reuse the cached dependency layers)
docker compose --profile app build

# 2. Start everything. The order is automatic: postgres (healthy) -> migrate
#    (alembic upgrade head, then exits) -> mock-payments, mock-shipping (healthy) -> api, worker
#    -> frontend
docker compose --profile app up -d
docker compose --profile app ps          # all "healthy"; migrate "Exited (0)"

# 3. Load policies and demo data (one-shot, safe to re-run; never runs on its own)
docker compose run --rm seed

# 4. Use it
Invoke-RestMethod http://localhost:8000/health   # status ok, database ok
Start-Process http://localhost:8000/docs         # interactive API docs
Start-Process http://localhost:3000              # the web app (log in with a demo account)

# 5. Logs (JSON lines); -f follows them, Ctrl+C stops following
docker compose logs -f api worker
docker compose logs mock-payments mock-shipping

# 6. Stop. Data stays in the pgdata volume; add -v to delete it too
docker compose --profile app down
```

| Service | Port | Command |
|---|---|---|
| `api` | 8000 | `uvicorn support_agent.main:create_app` (the image's default) |
| `worker` | none | `python -m support_agent.worker`; health check on its loopback metrics port |
| `mock-payments` | 8001 | `uvicorn mock_providers.payments.app:create_app` |
| `mock-shipping` | 8002 | `uvicorn mock_providers.shipping.app:create_app` |
| `migrate` | none | `alembic upgrade head`, then exits |
| `seed` | none | migrate, `scripts/ingest_policies.py`, `scripts/seed_db.py` (only with `run`) |
| `frontend` | 3000 | nginx (non-root) serving the built React app |

Notes:

- **The `app` profile** keeps these services out of a plain `docker compose up -d`, which
  still starts only Postgres for the non-Docker workflow below. Don't run both at once:
  they use the same ports.
- **Inside containers**, `DATABASE_URL` and the provider URLs from `.env` are replaced in
  `docker-compose.yml`: the database is built from `POSTGRES_USER`, `POSTGRES_PASSWORD` and
  `POSTGRES_DB` on host `postgres`, and the mocks are reached by service name. Keep
  `POSTGRES_PASSWORD` free of `@ / : #`, because it goes into a URL.
- **No Windows event-loop flag:** containers run Linux, whose default event loop already
  suits psycopg (ADR-011).
- **The embedding model** is downloaded while the image is built, so containers start
  without network access to Hugging Face (`HF_HUB_OFFLINE=1`). An onnxruntime warning
  "Failed to persist telemetry device ID" at startup is harmless.
- **The image** runs as an unprivileged user (`app`, uid 10001) and holds no tests, `.env`
  files or eval reports (`backend/.dockerignore`). Size: **842 MB** (`docker image ls
  support-agent`), of which about 390 MB is the Python packages (onnxruntime for the
  embedding model is the largest) and 65 MB the model itself.
- **Tracing:** `docker compose --profile app --profile tracing up -d` adds Jaeger; set
  `OTEL_EXPORTER=otlp` in `.env`. The containers already send to `http://jaeger:4318`.
- **The frontend image** is 82 MB. The browser calls the API directly, at the address built
  into the JavaScript: `FRONTEND_API_BASE_URL` in `.env` (default `http://localhost:8000`), then
  rebuild. That address's origin must be in the API's `CORS_ALLOWED_ORIGINS`.
- **Rebuild after code changes:** `docker compose --profile app up -d --build`.

## Run the frontend locally (from `frontend/`, next to the API)

Needs Node 22.13 or newer. Start the API as below (or with Docker), then:

```powershell
npm ci                                  # install exactly what package-lock.json lists
npm run dev                             # http://localhost:5173
```

Log in as a demo customer (`priya@example.com`) or reviewer (`anita@example.com`) with your
`DEMO_PASSWORD`. To watch a run live, the worker must be running too. The API address
defaults to `http://localhost:8000`; to change it, copy `.env.example` to `.env.local` and set
`VITE_API_BASE_URL`. Checks: `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`.
See `frontend/README.md` for the pages and the code layout, and ADR-019 for the design (live
status over Server-Sent Events, token storage, CORS).

## Run the API (from `backend/`)

```powershell
uv run uvicorn support_agent.main:create_app --factory --port 8000 `
    --loop support_agent.core.event_loop:selector_loop_factory
```

The `--loop` option is needed on Windows: the workflow checkpointer's database driver
(psycopg) cannot use Windows' default event loop (ADR-011). It is harmless elsewhere.

Open <http://localhost:8000/docs> for the interactive API docs, and check
<http://localhost:8000/health> (it reports whether the database answers).

Log in as a demo customer (`priya@example.com`, `arjun@example.com`, ...) or a reviewer
(`anita@example.com`, `karan@example.com`). The password is your `DEMO_PASSWORD`:

```powershell
$body = @{ email = "priya@example.com"; password = "<your DEMO_PASSWORD>" } | ConvertTo-Json
$login = Invoke-RestMethod -Method Post -Uri http://localhost:8000/auth/login `
  -ContentType "application/json" -Body $body
Invoke-RestMethod -Uri http://localhost:8000/me -Headers @{ Authorization = "Bearer $($login.access_token)" }
```

Every error has the same shape: `{"success": false, "error_code": "...", "message": "..."}`.

## Run the background worker (from `backend/`, next to the API)

The API never runs the agent workflow inside a request. Starting a run, a customer's reply
and a reviewer's decision on a workflow run each store a job in the `jobs` table and answer
`202 Accepted`. The worker picks the jobs up and runs the workflow (ADR-012). Without a
worker, runs stay `created` and their job stays `queued`.

Use two PowerShell windows, both in `backend/`:

```powershell
# Window 1: the API
uv run uvicorn support_agent.main:create_app --factory --port 8000 `
    --loop support_agent.core.event_loop:selector_loop_factory

# Window 2: the worker (it handles the Windows event loop itself)
uv run python -m support_agent.worker
```

Ctrl+C in the worker's window stops it gracefully: it takes no new job, lets the current
one finish (up to `WORKER_SHUTDOWN_GRACE_SECONDS`), or else puts it back in the queue. A
second Ctrl+C stops it at once; the job's lease then runs out and the next worker takes
it over, continuing from the last saved step. You can run several workers at once.

The worker also expires approvals nobody decided in time (every
`APPROVAL_EXPIRY_CHECK_SECONDS`): the paused run is escalated to a person.

Follow a run while the worker runs it:

```powershell
$h = @{ Authorization = "Bearer $($login.access_token)" }
$ticket = Invoke-RestMethod -Method Post http://localhost:8000/tickets -Headers $h `
  -ContentType "application/json" `
  -Body '{"message": "Where is my refund?", "order_number": "ORD-1001"}'
$run = Invoke-RestMethod -Method Post "http://localhost:8000/tickets/$($ticket.id)/runs" -Headers $h
$run.status            # created: queued, not run yet
Invoke-RestMethod "http://localhost:8000/runs/$($run.run_id)" -Headers $h   # status and job
```

The run's live status, as the web app sees it (ADR-019): `GET /runs/{id}/events` returns the
status, the verified reply and the steps as JSON; `GET /runs/{id}/stream` sends the same as
Server-Sent Events, then each change as it happens (the token goes in the header, never the URL):

```powershell
(Invoke-RestMethod "http://localhost:8000/runs/$($run.run_id)/events" -Headers $h).steps | Format-Table type, at
curl.exe -N -H "Authorization: Bearer $($login.access_token)" "http://localhost:8000/runs/$($run.run_id)/stream"
```

The jobs themselves:

```powershell
docker compose exec postgres psql -U support -d support_agent -c `
  "SELECT job_type, status, attempts, available_at, last_error_code FROM jobs ORDER BY created_at DESC LIMIT 10;"
```

### Orders and tickets

Customers see only their own orders and tickets. Asking for someone else's order or ticket
returns `404`, the same as one that does not exist. Reviewers get `403` on the customer-only
routes (`/me/orders`, `POST /tickets`) and can list and read all tickets.

```powershell
$h = @{ Authorization = "Bearer $($login.access_token)" }   # Priya's token from above

# Priya's orders (items and returns included), then one order
Invoke-RestMethod -Uri http://localhost:8000/me/orders -Headers $h
Invoke-RestMethod -Uri http://localhost:8000/me/orders/ORD-1001 -Headers $h

# Create a ticket (order_number is optional; it must be one of your own orders)
$body = @{ order_number = "ORD-1001"; message = "My headphones stopped working" } | ConvertTo-Json
$ticket = Invoke-RestMethod -Method Post -Uri http://localhost:8000/tickets -Headers $h `
  -ContentType "application/json" -Body $body

# List your tickets (limit 1-100, default 20; offset default 0), then fetch one
Invoke-RestMethod -Uri "http://localhost:8000/tickets?limit=10&offset=0" -Headers $h
Invoke-RestMethod -Uri "http://localhost:8000/tickets/$($ticket.id)" -Headers $h
```

Log in as `anita@example.com` and call `GET /tickets` to see every customer's tickets,
including `customer_id` and `customer_name`.

### Refunds: proposal, human approval, execution

A reviewer turns a ticket into a refund proposal. Code decides eligibility and the amount
(the request has no body, so no amount can be sent), stores the proposal once with a
SHA-256 hash, and opens an approval that expires after `APPROVAL_TTL_MINUTES` (default 60).
Approving it issues the refund exactly once through the payment provider. Customers get
`403` on all three routes.

| Endpoint | Purpose |
|---|---|
| `POST /tickets/{ticket_id}/refund-proposals` | Check eligibility and create the proposal and pending approval |
| `GET /approvals` | Pending, unexpired approvals with order, customer, amount, reasons and expiry |
| `POST /approvals/{approval_id}/decision` | `{"decision": "approve" \| "reject", "note": "..."}` |

Start the mock payments provider first (`uv run uvicorn mock_providers.payments.app:create_app --factory --port 8001`
in its own terminal), then run Priya's refund end to end:

```powershell
$api = "http://localhost:8000"
function Login($email) {
  $body = @{ email = $email; password = "<your DEMO_PASSWORD>" } | ConvertTo-Json
  $token = (Invoke-RestMethod -Method Post -Uri "$api/auth/login" -ContentType "application/json" -Body $body).access_token
  @{ Authorization = "Bearer $token" }
}
$priya = Login "priya@example.com"
$anita = Login "anita@example.com"

# 1. Priya opens a ticket about ORD-1001 (returned, inspection passed)
$body = @{ order_number = "ORD-1001"; message = "I returned my headphones, please refund" } | ConvertTo-Json
$ticket = Invoke-RestMethod -Method Post -Uri "$api/tickets" -Headers $priya -ContentType "application/json" -Body $body

# 2. Anita (reviewer) creates the proposal: amount 249900 paise comes from the rules
$proposal = Invoke-RestMethod -Method Post -Uri "$api/tickets/$($ticket.id)/refund-proposals" -Headers $anita
$proposal

# 3. The approval queue
(Invoke-RestMethod -Uri "$api/approvals" -Headers $anita).items | Format-List

# 4. Approve: the refund is reserved, then sent to the payment provider once
$body = @{ decision = "approve"; note = "Return verified" } | ConvertTo-Json
$result = Invoke-RestMethod -Method Post -Uri "$api/approvals/$($proposal.approval_id)/decision" `
  -Headers $anita -ContentType "application/json" -Body $body
$result.refund      # status "submitted"
$result.message     # states only what the provider confirmed

# 5. Approving again is refused (409 ALREADY_DECIDED); still one refund
try { Invoke-RestMethod -Method Post -Uri "$api/approvals/$($proposal.approval_id)/decision" `
  -Headers $anita -ContentType "application/json" -Body $body } catch { $_.ErrorDetails.Message }

# 6. The provider has exactly one refund for this key
Invoke-RestMethod "http://localhost:8001/refunds?idempotency_key=refund-$($proposal.proposal_id)"
```

Ineligible orders are refused with `409 NOT_ELIGIBLE` and their reason codes in
`details.reasons`, for example Rahul's `ORD-1004` (`NO_RETURN`, `OUTSIDE_RETURN_WINDOW`),
Vikram's `ORD-1006` (`INSPECTION_FAILED`) and Kavya's `ORD-1005` (`ALREADY_REFUNDED`).

What happens after a decision:

| Outcome | Refund | Run | Ticket |
|---|---|---|---|
| Rejected | none | `completed` | `open` (needs follow-up; note kept in the audit event) |
| Approval expired | none | `escalated` | `open` (create a new proposal) |
| Provider accepted | `submitted` / `processing` | `completed` | `in_progress` (resolved only on `succeeded`) |
| Reply lost (`drop_response`) | `pending_reconciliation` | `executing` | `in_progress` |
| Provider unreachable or rejected | `failed` | `failed` | `escalated` |

To see a lost reply, switch on the fault before approving: the refund stays
`pending_reconciliation`, and the provider still finds it by its key.

```powershell
Invoke-RestMethod -Method Post http://localhost:8001/_admin/faults -ContentType "application/json" `
  -Body '{"mode": "drop_response", "count": 1}'
```

Every step is in the audit trail:

```powershell
docker compose exec postgres psql -U support -d support_agent -c `
  "SELECT event_type, actor, created_at FROM audit_events ORDER BY created_at DESC LIMIT 15;"
```

## Tracing and metrics (ADR-018)

**Traces** show one run step by step: the API request, the worker job it queued (the same
trace, even though they are two processes), each workflow node, every model call and every
tool call. Spans hold ids, codes, counts, model names and timings, never customer text.

To see a run's trace in Jaeger:

```powershell
# 1. Start Jaeger (it only starts when you ask for the "tracing" profile)
docker compose --profile tracing up -d jaeger

# 2. In .env set:  OTEL_EXPORTER=otlp   then restart the API and the worker

# 3. Start a run (see "Run the background worker"), wait for the worker to finish it

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

## Ingest policies (from `backend/`)

The support policies live in `data/policies/` as Markdown files with YAML front matter
(`policy_key`, `version`, `title`, `effective_from`, `effective_to`, and
`refund_window_days` for the refund policy). Each `## N. Title` section becomes one
searchable chunk with a stable citation id such as `refund_policy_v2:section_3`.

Run this right after every `alembic upgrade head` (it is part of Local setup above).
Eligibility reads each order's refund window from these rows. Without them, every order
gets `NO_APPLICABLE_POLICY`.

```powershell
uv run alembic upgrade head
uv run python scripts/ingest_policies.py
```

The first run downloads the embedding model (BAAI/bge-small-en-v1.5, about 70 MB) into
fastembed's cache. On Windows you may see warnings about symlinks or `HF_TOKEN`; they are
harmless. Running the script again is safe: unchanged sections are skipped, edited
sections are re-embedded and updated in place, and removed sections are deleted.
Everything happens in one transaction, so a failure leaves the database unchanged.

```
Embedding model: BAAI/bge-small-en-v1.5
               created  updated  unchanged  deleted
  documents          0        0          6        -
  chunks             0        0         36        0
```

Set `EMBEDDING_PROVIDER=fake` to use deterministic stand-in vectors with no download
(tests and CI always do). The fake vectors carry no meaning, so don't use them for search.

## Development commands (from `backend/`)

```powershell
uv run pytest
uv run ruff check . ; uv run ruff format .
uv run mypy src tests scripts
```

## Mock services (from `backend/`)

Fake third-party providers used in development and tests. Run each one in its own
terminal. Each has interactive API docs at `/docs`.

| Provider | Port | Storage | Start command |
|---|---|---|---|
| Payments | 8001 | Postgres schema `mock_payments` | `uv run uvicorn mock_providers.payments.app:create_app --factory --port 8001` |
| Shipping | 8002 | In memory, from `data/mock_shipping/shipments.json` | `uv run uvicorn mock_providers.shipping.app:create_app --factory --port 8002` |
| Ticketing | 8003 | Postgres schema `mock_ticketing` | `uv run uvicorn mock_providers.ticketing.app:create_app --factory --port 8003` |

Payments and ticketing need Postgres running (`docker compose up -d`) and create their
own schema on startup. Shipping needs no database; its dates are written as "days ago"
so the demo data stays current.

Try them:

```powershell
# Arjun's delayed laptop
Invoke-RestMethod http://localhost:8002/shipments/ORD-1002

# Create a ticket (writes need an Idempotency-Key; reusing it returns the original)
$ticket = Invoke-RestMethod -Method Post http://localhost:8003/tickets -ContentType "application/json" `
  -Headers @{ "Idempotency-Key" = "demo-ticket-1" } `
  -Body '{"customer_reference": "cust_arjun", "order_reference": "ORD-1002", "subject": "Laptop late", "description": "In transit for 8 days"}'

# Add a note, then change the status (open, pending, resolved, closed)
Invoke-RestMethod -Method Post "http://localhost:8003/tickets/$($ticket.id)/notes" -ContentType "application/json" `
  -Headers @{ "Idempotency-Key" = "demo-note-1" } -Body '{"body": "Held at Nagpur hub."}'
Invoke-RestMethod -Method Patch "http://localhost:8003/tickets/$($ticket.id)" -ContentType "application/json" `
  -Body '{"status": "pending"}'
```

Failures can be switched on for the next N requests (not available when `APP_ENV=production`):

```powershell
Invoke-RestMethod -Method Post http://localhost:8001/_admin/faults -ContentType "application/json" `
  -Body '{"mode": "drop_response", "count": 1}'
```

Modes: `none`, `slow` (needs `delay_ms`), `error`, `drop_response`, plus `decline` on
payments only. `drop_response` does the work and then replies 504, as if the reply was lost.

## Demo

_To be added._

## License

MIT — see [LICENSE](LICENSE).
