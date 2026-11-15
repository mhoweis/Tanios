"""Alembic environment. Driven by tanios.migrate, or by the alembic command for authors."""

import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from tanios.config import get_settings
from tanios.tables import Base

target_metadata = Base.metadata


def database_url() -> str:
    url = context.config.attributes.get("database_url") or get_settings().database_url
    if not url:
        raise RuntimeError("set TANIOS_DATABASE_URL to run migrations")
    return url


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported; connect to a database")

asyncio.run(run_online())
