"""What Tanios keeps in the database about a user's mail.

Kept: the encrypted Google token, one row per sorted email (ID, label, sender, subject
and a one-line summary) and the drafts waiting for a `send`. Never kept: email bodies.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tanios.tables import EmailDraftRow, EmailRow, GmailConnectionRow, GmailLinkRow


@dataclass(frozen=True)
class Connection:
    user_id: int
    address: str
    sealed_token: str
    connected_at: datetime
    last_checked_at: datetime | None = None
    digest_times: tuple[str, ...] = ()
    last_digest_key: str | None = None


@dataclass(frozen=True)
class SortedEmail:
    user_id: int
    gmail_id: str
    thread_id: str
    category: str  # "needs_reply", "fyi", "newsletter" or "suspicious"
    sender: str
    subject: str
    summary: str
    received_at: datetime
    list_number: int | None = None


@dataclass(frozen=True)
class Draft:
    id: int
    user_id: int
    number: int
    gmail_draft_id: str
    gmail_id: str
    thread_id: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    thread_addresses: tuple[str, ...]
    subject: str
    body: str
    status: str  # "pending", "sending", "sent" or "cancelled"
    expires_at: datetime

    def expired(self, now: datetime) -> bool:
        return self.status == "pending" and now >= self.expires_at

    @property
    def new_recipients(self) -> tuple[str, ...]:
        """E6: recipients who were not on the thread before this reply."""
        known = set(self.thread_addresses)
        return tuple(address for address in (*self.to, *self.cc) if address not in known)


class MailStore(Protocol):
    """Tests use an in-memory stand-in with this shape (tests/fakes.py)."""

    # Sign-in links
    async def add_link(self, token_hash: str, user_id: int, expires_at: datetime) -> None: ...

    async def link_user(self, token_hash: str, now: datetime) -> int | None:
        """Whose link this is, if it is unused and not expired. Does not use it up."""
        ...

    async def use_link(self, token_hash: str, now: datetime) -> int | None:
        """As link_user, and the link is used up. It answers once only."""
        ...

    # Connections
    async def save_connection(
        self, user_id: int, address: str, sealed_token: str, now: datetime
    ) -> None: ...

    async def connection(self, user_id: int) -> Connection | None: ...

    async def connections(self) -> list[Connection]: ...

    async def drop_connection(self, user_id: int) -> None: ...

    async def mark_checked(self, user_id: int, now: datetime) -> None: ...

    async def set_digest_times(self, user_id: int, times: tuple[str, ...]) -> None: ...

    async def mark_digest(self, user_id: int, key: str) -> None: ...

    # Sorted emails
    async def known_ids(self, user_id: int, gmail_ids: list[str]) -> set[str]: ...

    async def add_email(self, email: SortedEmail, now: datetime) -> None: ...

    async def unreported(self, user_id: int) -> list[SortedEmail]:
        """Sorted emails not yet shown in a list, oldest first."""
        ...

    async def show_list(self, user_id: int, gmail_ids: list[str], also_seen: list[str]) -> None:
        """Number gmail_ids 1, 2, 3 as the user's current list and mark all as shown."""
        ...

    async def email_number(self, user_id: int, number: int) -> SortedEmail | None: ...

    # Drafts
    async def add_draft(
        self,
        *,
        user_id: int,
        gmail_draft_id: str,
        gmail_id: str,
        thread_id: str,
        to: tuple[str, ...],
        cc: tuple[str, ...],
        thread_addresses: tuple[str, ...],
        subject: str,
        body: str,
        now: datetime,
        expires_at: datetime,
    ) -> Draft: ...

    async def open_drafts(self, user_id: int) -> list[Draft]:
        """Drafts still marked pending, including expired ones, oldest first."""
        ...

    async def revise_draft(
        self,
        draft_id: int,
        *,
        to: tuple[str, ...],
        cc: tuple[str, ...],
        body: str,
        expires_at: datetime,
    ) -> None: ...

    async def move_draft(self, draft_id: int, before: str, after: str, now: datetime) -> bool:
        """Change a draft's status, only if it is still `before`. True when it changed.

        Sending goes pending, sending, sent. Only one caller can win the first step, so
        two `send` messages at the same moment cannot send a draft twice.
        """
        ...


def _list(value: str) -> tuple[str, ...]:
    try:
        items = json.loads(value)
    except ValueError:
        return ()
    return tuple(item for item in items if isinstance(item, str)) if isinstance(items, list) else ()


