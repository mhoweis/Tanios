"""The private setup page (U1): model keys, Gmail, budget and vault, one user at a time.

A user gets a one-time link by sending `setup` on WhatsApp. Opening it starts a short
session in that browser. Keys are typed here and nowhere else, stored encrypted, and
never shown again. The page only ever acts for the user whose link opened it (U2).
"""

import hashlib
import hmac
import html
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tanios.catalog import Catalog
from tanios.keys import UserKeys, looks_like_a_key
from tanios.router import money, utc_now
from tanios.store import Store, User
from tanios.tables import SetupTokenRow

if TYPE_CHECKING:
    from tanios.mail import Mailroom
    from tanios.people import Team

logger = logging.getLogger(__name__)

LINK_LIFETIME = timedelta(minutes=15)
SESSION_LIFETIME = timedelta(minutes=30)
COOKIE = "tanios_setup"
MAX_CAP = Decimal("1000")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_for(session: str) -> str:
    """A value only a page served to this session can know, for its forms."""
    return hashlib.sha256(f"csrf:{session}".encode()).hexdigest()


class SetupTokens(Protocol):
    """Tests use an in-memory stand-in with this shape (tests/fakes.py)."""

    async def add(self, hashed: str, user_id: int, kind: str, expires_at: datetime) -> None: ...

    async def use(self, hashed: str, kind: str, now: datetime) -> int | None:
        """Whose token this is, if live, and mark it used. Answers once only."""
        ...

    async def holder(self, hashed: str, kind: str, now: datetime) -> int | None:
        """Whose token this is, if live. Does not use it up."""
        ...

    async def end(self, hashed: str) -> None: ...


