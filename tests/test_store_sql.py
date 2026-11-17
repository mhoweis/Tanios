"""Milestone 1: the real database store and its migration.

Needs a running Postgres: runs in CI, and locally when TANIOS_TEST_DATABASE_URL is set.
"""

import asyncio
import os

import pytest
from sqlalchemy import text

from tanios.db import Database
from tanios.migrate import upgrade
from tanios.store import SqlStore, normalize_phone

DATABASE_URL = os.environ.get("TANIOS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set TANIOS_TEST_DATABASE_URL to run against a real database"
)

TABLES = "users, strangers, processed_messages"


def run(scenario):
    """Run one async scenario against a migrated, emptied database."""

    async def wrapped():
        database = Database(DATABASE_URL)
        try:
            async with database.sessions.begin() as session:
                await session.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY"))
            return await scenario(SqlStore(database.sessions))
        finally:
            await database.dispose()

    return asyncio.run(wrapped())


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    assert upgrade(DATABASE_URL) is True


def test_migrations_can_be_applied_again_without_error():
    assert upgrade(DATABASE_URL) is True


def test_the_owner_is_registered_once_however_often_the_server_starts():
    async def scenario(store: SqlStore):
        await store.ensure_owner("+962 79 000 0001")
        await store.ensure_owner("962790000001")
        return await store.find_user("962790000001", None)

    user = run(scenario)

    assert user is not None
    assert user.id == 1
    assert user.phone == "962790000001"
    assert user.paused is False


def test_w3_an_unregistered_number_is_not_found():
    async def scenario(store: SqlStore):
        await store.ensure_owner("962790000001")
        return await store.find_user("15550009999", None)

    assert run(scenario) is None


def test_a_message_id_can_be_claimed_exactly_once():
    async def scenario(store: SqlStore):
        return [await store.claim_message("wamid.A") for _ in range(3)]

    assert run(scenario) == [True, False, False]


def test_two_deliveries_of_one_message_at_the_same_moment_yield_one_claim():
    async def scenario(store: SqlStore):
        return await asyncio.gather(*(store.claim_message("wamid.race") for _ in range(8)))

    assert sorted(run(scenario)) == [False] * 7 + [True]


def test_w3_a_stranger_counts_as_first_contact_only_once():
    async def scenario(store: SqlStore):
        return [await store.first_contact_from_stranger("15550009999") for _ in range(3)]

    assert run(scenario) == [True, False, False]


def test_w4_pause_and_resume_are_remembered():
    async def scenario(store: SqlStore):
        await store.ensure_owner("962790000001")
        user = await store.find_user("962790000001", None)
        await store.set_paused(user.id, True)
        paused = await store.find_user("962790000001", None)
        await store.set_paused(user.id, False)
        resumed = await store.find_user("962790000001", None)
        return paused.paused, resumed.paused

    assert run(scenario) == (True, False)


def test_a_known_number_teaches_tanios_the_whatsapp_user_id_for_later():
    async def scenario(store: SqlStore):
        await store.ensure_owner("962790000001")
        before = await store.find_user(None, "JO.111")
        await store.find_user("962790000001", "JO.111")
        after = await store.find_user(None, "JO.111")
        return before, after

    before, after = run(scenario)

    assert before is None
    assert after is not None and after.phone == "962790000001"


def test_phone_numbers_are_compared_by_their_digits():
    assert normalize_phone("+962 (79) 000-0001") == "962790000001"
