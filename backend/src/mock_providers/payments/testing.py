"""The mock payment provider in memory, for tests: no server, no network.

The app's PaymentClient talks to the mock's ASGI app directly, and `refund_count` counts the
refunds the provider holds, so a test can check money at the provider as well as in our own
refund_operations table.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from mock_providers.payments.app import create_app
from mock_providers.payments.config import PaymentsSettings
from support_agent.core.config import Settings
from support_agent.integrations.payments import PaymentClient


@dataclass
class InMemoryPayments:
    app: FastAPI
    engine: AsyncEngine

    def client(self) -> PaymentClient:
        settings = Settings(
            _env_file=None,
            database_url=SecretStr("postgresql+asyncpg://user:pw@localhost:5432/unused"),
            jwt_secret=SecretStr("x" * 32),
            payments_base_url="http://payments.mock",
        )
        return PaymentClient(settings, httpx.ASGITransport(app=self.app))

    async def refund_count(self) -> int:
        async with self.engine.connect() as conn:
            return int(await conn.scalar(text("SELECT count(*) FROM mock_payments.refunds")) or 0)


@asynccontextmanager
async def in_memory_payments(database_url: str) -> AsyncIterator[InMemoryPayments]:
    """The mock provider on `database_url`, starting with no refunds."""
    settings = PaymentsSettings(app_env="test", database_url=SecretStr(database_url))
    app = create_app(settings)
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with app.router.lifespan_context(app):  # ASGITransport does not run lifespan
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE mock_payments.refunds"))
        yield InMemoryPayments(app, engine)
    await engine.dispose()
