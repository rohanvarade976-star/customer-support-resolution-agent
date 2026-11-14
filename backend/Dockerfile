# One image for the API, the worker and the mock providers; docker-compose.yml picks the
# command. Build from the repo root:  docker compose build
# No secrets are baked in: settings come from environment variables at runtime.

# ------------------------------------------------------------------ builder
# Installs the dependencies and downloads the embedding model. Nothing from this stage
# reaches the final image except /app (code + .venv) and the model cache.
FROM python:3.12-slim-bookworm AS builder

# uv itself, copied from its official image (pinned; matches uv_build<0.14 in pyproject).
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/backend/.venv \
    FASTEMBED_CACHE_PATH=/opt/fastembed

WORKDIR /app/backend

# 1. Dependencies only. This layer is rebuilt only when pyproject.toml or uv.lock change,
#    so editing source code does not reinstall everything.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 2. The source, then the project itself (editable, so the code stays at /app/backend/src
#    and its "walk up to backend/" paths find data/ exactly as in the repo).
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 3. Download the embedding model now, so containers never fetch it at startup.
RUN /app/backend/.venv/bin/python -c \
    "from fastembed import TextEmbedding; from support_agent.rag.embeddings import FASTEMBED_MODEL; TextEmbedding(model_name=FASTEMBED_MODEL)"

# ------------------------------------------------------------------ runtime
FROM python:3.12-slim-bookworm AS runtime

# A fixed, unprivileged user. The app never needs root.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

ENV PATH="/app/backend/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed \
    HF_HUB_OFFLINE=1

WORKDIR /app/backend

# Code and model are owned by root and only readable by the app user: it cannot modify
# them. The model cache is owned by the app user, because loading it may write lock files.
COPY --from=builder /app/backend /app/backend
COPY --from=builder --chown=app:app /opt/fastembed /opt/fastembed

USER app

EXPOSE 8000

# The default command is the API. Linux's default event loop already suits psycopg, so
# the Windows-only --loop option is not needed (ADR-011).
CMD ["uvicorn", "support_agent.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
