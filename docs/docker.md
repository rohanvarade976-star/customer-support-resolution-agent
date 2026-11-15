# Run everything with Docker

The full detail behind the README quick start. Run every command from the repo root.

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
  still starts only Postgres for the non-Docker workflow in [running-locally.md](running-locally.md). Don't run both at once:
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