class SqlSetupTokens:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add(self, hashed: str, user_id: int, kind: str, expires_at: datetime) -> None:
        async with self._sessions.begin() as session:
            if kind == "link":
                # One live link per user: asking again replaces the earlier one.
                await session.execute(
                    delete(SetupTokenRow).where(
                        SetupTokenRow.user_id == user_id, SetupTokenRow.kind == "link"
                    )
                )
            session.add(
                SetupTokenRow(token_hash=hashed, user_id=user_id, kind=kind, expires_at=expires_at)
            )

    def _live(self, hashed: str, kind: str, now: datetime):
        return (
            SetupTokenRow.token_hash == hashed,
            SetupTokenRow.kind == kind,
            SetupTokenRow.used_at.is_(None),
            SetupTokenRow.expires_at > now,
        )

    async def use(self, hashed: str, kind: str, now: datetime) -> int | None:
        statement = (
            update(SetupTokenRow)
            .where(*self._live(hashed, kind, now))
            .values(used_at=now)
            .returning(SetupTokenRow.user_id)
        )
        async with self._sessions.begin() as session:
            return await session.scalar(statement)

    async def holder(self, hashed: str, kind: str, now: datetime) -> int | None:
        async with self._sessions.begin() as session:
            return await session.scalar(
                select(SetupTokenRow.user_id).where(*self._live(hashed, kind, now))
            )

    async def end(self, hashed: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(delete(SetupTokenRow).where(SetupTokenRow.token_hash == hashed))


@dataclass(frozen=True)
class Outcome:
    """What a form did: a line to show on the page, and where to go next."""

    message: str
    redirect: str | None = None  # an outside address, such as Google's sign-in page
    ended: bool = False  # the session is over (the user deleted their data)


class SetupDesk:
    def __init__(
        self,
        *,
        tokens: SetupTokens,
        users: Store,
        keys: UserKeys,
        catalog: Catalog,
        public_url: str,
        mail: "Mailroom | None" = None,
        memory_on: bool = False,
        team: "Team | None" = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._tokens = tokens
        self._users = users
        self._keys = keys
        self._catalog = catalog
        self._public_url = public_url.rstrip("/")
        self._mail = mail
        self._memory_on = memory_on
        self.team = team
        self._now = now

    @property
    def secure(self) -> bool:
        return self._public_url.startswith("https://")

    # Getting in ------------------------------------------------------------------------

    async def link_message(self, user: User) -> str:
        token = secrets.token_urlsafe(32)
        await self._tokens.add(token_hash(token), user.id, "link", self._now() + LINK_LIFETIME)
        return (
            "Your private setup page. The link works once, for 15 minutes:\n"
            f"{self._public_url}/setup/open?t={token}\n\n"
            "Add your model key, connect Gmail and set your budget there. Never type a key "
            "into WhatsApp."
        )

    async def open_link(self, token: str) -> str | None:
        """Trade a one-time link for a session. Returns the session token, or None."""
        user_id = await self._tokens.use(token_hash(token), "link", self._now())
        if user_id is None:
            return None
        session = secrets.token_urlsafe(32)
        await self._tokens.add(
            token_hash(session), user_id, "session", self._now() + SESSION_LIFETIME
        )
        return session

    async def session_user(self, session: str | None) -> User | None:
        if not session:
            return None
        user_id = await self._tokens.holder(token_hash(session), "session", self._now())
        return None if user_id is None else await self._users.user_by_id(user_id)

    async def end_session(self, session: str) -> None:
        await self._tokens.end(token_hash(session))

    # The page --------------------------------------------------------------------------

    async def page(self, user: User, session: str, notice: str = "") -> str:
        csrf = csrf_for(session)
        added = set(await self._keys.keys_for(user.id))
        parts = [f"<p class=notice>{_e(notice)}</p>" if notice else ""]

        # Model keys (R2)
        rows = []
        for name in sorted(self._catalog.providers):
            state = "added" if name in added else "not added"
            remove = (
                _form(
                    "/setup/keys/remove",
                    csrf,
                    f'<input type=hidden name=provider value="{_e(name)}">',
                    "Remove",
                )
                if name in added
                else ""
            )
            rows.append(f"<tr><td>{_e(name)}</td><td>{state}</td><td>{remove}</td></tr>")
        options = "".join(f"<option>{_e(n)}</option>" for n in sorted(self._catalog.providers))
        server_keys = (
            "<p>As the owner, the keys in the server settings are yours too. A key you add "
            "here is used instead of the server's for that provider.</p>"
            if user.is_owner
            else "<p>Tanios uses only the keys you add here. Without one it cannot answer "
            "you. Most people need just an OpenRouter key: openrouter.ai, then Keys.</p>"
        )
        parts.append(
            "<h2>Model keys</h2>"
            + server_keys
            + "<table>"
            + "".join(rows)
            + "</table>"
            + _form(
                "/setup/keys",
                csrf,
                f"<label>Provider <select name=provider>{options}</select></label>"
                "<label>Key <input type=password name=key autocomplete=off required></label>",
                "Save key",
            )
            + "<p class=small>A saved key is stored encrypted and never shown again.</p>"
        )

        # Gmail (E1)
        if self._mail is None:
            gmail = "<p>Email is not switched on on this server.</p>"
        else:
            status = _e(await self._mail.status_line(user))
            if await self._mail.connected(user):
                gmail = f"<p>{status}</p>" + _form(
                    "/setup/gmail/disconnect", csrf, "", "Disconnect Gmail"
                )
            else:
                gmail = f"<p>{status}</p>" + _form(
                    "/setup/gmail/connect", csrf, "", "Connect Gmail"
                )
        parts.append("<h2>Gmail</h2>" + gmail)

        # Budget (R3, R4)
        cap = "" if user.monthly_cap_usd is None else f"{user.monthly_cap_usd.normalize():f}"
        free_state = "on" if user.allow_free_for_private else "off"
        if user.is_owner:
            budget = (
                f"<p>Monthly cap: {_e(money(user.monthly_cap_usd)) if cap else 'none'}. "
                f"Free models for private content: {free_state}.</p>"
                "<p class=small>As the owner, you set these in the server's .env file "
                "(TANIOS_MONTHLY_CAP_USD and TANIOS_ALLOW_FREE_FOR_PRIVATE).</p>"
            )
        else:
            checked = " checked" if user.allow_free_for_private else ""
            budget = _form(
                "/setup/budget",
                csrf,
                "<label>Monthly cap in US dollars "
                f'<input name=cap inputmode=decimal value="{_e(cap)}"></label>'
                "<p class=small>Leave empty for no cap. You pay your model provider directly.</p>"
                f"<label><input type=checkbox name=free value=yes{checked}> Let free models see my "
                "email, notes and files</label>",
                "Save budget",
            )
        parts.append("<h2>Budget</h2>" + budget)

        # Vault (M1)
        if not self._memory_on:
            vault = "<p>Memory is not switched on on this server.</p>"
        elif user.is_owner:
            vault = (
                "<p>Your notes are kept on the server in your own folder. To see them in "
                "Obsidian, follow part 2 of docs/MEMORY.md.</p>"
            )
        else:
            vault = (
                "<p>Your notes and saved files are kept on the server in a folder of your "
                "own, which no other user can reach. In this version, only the owner's notes "
                "can be synced to Obsidian.</p>"
            )
        parts.append("<h2>Notes and files</h2>" + vault)

        # Delete my data (U3)
        if not user.is_owner and self.team is not None:
            parts.append(
                "<h2>Delete my data</h2><p>Deletes everything this server holds about you. "
                "This cannot be undone.</p>"
                + _form(
                    "/setup/delete",
                    csrf,
                    "<label>Type DELETE to confirm <input name=confirm autocomplete=off></label>",
                    "Delete everything",
                )
            )
        return _page("Tanios setup", f"<p>Number: +{_e(user.phone)}</p>" + "".join(parts))

    # The forms -------------------------------------------------------------------------

    async def save_key(self, user: User, provider: str, key: str) -> Outcome:
        provider = provider.strip().lower()
        if provider not in self._catalog.providers:
            return Outcome("That provider is not in this server's list.")
        key = key.strip()
        if not looks_like_a_key(key):
            return Outcome("That does not look like a key. Copy it again, with nothing around it.")
        await self._keys.put(user.id, provider, key)
        logger.info("a model key was saved on the setup page")
        return Outcome(f"Your {provider} key is saved.")

    async def remove_key(self, user: User, provider: str) -> Outcome:
        await self._keys.remove(user.id, provider.strip().lower())
        return Outcome("The key is removed.")

    async def save_budget(self, user: User, cap_text: str, free: bool) -> Outcome:
        if user.is_owner:
            return Outcome("As the owner, you set your budget in the server's .env file.")
        cap_text = cap_text.strip().lstrip("$").strip()
        if not cap_text:
            cap = None
        else:
            try:
                cap = Decimal(cap_text)
            except InvalidOperation:
                return Outcome("The cap must be a number, such as 5 or 12.50.")
            if not cap.is_finite() or cap < 0 or cap > MAX_CAP:
                return Outcome(f"The cap must be between 0 and {MAX_CAP}.")
        await self._users.set_budget(user.id, cap, free)
        shown = "no cap" if cap is None else money(cap)
        return Outcome(
            f"Saved: {shown} a month, free models for private content {'on' if free else 'off'}."
        )

    async def connect_gmail(self, user: User) -> Outcome:
        if self._mail is None:
            return Outcome("Email is not switched on on this server.")
        return Outcome("", redirect=await self._mail.consent_for(user))

    async def disconnect_gmail(self, user: User) -> Outcome:
        if self._mail is None:
            return Outcome("Email is not switched on on this server.")
        return Outcome(await self._mail.disconnect(user))

    async def delete_everything(self, user: User, confirm: str) -> Outcome:
        if user.is_owner or self.team is None:
            return Outcome("This is not available for the owner.")
        if confirm.strip() != "DELETE":
            return Outcome("Nothing was deleted. Type DELETE in capitals to confirm.")
        reply = await self.team.delete_user(user)
        return Outcome(reply, ended=reply.startswith("Done."))


# HTML --------------------------------------------------------------------------------


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _form(action: str, csrf: str, fields: str, button: str) -> str:
    return (
        f'<form method=post action="{action}">'
        f'<input type=hidden name=csrf value="{csrf}">{fields}'
        f"<button>{_e(button)}</button></form>"
    )


STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:34rem;margin:2rem auto;padding:0 1rem;"
    "line-height:1.45}h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:2rem}"
    "label{display:block;margin:.6rem 0}input:not([type=checkbox]),select{display:block;"
    "width:100%;padding:.5rem;font-size:1rem;box-sizing:border-box}button{margin-top:.6rem;"
    "padding:.55rem 1rem;font-size:1rem}table{border-collapse:collapse;width:100%}"
    "td{padding:.3rem .4rem;border-bottom:1px solid #ddd}td form{margin:0}td button{margin:0}"
    ".notice{background:#eef6ee;padding:.6rem;border-radius:4px}.small{font-size:.9rem;color:#555}"
)


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        '<meta name=referrer content="no-referrer">'
        f"<title>{_e(title)}</title><style>{STYLE}</style></head>"
        f"<body><h1>{_e(title)}</h1>{body}</body></html>"
    )


def plain_page(title: str, text: str) -> str:
    return _page(title, f"<p>{_e(text)}</p>")


def check_csrf(session: str, given: str) -> bool:
    return hmac.compare_digest(csrf_for(session), given or "")
