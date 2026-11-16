"""Milestone 5: the mail tables in a real database (E1, E2, E3, E5).

Needs a running Postgres: runs in CI, and locally when TANIOS_TEST_DATABASE_URL is set.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from tanios.db import Database
from tanios.mailstore import SortedEmail, SqlMailStore
from tanios.migrate import upgrade
from tanios.store import SqlStore
from tests.test_store_sql import DATABASE_URL, TABLES

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set TANIOS_TEST_DATABASE_URL to run against a real database"
)

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    assert upgrade(DATABASE_URL) is True


def run(scenario):
    """Run one scenario against an emptied database that holds two users, 1 and 2."""

    async def wrapped():
        database = Database(DATABASE_URL)
        try:
            async with database.sessions.begin() as session:
                await session.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY"))
                await session.execute(
                    text("INSERT INTO users (phone) VALUES ('962790000001'), ('962790000002')")
                )
            return await scenario(SqlMailStore(database.sessions), database)
        finally:
            await database.dispose()

    return asyncio.run(wrapped())


def sorted_email(gmail_id: str, category: str = "fyi", user_id: int = 1, minutes: int = 0):
    return SortedEmail(
        user_id=user_id,
        gmail_id=gmail_id,
        thread_id=f"t-{gmail_id}",
        category=category,
        sender="Rana Haddad",
        subject=f"Subject {gmail_id}",
        summary=f"About {gmail_id}.",
        received_at=NOW + timedelta(minutes=minutes),
    )


async def a_draft(store: SqlMailStore, user_id: int = 1, **overrides):
    fields = {
        "user_id": user_id,
        "gmail_draft_id": "r-1",
        "gmail_id": "m1",
        "thread_id": "t-m1",
        "to": ("rana@example.com",),
        "cc": ("sami@example.com",),
        "thread_addresses": ("rana@example.com", "owner@example.com"),
        "subject": "Re: Invoice",
        "body": "Yes.",
        "now": NOW,
        "expires_at": NOW + 24 * HOUR,
    }
    fields.update(overrides)
    return await store.add_draft(**fields)


def test_user_last_message_time_is_kept_and_read_back():
    async def scenario(store, database):
        users = SqlStore(database.sessions)
        before = await users.user_by_id(1)
        await users.note_message(1, NOW)
        return before, await users.user_by_id(1), await users.user_by_id(99)

    before, after, nobody = run(scenario)

    assert before.last_message_at is None and before.phone == "962790000001"
    assert after.last_message_at == NOW
    assert nobody is None


def test_e1_a_sign_in_link_is_found_by_its_hash_and_answers_once():
    async def scenario(store, database):
        await store.add_link("hash-a", 1, NOW + HOUR)
        peek = [await store.link_user("hash-a", NOW), await store.link_user("hash-a", NOW)]
        used = [await store.use_link("hash-a", NOW), await store.use_link("hash-a", NOW)]
        return peek, used, await store.link_user("hash-a", NOW), await store.use_link("nope", NOW)

    assert run(scenario) == ([1, 1], [1, None], None, None)


def test_e1_two_uses_of_one_link_at_the_same_moment_yield_one_winner():
    async def scenario(store, database):
        await store.add_link("hash-a", 1, NOW + HOUR)
        return await asyncio.gather(*(store.use_link("hash-a", NOW) for _ in range(8)))

    assert sorted(run(scenario), key=str) == [1] + [None] * 7


def test_e1_an_expired_link_is_dead_and_a_new_link_replaces_the_old_one():
    async def scenario(store, database):
        await store.add_link("old", 1, NOW + HOUR)
        await store.add_link("new", 1, NOW + HOUR)
        await store.add_link("other-user", 2, NOW + HOUR)
        return (
            await store.link_user("old", NOW),
            await store.link_user("new", NOW),
            await store.link_user("new", NOW + HOUR),
            await store.use_link("new", NOW + 2 * HOUR),
            await store.link_user("other-user", NOW),
        )

    assert run(scenario) == (None, 1, None, None, 2)


def test_e1_a_connection_is_saved_replaced_and_dropped():
    async def scenario(store, database):
        assert await store.connection(1) is None
        await store.save_connection(1, "owner@example.com", "sealed-1", NOW)
        await store.mark_checked(1, NOW + HOUR)
        await store.set_digest_times(1, ("08:00", "18:00"))
        await store.mark_digest(1, "2026-11-15T08:00")
        first = await store.connection(1)
        # Connecting again starts fresh checks but keeps the summary times.
        await store.save_connection(1, "other@example.com", "sealed-2", NOW + 2 * HOUR)
        second = await store.connection(1)
        await store.save_connection(2, "two@example.com", "sealed-3", NOW)
        everyone = await store.connections()
        await store.drop_connection(1)
        return first, second, everyone, await store.connection(1), await store.connections()

    first, second, everyone, gone, left = run(scenario)

    assert (first.address, first.sealed_token, first.last_checked_at) == (
        "owner@example.com",
        "sealed-1",
        NOW + HOUR,
    )
    assert first.digest_times == ("08:00", "18:00") and first.last_digest_key == "2026-11-15T08:00"
    assert (second.address, second.sealed_token, second.last_checked_at) == (
        "other@example.com",
        "sealed-2",
        None,
    )
    assert second.connected_at == NOW + 2 * HOUR and second.digest_times == ("08:00", "18:00")
    assert [connection.user_id for connection in everyone] == [1, 2]
    assert gone is None and [connection.user_id for connection in left] == [2]


def test_e2_an_email_is_recorded_once_per_user():
    async def scenario(store, database):
        await store.add_email(sorted_email("m1"), NOW)
        await store.add_email(sorted_email("m1", category="newsletter"), NOW)  # a repeat
        await store.add_email(sorted_email("m1", user_id=2), NOW)  # another user's mailbox
        return (
            await store.known_ids(1, ["m1", "m2"]),
            await store.known_ids(1, []),
            await store.unreported(1),
            await store.unreported(2),
        )

    known, none, mine, theirs = run(scenario)

    assert known == {"m1"} and none == set()
    assert [(e.gmail_id, e.category) for e in mine] == [("m1", "fyi")]
    assert [e.user_id for e in theirs] == [2]


def test_e3_showing_a_list_numbers_it_and_marks_it_as_seen():
    async def scenario(store, database):
        for number, category in enumerate(["fyi", "needs_reply", "newsletter", "fyi"]):
            await store.add_email(sorted_email(f"m{number}", category, minutes=number), NOW)
        await store.add_email(sorted_email("x1", user_id=2), NOW)
        waiting = [e.gmail_id for e in await store.unreported(1)]
        await store.show_list(1, ["m1", "m0"], ["m2"])
        after = [e.gmail_id for e in await store.unreported(1)]
        first = await store.email_number(1, 1)
        second = await store.email_number(1, 2)
        await store.show_list(1, ["m3"], [])  # a newer list replaces the numbers
        return (
            waiting,
            after,
            first.gmail_id,
            second.gmail_id,
            (await store.email_number(1, 1)).gmail_id,
            await store.email_number(1, 2),
            await store.email_number(2, 1),
            [e.gmail_id for e in await store.unreported(2)],
        )

    assert run(scenario) == (
        ["m0", "m1", "m2", "m3"],
        ["m3"],
        "m1",
        "m0",
        "m3",
        None,
        None,
        ["x1"],
    )


def test_e5_drafts_are_numbered_per_user_and_read_back_whole():
    async def scenario(store, database):
        first = await a_draft(store)
        second = await a_draft(store, gmail_draft_id="r-2")
        theirs = await a_draft(store, user_id=2, gmail_draft_id="r-3")
        return first, second, theirs, await store.open_drafts(1)

    first, second, theirs, waiting = run(scenario)

    assert (first.number, second.number, theirs.number) == (1, 2, 1)
    assert first.to == ("rana@example.com",) and first.cc == ("sami@example.com",)
    assert first.new_recipients == ("sami@example.com",)  # E6: not on the thread before
    assert first.status == "pending" and first.expires_at == NOW + 24 * HOUR
    assert not first.expired(NOW + 23 * HOUR) and first.expired(NOW + 24 * HOUR)
    assert [draft.number for draft in waiting] == [1, 2]


def test_e5_a_draft_changes_status_only_from_the_expected_one():
    async def scenario(store, database):
        draft = await a_draft(store)
        steps = [
            await store.move_draft(draft.id, "sending", "sent", NOW),  # not sending yet
            await store.move_draft(draft.id, "pending", "sending", NOW),
            await store.move_draft(draft.id, "pending", "sending", NOW),  # already claimed
            await store.move_draft(draft.id, "sending", "sent", NOW),
            await store.move_draft(draft.id, "pending", "cancelled", NOW),  # too late
        ]
        return steps, await store.open_drafts(1)

    steps, waiting = run(scenario)

    assert steps == [False, True, False, True, False]
    assert waiting == []


def test_e5_two_sends_at_the_same_moment_claim_a_draft_once():
    async def scenario(store, database):
        draft = await a_draft(store)
        return await asyncio.gather(
            *(store.move_draft(draft.id, "pending", "sending", NOW) for _ in range(8))
        )

    assert sorted(run(scenario)) == [False] * 7 + [True]


def test_e5_a_revised_draft_keeps_its_number_and_gets_new_text_recipients_and_expiry():
    async def scenario(store, database):
        draft = await a_draft(store)
        await store.revise_draft(
            draft.id,
            to=("rana@example.com", "lawyer@firm.example"),
            cc=(),
            body="Shorter.",
            expires_at=NOW + 30 * HOUR,
        )
        await store.move_draft(draft.id, "pending", "cancelled", NOW)
        # A cancelled draft can no longer be revised.
        await store.revise_draft(draft.id, to=("x@y.example",), cc=(), body="!", expires_at=NOW)
        async with database.sessions.begin() as session:
            row = (
                await session.execute(text("SELECT body, to_addresses, status FROM email_drafts"))
            ).one()
        return row

    body, to_addresses, status = run(scenario)

    assert body == "Shorter."
    assert to_addresses == '["rana@example.com", "lawyer@firm.example"]'
    assert status == "cancelled"


def test_u2_deleting_a_user_removes_their_mail_rows_and_nobody_elses():
    async def scenario(store, database):
        for user_id in (1, 2):
            await store.save_connection(user_id, f"u{user_id}@example.com", "sealed", NOW)
            await store.add_link(f"hash-{user_id}", user_id, NOW + HOUR)
            await store.add_email(sorted_email("m1", user_id=user_id), NOW)
            await a_draft(store, user_id=user_id)
        async with database.sessions.begin() as session:
            await session.execute(text("DELETE FROM users WHERE id = 1"))
            counts = {}
            for table in ("gmail_connections", "gmail_links", "emails", "email_drafts"):
                rows = await session.execute(text(f"SELECT user_id FROM {table}"))
                counts[table] = [row[0] for row in rows]
        return counts

    assert run(scenario) == {
        "gmail_connections": [2],
        "gmail_links": [2],
        "emails": [2],
        "email_drafts": [2],
    }


def test_e2_the_database_has_no_column_that_could_hold_an_email_body():
    async def scenario(store, database):
        async with database.sessions.begin() as session:
            rows = await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'emails' ORDER BY column_name"
                )
            )
            return [row[0] for row in rows]

    assert run(scenario) == [
        "category",
        "created_at",
        "gmail_id",
        "id",
        "list_number",
        "received_at",
        "reported",
        "sender",
        "subject",
        "summary",
        "thread_id",
        "user_id",
    ]
