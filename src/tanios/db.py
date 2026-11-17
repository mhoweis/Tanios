"""Database connection."""

import logging
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)


class DatabaseLike(Protocol):
    """What the app needs from a database. Tests pass a stand-in with this shape."""

    async def ping(self) -> bool: ...

    async def dispose(self) -> None: ...


class Database:
    def __init__(self, url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    async def ping(self) -> bool:
        """True when a trivial query succeeds. Never raises."""
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as error:
            # The exception type is enough to diagnose; its message can contain the
            # connection URL, which holds the password.
            logger.warning("database ping failed: %s", type(error).__name__)
            return False
        return True

    async def dispose(self) -> None:
        await self._engine.dispose()
