"""Model keys that users add on their setup page (R2, U1).

A key is typed into the setup page and nowhere else. It is stored encrypted, used to
call the model provider, and never shown again, logged or sent over WhatsApp.
"""

import logging
import re
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tanios.crypto import SealError, TokenBox
from tanios.tables import UserKeyRow

logger = logging.getLogger(__name__)

# What a provider key may look like: one run of ordinary visible characters.
_KEY = re.compile(r"^[\x21-\x7e]{8,400}$")


def looks_like_a_key(text: str) -> bool:
    return bool(_KEY.match(text))


class UserKeys(Protocol):
    """Tests use an in-memory stand-in with this shape (tests/fakes.py)."""

    async def keys_for(self, user_id: int) -> dict[str, str]:
        """Provider name to key, for this user only."""
        ...

    async def put(self, user_id: int, provider: str, key: str) -> None: ...

    async def remove(self, user_id: int, provider: str) -> None: ...


class SqlUserKeys:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], box: TokenBox) -> None:
        self._sessions = sessions
        self._box = box

    async def keys_for(self, user_id: int) -> dict[str, str]:
        statement = select(UserKeyRow).where(UserKeyRow.user_id == user_id)
        async with self._sessions.begin() as session:
            rows = list(await session.scalars(statement))
        keys: dict[str, str] = {}
        for row in rows:
            try:
                keys[row.provider] = self._box.open(row.sealed_key)
            except SealError:
                # The server's encryption key changed. The user has to add the key again.
                logger.warning("a stored model key could not be opened; it is ignored")
        return keys

    async def put(self, user_id: int, provider: str, key: str) -> None:
        values = {"sealed_key": self._box.seal(key), "created_at": datetime.now(UTC)}
        statement = (
            insert(UserKeyRow)
            .values(user_id=user_id, provider=provider.lower(), **values)
            .on_conflict_do_update(
                index_elements=[UserKeyRow.user_id, UserKeyRow.provider], set_=values
            )
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)

    async def remove(self, user_id: int, provider: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                delete(UserKeyRow).where(
                    UserKeyRow.user_id == user_id, UserKeyRow.provider == provider.lower()
                )
            )