def _connection(row: GmailConnectionRow) -> Connection:
    return Connection(
        user_id=row.user_id,
        address=row.address,
        sealed_token=row.sealed_token,
        connected_at=row.connected_at,
        last_checked_at=row.last_checked_at,
        digest_times=tuple(time for time in row.digest_times.split(",") if time),
        last_digest_key=row.last_digest_key,
    )


def _email(row: EmailRow) -> SortedEmail:
    return SortedEmail(
        user_id=row.user_id,
        gmail_id=row.gmail_id,
        thread_id=row.thread_id,
        category=row.category,
        sender=row.sender,
        subject=row.subject,
        summary=row.summary,
        received_at=row.received_at,
        list_number=row.list_number,
    )


def _draft(row: EmailDraftRow) -> Draft:
    return Draft(
        id=row.id,
        user_id=row.user_id,
        number=row.number,
        gmail_draft_id=row.gmail_draft_id,
        gmail_id=row.gmail_id,
        thread_id=row.thread_id,
        to=_list(row.to_addresses),
        cc=_list(row.cc_addresses),
        thread_addresses=_list(row.thread_addresses),
        subject=row.subject,
        body=row.body,
        status=row.status,
        expires_at=row.expires_at,
    )


class SqlMailStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # Sign-in links ---------------------------------------------------------------------

    async def add_link(self, token_hash: str, user_id: int, expires_at: datetime) -> None:
        async with self._sessions.begin() as session:
            # One live link per user: asking again replaces the earlier one.
            await session.execute(delete(GmailLinkRow).where(GmailLinkRow.user_id == user_id))
            session.add(GmailLinkRow(token_hash=token_hash, user_id=user_id, expires_at=expires_at))

    async def link_user(self, token_hash: str, now: datetime) -> int | None:
        statement = select(GmailLinkRow.user_id).where(
            GmailLinkRow.token_hash == token_hash,
            GmailLinkRow.used_at.is_(None),
            GmailLinkRow.expires_at > now,
        )
        async with self._sessions.begin() as session:
            return await session.scalar(statement)

    async def use_link(self, token_hash: str, now: datetime) -> int | None:
        statement = (
            update(GmailLinkRow)
            .where(
                GmailLinkRow.token_hash == token_hash,
                GmailLinkRow.used_at.is_(None),
                GmailLinkRow.expires_at > now,
            )
            .values(used_at=now)
            .returning(GmailLinkRow.user_id)
        )
        async with self._sessions.begin() as session:
            return await session.scalar(statement)

    # Connections -----------------------------------------------------------------------

    async def save_connection(
        self, user_id: int, address: str, sealed_token: str, now: datetime
    ) -> None:
        values = {"address": address, "sealed_token": sealed_token, "connected_at": now}
        statement = (
            insert(GmailConnectionRow)
            .values(user_id=user_id, **values)
            .on_conflict_do_update(
                index_elements=[GmailConnectionRow.user_id],
                set_={**values, "last_checked_at": None},
            )
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)

    async def connection(self, user_id: int) -> Connection | None:
        async with self._sessions.begin() as session:
            row = await session.get(GmailConnectionRow, user_id)
            return None if row is None else _connection(row)

    async def connections(self) -> list[Connection]:
        statement = select(GmailConnectionRow).order_by(GmailConnectionRow.user_id)
        async with self._sessions.begin() as session:
            return [_connection(row) for row in await session.scalars(statement)]

    async def drop_connection(self, user_id: int) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                delete(GmailConnectionRow).where(GmailConnectionRow.user_id == user_id)
            )

    async def _update_connection(self, user_id: int, **values: object) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(GmailConnectionRow)
                .where(GmailConnectionRow.user_id == user_id)
                .values(**values)
            )

    async def mark_checked(self, user_id: int, now: datetime) -> None:
        await self._update_connection(user_id, last_checked_at=now)

    async def set_digest_times(self, user_id: int, times: tuple[str, ...]) -> None:
        await self._update_connection(user_id, digest_times=",".join(times))

    async def mark_digest(self, user_id: int, key: str) -> None:
        await self._update_connection(user_id, last_digest_key=key)

    # Sorted emails ---------------------------------------------------------------------

    async def known_ids(self, user_id: int, gmail_ids: list[str]) -> set[str]:
        if not gmail_ids:
            return set()
        statement = select(EmailRow.gmail_id).where(
            EmailRow.user_id == user_id, EmailRow.gmail_id.in_(gmail_ids)
        )
        async with self._sessions.begin() as session:
            return set(await session.scalars(statement))

    async def add_email(self, email: SortedEmail, now: datetime) -> None:
        statement = (
            insert(EmailRow)
            .values(
                user_id=email.user_id,
                gmail_id=email.gmail_id,
                thread_id=email.thread_id,
                category=email.category,
                sender=email.sender[:200],
                subject=email.subject[:200],
                summary=email.summary[:300],
                received_at=email.received_at,
                created_at=now,
            )
            .on_conflict_do_nothing()
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)

    async def unreported(self, user_id: int) -> list[SortedEmail]:
        statement = (
            select(EmailRow)
            .where(EmailRow.user_id == user_id, EmailRow.reported.is_(False))
            .order_by(EmailRow.received_at, EmailRow.id)
        )
        async with self._sessions.begin() as session:
            return [_email(row) for row in await session.scalars(statement)]

    async def show_list(self, user_id: int, gmail_ids: list[str], also_seen: list[str]) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(EmailRow).where(EmailRow.user_id == user_id).values(list_number=None)
            )
            for number, gmail_id in enumerate(gmail_ids, start=1):
                await session.execute(
                    update(EmailRow)
                    .where(EmailRow.user_id == user_id, EmailRow.gmail_id == gmail_id)
                    .values(list_number=number, reported=True)
                )
            if also_seen:
                await session.execute(
                    update(EmailRow)
                    .where(EmailRow.user_id == user_id, EmailRow.gmail_id.in_(also_seen))
                    .values(reported=True)
                )

    async def email_number(self, user_id: int, number: int) -> SortedEmail | None:
        statement = select(EmailRow).where(
            EmailRow.user_id == user_id, EmailRow.list_number == number
        )
        async with self._sessions.begin() as session:
            row = await session.scalar(statement)
            return None if row is None else _email(row)

    # Drafts ----------------------------------------------------------------------------

    async def add_draft(
        self,
        *,
        user_id: int,
        gmail_draft_id: str,
        gmail_id: str,
        thread_id: str,
        to: tuple[str, ...],
        cc: tuple[str, ...],
        thread_addresses: tuple[str, ...],
        subject: str,
        body: str,
        now: datetime,
        expires_at: datetime,
    ) -> Draft:
        async with self._sessions.begin() as session:
            highest = await session.scalar(
                select(func.coalesce(func.max(EmailDraftRow.number), 0)).where(
                    EmailDraftRow.user_id == user_id
                )
            )
            row = EmailDraftRow(
                user_id=user_id,
                number=highest + 1,
                gmail_draft_id=gmail_draft_id,
                gmail_id=gmail_id,
                thread_id=thread_id,
                to_addresses=json.dumps(list(to)),
                cc_addresses=json.dumps(list(cc)),
                thread_addresses=json.dumps(list(thread_addresses)),
                subject=subject[:300],
                body=body,
                status="pending",
                created_at=now,
                expires_at=expires_at,
            )
            session.add(row)
            await session.flush()
            return _draft(row)

    async def open_drafts(self, user_id: int) -> list[Draft]:
        statement = (
            select(EmailDraftRow)
            .where(EmailDraftRow.user_id == user_id, EmailDraftRow.status == "pending")
            .order_by(EmailDraftRow.number)
        )
        async with self._sessions.begin() as session:
            return [_draft(row) for row in await session.scalars(statement)]

    async def revise_draft(
        self,
        draft_id: int,
        *,
        to: tuple[str, ...],
        cc: tuple[str, ...],
        body: str,
        expires_at: datetime,
    ) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(EmailDraftRow)
                .where(EmailDraftRow.id == draft_id, EmailDraftRow.status == "pending")
                .values(
                    to_addresses=json.dumps(list(to)),
                    cc_addresses=json.dumps(list(cc)),
                    body=body,
                    expires_at=expires_at,
                )
            )

    async def move_draft(self, draft_id: int, before: str, after: str, now: datetime) -> bool:
        statement = (
            update(EmailDraftRow)
            .where(EmailDraftRow.id == draft_id, EmailDraftRow.status == before)
            .values(status=after, finished_at=None if after == "pending" else now)
            .returning(EmailDraftRow.id)
        )
        async with self._sessions.begin() as session:
            return (await session.scalar(statement)) is not None
