"""Milestone 7: testers, keys and setup links in a real database (U1, U2, U3, W5).

Needs a running Postgres: runs in CI, and locally when TANIOS_TEST_DATABASE_URL is set.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from tanios.crypto import TokenBox
from tanios.db import Database
from tanios.keys import SqlUserKeys
from tanios.mailstore import SortedEmail, SqlMailStore
from tanios.migrate import upgrade
from tanios.setup import SqlSetupTokens
from tanios.store import SqlStore, UsageEntry
from tests.test_store_sql import DATABASE_URL, TABLES

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set TANIOS_TEST_DATABASE_URL to run against a real database"
)

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
OWNER = "962790000001"
TESTER = "962790000002"


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    assert upgrade(DATABASE_URL) is True


def run(scenario):
    async def wrapped():
        database = Database(DATABASE_URL)
        try:
            async with database.sessions.begin() as session:
                await session.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY"))
            store = SqlStore(database.sessions)
            await store.ensure_owner(OWNER, monthly_cap_usd=None, allow_free_for_private=False)
            return await scenario(store, database)
        finally:
            await database.dispose()

    return asyncio.run(wrapped())


def test_u1_a_tester_is_added_once_and_listed_and_the_owner_is_marked():
    async def scenario(store, database):
        added = await store.add_tester(TESTER, Decimal("5"))
        again = await store.add_tester(f"+{TESTER}", Decimal("9"))
        owner_again = await store.add_tester(OWNER, None)
        owner = await store.find_user(OWNER, None)
        return added, again, owner_again, owner, await store.testers()

    added, again, owner_again, owner, testers = run(scenario)

    assert added.phone == TESTER and added.is_owner is False
    assert added.monthly_cap_usd == Decimal("5")
    assert again is None and owner_again is None
    assert owner.is_owner is True
    assert [tester.phone for tester in testers] == [TESTER]


def test_u1_r4_a_new_budget_clears_the_months_warnings():
    async def scenario(store, database):
        tester = await store.add_tester(TESTER, Decimal("5"))
        await store.mark_cap_warned(tester.id, "2026-11")
        await store.mark_cap_reached(tester.id, "2026-11")
        await store.set_budget(tester.id, Decimal("12.5"), True)
        return await store.user_by_id(tester.id)

    after = run(scenario)

    assert after.monthly_cap_usd == Decimal("12.5") and after.allow_free_for_private
    assert after.cap_warned_month is None and after.cap_reached_month is None


def test_w5_the_time_of_the_last_template_is_kept():
    async def scenario(store, database):
        owner = await store.find_user(OWNER, None)
        await store.mark_template(owner.id, NOW)
        return await store.user_by_id(owner.id)

    assert run(scenario).template_sent_at == NOW


def test_u2_r2_keys_are_stored_sealed_and_read_back_per_user_only():
    box = TokenBox(Fernet.generate_key().decode())

    async def scenario(store, database):
        tester = await store.add_tester(TESTER, None)
        owner = await store.find_user(OWNER, None)
        keys = SqlUserKeys(database.sessions, box)
        await keys.put(tester.id, "OpenRouter", "sk-or-tester-1")
        await keys.put(tester.id, "openrouter", "sk-or-tester-2")  # replaces the first
        await keys.put(owner.id, "other", "sk-other-owner")
        async with database.sessions.begin() as session:
            stored = [
                row[0] for row in await session.execute(text("SELECT sealed_key FROM user_keys"))
            ]
        mine, theirs = await keys.keys_for(tester.id), await keys.keys_for(owner.id)
        await keys.remove(tester.id, "OPENROUTER")
        return stored, mine, theirs, await keys.keys_for(tester.id)

    stored, mine, theirs, after = run(scenario)

    assert mine == {"openrouter": "sk-or-tester-2"}
    assert theirs == {"other": "sk-other-owner"}
    assert after == {}
    assert len(stored) == 2 and not any("sk-" in value for value in stored)


def test_u2_a_key_sealed_with_another_server_key_is_ignored_not_shown():
    async def scenario(store, database):
        tester = await store.add_tester(TESTER, None)
        await SqlUserKeys(database.sessions, TokenBox(Fernet.generate_key().decode())).put(
            tester.id, "openrouter", "sk-or-old"
        )
        other = SqlUserKeys(database.sessions, TokenBox(Fernet.generate_key().decode()))
        return await other.keys_for(tester.id)

    assert run(scenario) == {}


def test_u1_a_setup_link_is_used_once_and_a_new_link_replaces_the_old():
    async def scenario(store, database):
        tester = await store.add_tester(TESTER, None)
        tokens = SqlSetupTokens(database.sessions)
        await tokens.add("old", tester.id, "link", NOW + HOUR)
        await tokens.add("new", tester.id, "link", NOW + HOUR)
        await tokens.add("session-a", tester.id, "session", NOW + HOUR)
        return (
            await tokens.use("old", "link", NOW),
            await tokens.use("new", "session", NOW),  # wrong kind
            await tokens.use("new", "link", NOW),
            await tokens.use("new", "link", NOW),
            await tokens.holder("session-a", "session", NOW),
            await tokens.holder("session-a", "session", NOW + 2 * HOUR),
        )

    tester_id = 2
    assert run(scenario) == (None, None, tester_id, None, tester_id, None)


def test_u1_eight_uses_of_one_link_at_once_yield_one_session():
    async def scenario(store, database):
        tester = await store.add_tester(TESTER, None)
        tokens = SqlSetupTokens(database.sessions)
        await tokens.add("link", tester.id, "link", NOW + HOUR)
        return await asyncio.gather(*(tokens.use("link", "link", NOW) for _ in range(8)))

    assert sorted(run(scenario), key=str) == [2] + [None] * 7


def test_u1_ending_a_session_removes_it():
    async def scenario(store, database):
        tester = await store.add_tester(TESTER, None)
        tokens = SqlSetupTokens(database.sessions)
        await tokens.add("s", tester.id, "session", NOW + HOUR)
        await tokens.end("s")
        return await tokens.holder("s", "session", NOW)

    assert run(scenario) is None


def test_u3_deleting_a_user_leaves_no_row_of_theirs_in_any_table():
    """Every table that names a user is checked, so a new table cannot be forgotten."""
    box = TokenBox(Fernet.generate_key().decode())

    async def scenario(store, database):
        owner = await store.find_user(OWNER, None)
        tester = await store.add_tester(TESTER, None)
        mail = SqlMailStore(database.sessions)
        for user in (owner, tester):
            await store.record_usage(
                UsageEntry(
                    user_id=user.id,
                    at=NOW,
                    task="chat",
                    tier="small",
                    provider="openrouter",
                    model="m",
                    cost_usd=Decimal("0.01"),
                    prompt_tokens=1,
                    completion_tokens=1,
                    is_free=False,
                    private=False,
                    outcome="ok",
                )
            )
            await SqlUserKeys(database.sessions, box).put(user.id, "openrouter", "sk-or-x")
            await SqlSetupTokens(database.sessions).add(f"t{user.id}", user.id, "session", NOW)
            await mail.save_connection(user.id, f"u{user.id}@example.com", "sealed", NOW)
            await mail.add_link(f"l{user.id}", user.id, NOW + HOUR)
            await mail.add_email(
                SortedEmail(
                    user_id=user.id,
                    gmail_id="m1",
                    thread_id="t1",
                    category="fyi",
                    sender="x",
                    subject="y",
                    summary="z",
                    received_at=NOW,
                ),
                NOW,
            )
            await mail.add_draft(
                user_id=user.id,
                gmail_draft_id="r1",
                gmail_id="m1",
                thread_id="t1",
                to=("a@example.com",),
                cc=(),
                thread_addresses=(),
                subject="Re: y",
                body="hi",
                now=NOW,
                expires_at=NOW + HOUR,
            )

        await store.delete_user(tester.id)

        async with database.sessions.begin() as session:
            tables = [
                row[0]
                for row in await session.execute(
                    text(
                        "SELECT DISTINCT table_name FROM information_schema.columns "
                        "WHERE column_name = 'user_id' AND table_schema = 'public'"
                    )
                )
            ]
            left = {}
            for table in sorted(tables):
                rows = await session.execute(text(f"SELECT user_id FROM {table}"))
                left[table] = sorted({row[0] for row in rows})
            users = [row[0] for row in await session.execute(text("SELECT id FROM users"))]
        return owner.id, tester.id, left, users

    owner_id, tester_id, left, users = run(scenario)

    assert set(left) == {
        "email_drafts",
        "emails",
        "gmail_connections",
        "gmail_links",
        "setup_tokens",
        "usage_ledger",
        "user_keys",
    }
    assert all(ids == [owner_id] for ids in left.values()), left
    assert users == [owner_id]
