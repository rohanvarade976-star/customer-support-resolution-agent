from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def ping(session: AsyncSession) -> None:
    """Run the cheapest possible query; raises if the database cannot answer."""
    await session.execute(text("SELECT 1"))
