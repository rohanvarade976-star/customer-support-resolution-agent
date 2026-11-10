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

To start the demo data over: `uv run python scripts/seed_db.py --reset` and then seed again.

## Development commands (from `backend/`)

```powershell
uv run pytest
uv run ruff check . ; uv run ruff format .
uv run mypy src tests scripts
```

## Mock services (from `backend/`)

Fake third-party providers used in development and tests. They need Postgres running.

```powershell
# Mock payment provider on http://localhost:8001 (API docs at /docs)
uv run uvicorn mock_providers.payments.app:create_app --factory --port 8001
```

Failures can be switched on for the next N requests (not available when `APP_ENV=production`):

```powershell
Invoke-RestMethod -Method Post http://localhost:8001/_admin/faults -ContentType "application/json" `
  -Body '{"mode": "drop_response", "count": 1}'
```

Modes: `none`, `slow` (needs `delay_ms`), `error`, `drop_response`, `decline`.

## Demo

_To be added._

## License

MIT — see [LICENSE](LICENSE).
