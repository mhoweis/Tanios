"""Everything Tanios remembers in the database, behind one small interface."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tanios.tables import ProcessedMessageRow, StrangerRow, UsageRow, UserRow


@dataclass(frozen=True)
class User:
    id: int
    phone: str
    paused: bool
    monthly_cap_usd: Decimal | None = None
    allow_free_for_private: bool = False
    cap_warned_month: str | None = None
    cap_reached_month: str | None = None


@dataclass(frozen=True)
class UsageEntry:
    """One model call, for the ledger."""

    user_id: int
    at: datetime
    task: str
    tier: str
    provider: str
    model: str
    cost_usd: Decimal
    prompt_tokens: int
    completion_tokens: int
    is_free: bool
    private: bool
    outcome: str  # "ok", "failed_check" or "error"


@dataclass(frozen=True)
class UsageSummary:
    total_usd: Decimal
    by_task: tuple[tuple[str, Decimal, int], ...]  # task, cost, calls; costliest first
    by_model: tuple[tuple[str, int], ...]  # model, calls; most used first


def normalize_phone(raw: str) -> str:
    """Keep the digits only: '+962 7 9000 0000' and '962790000000' are the same number."""
    return "".join(character for character in raw if character.isdigit())


class Store(Protocol):
    """Tests use an in-memory stand-in with this shape (tests/fakes.py)."""

    async def ensure_owner(
        self, phone: str, *, monthly_cap_usd: Decimal | None, allow_free_for_private: bool
    ) -> None: ...

    async def claim_message(self, wa_message_id: str) -> bool:
        """True the first time a message ID is seen, False for a repeat."""
        ...

    async def find_user(self, phone: str | None, wa_user_id: str | None) -> User | None: ...

    async def first_contact_from_stranger(self, sender_key: str) -> bool:
        """True the first time an unregistered sender writes, False ever after."""
        ...

    async def set_paused(self, user_id: int, paused: bool) -> None: ...

    async def record_usage(self, entry: UsageEntry) -> None: ...

    async def spend_since(self, user_id: int, since: datetime) -> Decimal: ...

    async def usage_summary(self, user_id: int, since: datetime) -> UsageSummary: ...

    async def mark_cap_warned(self, user_id: int, month: str) -> None: ...

    async def mark_cap_reached(self, user_id: int, month: str) -> None: ...


class SqlStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def ensure_owner(
        self, phone: str, *, monthly_cap_usd: Decimal | None, allow_free_for_private: bool
    ) -> None:
        # Until the setup page exists (milestone 7), the owner's budget comes from the
        # server settings, so it is refreshed on every start.
        budget = {
            "monthly_cap_usd": monthly_cap_usd,
            "allow_free_for_private": allow_free_for_private,
        }
        statement = (
            insert(UserRow)
            .values(phone=normalize_phone(phone), is_owner=True, **budget)
            .on_conflict_do_update(
                index_elements=[UserRow.phone], set_={"is_owner": True, **budget}
            )
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)

    async def claim_message(self, wa_message_id: str) -> bool:
        statement = (
            insert(ProcessedMessageRow)
            .values(wa_message_id=wa_message_id)
            .on_conflict_do_nothing()
            .returning(ProcessedMessageRow.wa_message_id)
        )
        async with self._sessions.begin() as session:
            claimed = (await session.execute(statement)).first()
        return claimed is not None

    async def find_user(self, phone: str | None, wa_user_id: str | None) -> User | None:
        async with self._sessions.begin() as session:
            row = None
            if phone:
                row = await session.scalar(
                    select(UserRow).where(UserRow.phone == normalize_phone(phone))
                )
                # A known phone number vouches for the WhatsApp user ID that came with it.
                if row is not None and wa_user_id and row.wa_user_id != wa_user_id:
                    row.wa_user_id = wa_user_id
            elif wa_user_id:
                row = await session.scalar(select(UserRow).where(UserRow.wa_user_id == wa_user_id))
            if row is None:
                return None
            return User(
                id=row.id,
                phone=row.phone,
                paused=row.paused,
                monthly_cap_usd=row.monthly_cap_usd,
                allow_free_for_private=row.allow_free_for_private,
                cap_warned_month=row.cap_warned_month,
                cap_reached_month=row.cap_reached_month,
            )

    async def first_contact_from_stranger(self, sender_key: str) -> bool:
        statement = (
            insert(StrangerRow)
            .values(sender_key=sender_key)
            .on_conflict_do_nothing()
            .returning(StrangerRow.sender_key)
        )
        async with self._sessions.begin() as session:
            first = (await session.execute(statement)).first()
        return first is not None

    async def set_paused(self, user_id: int, paused: bool) -> None:
        await self._update_user(user_id, paused=paused)

    async def record_usage(self, entry: UsageEntry) -> None:
        async with self._sessions.begin() as session:
            session.add(
                UsageRow(
                    user_id=entry.user_id,
                    created_at=entry.at,
                    task=entry.task,
                    tier=entry.tier,
                    provider=entry.provider,
                    model=entry.model,
                    cost_usd=entry.cost_usd,
                    prompt_tokens=entry.prompt_tokens,
                    completion_tokens=entry.completion_tokens,
                    is_free=entry.is_free,
                    private=entry.private,
                    outcome=entry.outcome,
                )
            )

    async def spend_since(self, user_id: int, since: datetime) -> Decimal:
        statement = select(func.coalesce(func.sum(UsageRow.cost_usd), 0)).where(
            UsageRow.user_id == user_id, UsageRow.created_at >= since
        )
        async with self._sessions.begin() as session:
            return Decimal(await session.scalar(statement))

    async def usage_summary(self, user_id: int, since: datetime) -> UsageSummary:
        in_period = (UsageRow.user_id == user_id, UsageRow.created_at >= since)
        cost = func.sum(UsageRow.cost_usd)
        calls = func.count()
        by_task = (
            select(UsageRow.task, cost, calls)
            .where(*in_period)
            .group_by(UsageRow.task)
            .order_by(cost.desc(), UsageRow.task)
        )
        by_model = (
            select(UsageRow.model, calls)
            .where(*in_period)
            .group_by(UsageRow.model)
            .order_by(calls.desc(), UsageRow.model)
        )
        async with self._sessions.begin() as session:
            tasks = tuple(
                (task, Decimal(total), count)
                for task, total, count in (await session.execute(by_task)).all()
            )
            models = tuple((model, count) for model, count in (await session.execute(by_model)))
        return UsageSummary(
            total_usd=sum((total for _, total, _ in tasks), Decimal(0)),
            by_task=tasks,
            by_model=models,
        )

    async def mark_cap_warned(self, user_id: int, month: str) -> None:
        await self._update_user(user_id, cap_warned_month=month)

    async def mark_cap_reached(self, user_id: int, month: str) -> None:
        await self._update_user(user_id, cap_reached_month=month)

    async def _update_user(self, user_id: int, **values: object) -> None:
        async with self._sessions.begin() as session:
            await session.execute(update(UserRow).where(UserRow.id == user_id).values(**values))
