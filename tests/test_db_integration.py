"""Exercises the real database driver.

The first test needs a running Postgres: it runs in CI, and locally when
TANIOS_TEST_DATABASE_URL is set. The second needs nothing and always runs.
"""

import asyncio
import os

import pytest

from tanios.db import Database

DATABASE_URL = os.environ.get("TANIOS_TEST_DATABASE_URL")


async def ping(url: str) -> bool:
    database = Database(url)
    try:
        return await database.ping()
    finally:
        await database.dispose()


@pytest.mark.skipif(
    not DATABASE_URL, reason="set TANIOS_TEST_DATABASE_URL to run against a real database"
)
def test_ping_succeeds_against_a_real_database():
    assert asyncio.run(ping(DATABASE_URL)) is True


def test_ping_reports_false_instead_of_raising_when_the_server_is_unreachable():
    # Port 1 on localhost: nothing listens there.
    assert asyncio.run(ping("postgresql+asyncpg://tanios:wrong@127.0.0.1:1/tanios")) is False


def test_a_failed_ping_does_not_log_the_password(caplog):
    with caplog.at_level("WARNING"):
        asyncio.run(ping("postgresql+asyncpg://tanios:s3cret-pass@127.0.0.1:1/tanios"))

    assert "database ping failed" in caplog.text
    assert "s3cret-pass" not in caplog.text
