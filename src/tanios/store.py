"""Everything Tanios remembers in the database, behind one small interface."""

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tanios.tables import ProcessedMessageRow, StrangerRow, UserRow


@dataclass(frozen=True)
class User:
    id: int
    phone: str
    paused: bool


def normalize_phone(raw: str) -> str:
    """Keep the digits only: '+962 7 9000 0000' and '962790000000' are the same number."""
    return "".join(character for character in raw if character.isdigit())


class Store(Protocol):
    """Tests use an in-memory stand-in with this shape (tests/fakes.py)."""

    async def ensure_owner(self, phone: str) -> None: ...

    async def claim_message(self, wa_message_id: str) -> bool:
        """True the first time a message ID is seen, False for a repeat."""
        ...

    async def find_user(self, phone: str | None, wa_user_id: str | None) -> User | None: ...

    async def first_contact_from_stranger(self, sender_key: str) -> bool:
        """True the first time an unregistered sender writes, False ever after."""
        ...

    async def set_paused(self, user_id: int, paused: bool) -> None: ...


class SqlStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def ensure_owner(self, phone: str) -> None:
        statement = (
            insert(UserRow)
            .values(phone=normalize_phone(phone), is_owner=True)
            .on_conflict_do_update(index_elements=[UserRow.phone], set_={"is_owner": True})
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
            return User(id=row.id, phone=row.phone, paused=row.paused)

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
        async with self._sessions.begin() as session:
            await session.execute(
                update(UserRow).where(UserRow.id == user_id).values(paused=paused)
            )
