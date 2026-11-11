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
uv run python scripts/seed_db.py  # add the demo customers and orders (safe to re-run)
```

Before running the API or the seed, set these in `.env` (see `.env.example`):

- `JWT_SECRET`: signs login tokens, at least 32 characters. Generate one with
  `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `DEMO_PASSWORD`: the password for every seeded demo account.

To start the demo data over: `uv run python scripts/seed_db.py --reset` and then seed again.

## Run the API (from `backend/`)

```powershell
uv run uvicorn support_agent.main:create_app --factory --port 8000
```

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
