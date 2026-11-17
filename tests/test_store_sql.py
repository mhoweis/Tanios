"""Milestone 1: the real database store and its migration.

Needs a running Postgres: runs in CI, and locally when TANIOS_TEST_DATABASE_URL is set.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from tanios.db import Database
from tanios.migrate import upgrade
from tanios.store import SqlStore, UsageEntry, normalize_phone

DATABASE_URL = os.environ.get("TANIOS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set TANIOS_TEST_DATABASE_URL to run against a real database"
)

TABLES = (
    "users, strangers, processed_messages, usage_ledger, "
    "gmail_connections, gmail_links, emails, email_drafts, user_keys, setup_tokens"
)


async def owner(store: SqlStore, phone: str = "962790000001", cap: str | None = None, **kw):
    await store.ensure_owner(
        phone,
        monthly_cap_usd=None if cap is None else Decimal(cap),
        allow_free_for_private=kw.get("allow_free_for_private", False),
    )
    return await store.find_user(phone, None)


NOVEMBER = datetime(2026, 11, 1, tzinfo=UTC)


def usage(user_id: int, cost: str, *, task="chat", model="small-model", days=14, **kw):
    return UsageEntry(
        user_id=user_id,
        at=NOVEMBER + timedelta(days=days),
        task=task,
        tier=kw.get("tier", "small"),
        provider="openrouter",
        model=model,
        cost_usd=Decimal(cost),
        prompt_tokens=100,
        completion_tokens=50,
        is_free=kw.get("is_free", False),
        private=kw.get("private", False),
        outcome=kw.get("outcome", "ok"),
    )


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
        await owner(store, "+962 79 000 0001")
        await owner(store)
        return await store.find_user("962790000001", None)

    user = run(scenario)

    assert user is not None
    assert user.id == 1
    assert user.phone == "962790000001"
    assert user.paused is False


def test_w3_an_unregistered_number_is_not_found():
    async def scenario(store: SqlStore):
        await owner(store)
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
        await owner(store)
        user = await store.find_user("962790000001", None)
        await store.set_paused(user.id, True)
        paused = await store.find_user("962790000001", None)
        await store.set_paused(user.id, False)
        resumed = await store.find_user("962790000001", None)
        return paused.paused, resumed.paused

    assert run(scenario) == (True, False)


def test_a_known_number_teaches_tanios_the_whatsapp_user_id_for_later():
    async def scenario(store: SqlStore):
        await owner(store)
        before = await store.find_user(None, "JO.111")
        await store.find_user("962790000001", "JO.111")
        after = await store.find_user(None, "JO.111")
        return before, after

    before, after = run(scenario)

    assert before is None
    assert after is not None and after.phone == "962790000001"


def test_phone_numbers_are_compared_by_their_digits():
    assert normalize_phone("+962 (79) 000-0001") == "962790000001"


# Milestone 2: budget settings and the usage ledger -------------------------------------


def test_r4_the_owners_cap_follows_the_server_settings_on_every_start():
    async def scenario(store: SqlStore):
        first = await owner(store, cap="5")
        second = await owner(store, cap="7.5", allow_free_for_private=True)
        third = await owner(store, cap=None)
        return first, second, third

    first, second, third = run(scenario)

    assert first.monthly_cap_usd == Decimal("5")
    assert first.allow_free_for_private is False
    assert second.monthly_cap_usd == Decimal("7.5")
    assert second.allow_free_for_private is True
    assert third.monthly_cap_usd is None
    assert first.id == second.id == third.id


def test_r4_spend_is_the_exact_sum_of_this_months_ledger_for_one_user():
    async def scenario(store: SqlStore):
        me = await owner(store)
        await store.ensure_owner("15550001111", monthly_cap_usd=None, allow_free_for_private=False)
        someone_else = await store.find_user("15550001111", None)
        await store.record_usage(usage(me.id, "0.000100"))
        await store.record_usage(usage(me.id, "0.000200"))
        await store.record_usage(usage(me.id, "0.000001", outcome="failed_check"))
        await store.record_usage(usage(me.id, "9", days=-3))  # October
        await store.record_usage(usage(someone_else.id, "5"))
        return await store.spend_since(me.id, NOVEMBER)

    assert run(scenario) == Decimal("0.000301")


def test_r4_spend_is_zero_with_an_empty_ledger():
    async def scenario(store: SqlStore):
        me = await owner(store)
        return await store.spend_since(me.id, NOVEMBER)

    assert run(scenario) == Decimal(0)


def test_r1_the_ledger_summary_groups_by_task_and_by_model():
    async def scenario(store: SqlStore):
        me = await owner(store)
        await store.record_usage(usage(me.id, "0.10", task="chat", model="small-model"))
        await store.record_usage(usage(me.id, "0.10", task="chat", model="small-model"))
        await store.record_usage(usage(me.id, "0.50", task="draft", model="medium-model"))
        await store.record_usage(usage(me.id, "0", task="sort", model="free-model", is_free=True))
        await store.record_usage(usage(me.id, "3", task="chat", days=-10))  # October
        return await store.usage_summary(me.id, NOVEMBER)

    summary = run(scenario)

    assert summary.total_usd == Decimal("0.70")
    assert summary.by_task == (
        ("draft", Decimal("0.50"), 1),
        ("chat", Decimal("0.20"), 2),
        ("sort", Decimal("0"), 1),
    )
    assert summary.by_model == (("small-model", 2), ("free-model", 1), ("medium-model", 1))


def test_r4_the_warning_and_cap_notices_are_remembered_per_month():
    async def scenario(store: SqlStore):
        me = await owner(store, cap="5")
        await store.mark_cap_warned(me.id, "2026-11")
        await store.mark_cap_reached(me.id, "2026-11")
        return await store.find_user("962790000001", None)

    user = run(scenario)

    assert user.cap_warned_month == "2026-11"
    assert user.cap_reached_month == "2026-11"
