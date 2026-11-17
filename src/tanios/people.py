"""Testers and their data (U1, U3).

The owner invites a tester by phone number. Each user opens their own setup page with
`setup`, and removes everything the server holds about them with `delete my data`.
Commands are recognised from the whole message, with no model.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from tanios.memory import Memory
from tanios.router import money, utc_now
from tanios.store import Store, User, normalize_phone
from tanios.vault import VaultError

if TYPE_CHECKING:
    from tanios.mail import Mailroom
    from tanios.setup import SetupDesk

logger = logging.getLogger(__name__)

CONFIRM_WINDOW = timedelta(minutes=10)
CONFIRM_PHRASE = "yes delete everything"
MAX_TESTERS = 10

_LEAD = re.compile(
    r"^(?:(?:hey|hi|hello|ok|okay)\b[\s,]*)?(?:tanios\b[\s,.!:]*)?"
    r"(?:(?:please|can you|could you|will you)\b[\s,]*)*",
    re.IGNORECASE,
)
_PHONE = r"(?P<phone>\+?[\d][\d\s().-]{6,22}\d)"
_INVITE = re.compile(rf"^(?:invite|add)\s+(?:tester\s+)?{_PHONE}$", re.IGNORECASE)
_REMOVE = re.compile(rf"^remove\s+(?:tester\s+)?{_PHONE}$", re.IGNORECASE)
_TESTERS = re.compile(r"^(?:testers|list\s+testers|my\s+testers)$", re.IGNORECASE)
_SETUP = re.compile(r"^(?:setup|set\s+up|settings|setup\s+page)$", re.IGNORECASE)
_DELETE = re.compile(r"^delete\s+(?:all\s+)?(?:of\s+)?my\s+data$", re.IGNORECASE)
_CONFIRM = re.compile(r"^yes,?\s+delete\s+everything$", re.IGNORECASE)


@dataclass(frozen=True)
class PeopleRequest:
    kind: str  # invite, remove, testers, setup, delete or confirm_delete
    phone: str = ""


def parse_people(said: str) -> PeopleRequest | None:
    whole = _LEAD.sub("", said.strip(), count=1).strip().rstrip(".!?").strip()
    if match := _INVITE.match(whole):
        return PeopleRequest("invite", normalize_phone(match.group("phone")))
    if match := _REMOVE.match(whole):
        return PeopleRequest("remove", normalize_phone(match.group("phone")))
    if _TESTERS.match(whole):
        return PeopleRequest("testers")
    if _SETUP.match(whole):
        return PeopleRequest("setup")
    if _DELETE.match(whole):
        return PeopleRequest("delete")
    if _CONFIRM.match(whole):
        return PeopleRequest("confirm_delete")
    return None


OWNER_ONLY = "Only the owner can invite or remove testers."
NOT_A_PHONE = "That does not look like a phone number. Use the full number with country code."
SETUP_OFF = (
    "The setup page is not switched on on this server yet. The owner needs to set "
    "TANIOS_DOMAIN and TANIOS_ENCRYPTION_KEY."
)
OWNER_CANNOT_DELETE = (
    "As the owner you run this server, so delete my data is not available to you. "
    "docs/DEPLOY.md explains how to wipe the server. To remove a tester, send: remove "
    "followed by their number."
)
NOTHING_TO_CONFIRM = "There is nothing waiting to be confirmed."
DELETE_WARNING = (
    "This deletes everything this server holds about you: your notes and saved files, "
    "your model keys, your Gmail sign-in, your drafts and mail summaries, and your usage "
    "record. You will no longer be able to use Tanios unless the owner invites you again. "
    "Your Gmail itself is not touched.\n\n"
    f"To go ahead, send exactly: {CONFIRM_PHRASE}\n"
    "This works for the next 10 minutes."
)


class Team:
    def __init__(
        self,
        *,
        store: Store,
        memories: Callable[[int], Memory] | None,
        mail: "Mailroom | None",
        setup: "SetupDesk | None",
        tester_cap_usd: Decimal | None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._memories = memories
        self._mail = mail
        self._setup = setup
        self._tester_cap = tester_cap_usd
        self._now = now
        self._confirming: dict[int, datetime] = {}
        # Called with a user ID when that user is deleted, so in-memory state goes too.
        self.forget_hooks: list[Callable[[int], None]] = []

    async def handle(self, user: User, request: PeopleRequest) -> str:
        if request.kind == "setup":
            if self._setup is None:
                return SETUP_OFF
            return await self._setup.link_message(user)
        if request.kind == "delete":
            if user.is_owner:
                return OWNER_CANNOT_DELETE
            self._confirming[user.id] = self._now()
            return DELETE_WARNING
        if request.kind == "confirm_delete":
            asked = self._confirming.pop(user.id, None)
            if asked is None or self._now() - asked > CONFIRM_WINDOW:
                return NOTHING_TO_CONFIRM
            return await self.delete_user(user)
        if not user.is_owner:
            return OWNER_ONLY
        if request.kind == "testers":
            return await self._list()
        if not 8 <= len(request.phone) <= 15:
            return NOT_A_PHONE
        if request.kind == "invite":
            return await self._invite(user, request.phone)
        return await self._remove(request.phone)

    async def _invite(self, owner: User, phone: str) -> str:
        if self._setup is None:
            # Without the setup page a tester could not add keys of their own.
            return SETUP_OFF
        if phone == owner.phone:
            return "That is your own number."
        if len(await self._store.testers()) >= MAX_TESTERS:
            return f"There are already {MAX_TESTERS} testers. Remove one first."
        tester = await self._store.add_tester(phone, self._tester_cap)
        if tester is None:
            return f"+{phone} is already registered."
        cap = "no monthly cap" if self._tester_cap is None else f"a {money(self._tester_cap)} cap"
        return (
            f"+{phone} is invited, with {cap} to start.\n"
            "WhatsApp does not let me write to them first, so ask them to send any message "
            "to this number, then send setup. On their own setup page they add their model "
            "key, connect Gmail and set their budget. You never see their keys."
        )

    async def _list(self) -> str:
        testers = await self._store.testers()
        if not testers:
            return "No testers yet. Invite one with: invite followed by their number."
        lines = ["Testers:"]
        for tester in testers:
            seen = "has written" if tester.last_message_at else "has not written yet"
            lines.append(f"- +{tester.phone} ({seen})")
        lines.append("To remove one and delete their data: remove followed by their number.")
        return "\n".join(lines)

    async def _remove(self, phone: str) -> str:
        tester = await self._store.find_user(phone, None)
        if tester is None:
            return f"+{phone} is not registered."
        if tester.is_owner:
            return "You cannot remove the owner."
        result = await self.delete_user(tester)
        return result.replace("Everything this server held about you", f"Everything about +{phone}")

    async def delete_user(self, user: User) -> str:
        """U3: remove the user's tokens, keys, vault and records, in that order."""
        notes: list[str] = []
        if self._mail is not None and not await self._mail.remove_user(user):
            notes.append(
                "I could not reach Google to cancel Tanios's access, so also remove Tanios at "
                "myaccount.google.com/permissions."
            )
        if self._memories is not None:
            try:
                await asyncio.to_thread(self._memories(user.id).destroy)
            except VaultError as error:
                logger.error("a vault could not be deleted: %s", error)
                return (
                    "Sorry, I could not delete the notes and files on the server. Nothing "
                    "else was removed yet. Please try again, and tell the owner if it fails "
                    "twice."
                )
        # Keys, setup links, mail records, drafts and the usage ledger go with the user row.
        await self._store.delete_user(user.id)
        for hook in self.forget_hooks:
            hook(user.id)
        logger.info("a user and all their data were deleted")
        reply = (
            "Done. Everything this server held about you is deleted: notes, files, model "
            "keys, Gmail sign-in, drafts and usage. Copies in Obsidian Sync on your own "
            "devices are yours and were not touched. Goodbye."
        )
        return " ".join([reply, *notes])
