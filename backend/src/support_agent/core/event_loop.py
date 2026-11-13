"""The asyncio event loop on Windows (ADR-011).

The checkpointer talks to Postgres through psycopg 3, whose async mode cannot run on
Windows' default "Proactor" event loop; it needs the "Selector" loop. asyncpg (the rest of
the app) works on both. On Linux and macOS the default loop is already a selector loop, so
nothing changes there (CI and production).

- The API: uvicorn takes a loop factory with `--loop`:
      uv run uvicorn support_agent.main:create_app --factory --port 8000 `
          --loop support_agent.core.event_loop:selector_loop_factory
- Scripts: `run(main())` instead of `asyncio.run(main())`.
- Tests: tests/integration/conftest.py sets the same loop for pytest-asyncio.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """For uvicorn's `--loop module:function` option: uvicorn calls it to create the loop."""
    return asyncio.SelectorEventLoop()


def run[T](main: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run` on a selector loop, so psycopg works on Windows too."""
    return asyncio.run(main, loop_factory=asyncio.SelectorEventLoop)
