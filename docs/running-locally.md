# Running locally without Docker

The step-by-step development setup for Windows / PowerShell: Postgres in Docker, the API, the worker
and the frontend as separate processes. For the one-command full stack, see the
[README quick start](../README.md#run-it-locally) and [docker.md](docker.md).

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
See `frontend/README.md` for the pages and the code layout, ADR-019 for the design (live
status over Server-Sent Events, token storage, CORS), and ADR-020 for the design system.

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
