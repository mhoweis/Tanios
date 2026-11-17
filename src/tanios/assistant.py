"""What Tanios does with one incoming WhatsApp message.

The order of the checks is the security model: nothing from an unregistered sender is
ever read as an instruction or shown to a model (W3).
"""

import asyncio
import logging

from tanios.llm import ModelError
from tanios.router import CHAT, BudgetExceeded, Router, money
from tanios.store import Store, UsageSummary, User
from tanios.whatsapp.client import WhatsAppClient, WhatsAppSendError
from tanios.whatsapp.parsing import InboundMessage

logger = logging.getLogger(__name__)

STRANGER_REPLY = "This is a private assistant. It only answers its registered users."
UNSUPPORTED_REPLY = "I can only read text messages for now."
WORKING_REPLY = "Working on it..."
APOLOGY_REPLY = "Sorry, I could not get an answer just now. Please try again in a minute."
PAUSED_REPLY = "Tanios is paused. Send resume to start again."
HELP_REPLY = (
    "Tanios commands:\n"
    "help - show this list\n"
    "status - see whether Tanios is active or paused\n"
    "pause - stop answering until you send resume\n"
    "resume - start answering again\n"
    "forget ... - remove something from memory\n"
    "\n"
    "Anything else you send is answered as a question."
)
STATUS_ACTIVE = "Tanios is active."
STATUS_PAUSED = "Tanios is paused. Send resume to start again."
PAUSE_REPLY = "Paused. I will not answer questions until you send resume."
RESUME_REPLY = "Resumed. I am answering again."
FORGET_REPLY = "There is nothing to forget yet: memory is not switched on."

# Message kinds a person sends on purpose and would expect an answer to. Reactions,
# read receipts and system notices are ignored silently.
UNSUPPORTED_KINDS = {"audio", "image", "video", "document", "sticker", "location", "contacts"}

SYSTEM_PROMPT = (
    "You are Tanios, a personal assistant the user talks to on WhatsApp. "
    "Answer in plain text suited to a phone: short and direct, with no markdown headings "
    "or tables. Today you can only chat and answer questions. Handling email, remembering "
    "things between messages, saving files and understanding voice notes are still being "
    "built. If the user asks for one of those, say plainly that it is not available yet. "
    "Never claim to have sent, saved, scheduled or remembered anything."
)


def format_status(user: User, month: str, usage: UsageSummary, key_names: list[str]) -> str:
    """The reply to `status`: state, this month's spend against the cap, and what ran (W4)."""
    lines = [STATUS_PAUSED if user.paused else STATUS_ACTIVE]

    cap = user.monthly_cap_usd
    if cap is None:
        lines.append(f"{month} spend: {money(usage.total_usd)}. No monthly cap is set.")
    else:
        percent = int(usage.total_usd * 100 / cap) if cap > 0 else 100
        lines.append(f"{month} spend: {money(usage.total_usd)} of {money(cap)} ({percent}%).")

    if not usage.by_task:
        lines.append("No model calls yet this month.")
    for task, cost, calls in usage.by_task:
        lines.append(f"- {task}: {money(cost)} in {calls} {'call' if calls == 1 else 'calls'}")
    if usage.by_model:
        used = ", ".join(f"{model} ({calls})" for model, calls in usage.by_model)
        lines.append(f"Models used: {used}")

    lines.append(f"Model keys: {', '.join(key_names) if key_names else 'none'}")
    lines.append(
        f"Free models for private content: {'on' if user.allow_free_for_private else 'off'}"
    )
    return "\n".join(lines)


def parse_command(text: str) -> str | None:
    """The command a message is, or None when it is an ordinary question (W4)."""
    words = text.strip().lower().split()
    if not words:
        return None
    if len(words) == 1 and words[0] in {"help", "status", "pause", "resume", "forget"}:
        return words[0]
    if words[0] == "forget":
        return "forget"
    return None


class Assistant:
    def __init__(
        self,
        store: Store,
        whatsapp: WhatsAppClient,
        router: Router,
        *,
        ack_after_seconds: float = 8.0,
    ) -> None:
        self._store = store
        self._whatsapp = whatsapp
        self._router = router
        self._ack_after_seconds = ack_after_seconds

    async def handle(self, message: InboundMessage) -> None:
        """Runs after the webhook has been acknowledged, so it must never raise."""
        try:
            await self._handle(message)
        except Exception as error:
            # Type only: database and HTTP error messages can carry numbers or text.
            logger.error("handling a message failed: %s", type(error).__name__)

    async def _handle(self, message: InboundMessage) -> None:
        if not await self._store.claim_message(message.id):
            return  # Meta delivered it twice

        user = await self._store.find_user(message.phone, message.wa_user_id)
        if user is None:
            if await self._store.first_contact_from_stranger(message.sender_key):
                await self._reply(message, STRANGER_REPLY)
            return

        if message.kind != "text" or not message.text or not message.text.strip():
            if message.kind in UNSUPPORTED_KINDS:
                await self._reply(message, UNSUPPORTED_REPLY)
            return

        command = parse_command(message.text)
        if command is not None:
            await self._run_command(command, user, message)
        elif user.paused:
            await self._reply(message, PAUSED_REPLY)
        else:
            await self._answer(user, message)

    async def _run_command(self, command: str, user: User, message: InboundMessage) -> None:
        if command == "help":
            await self._reply(message, HELP_REPLY)
        elif command == "status":
            month, usage = await self._router.month_usage(user)
            status = format_status(user, month, usage, self._router.key_names())
            await self._reply(message, status)
        elif command == "pause":
            await self._store.set_paused(user.id, True)
            await self._reply(message, PAUSE_REPLY)
        elif command == "resume":
            await self._store.set_paused(user.id, False)
            await self._reply(message, RESUME_REPLY)
        elif command == "forget":
            await self._reply(message, FORGET_REPLY)

    async def _answer(self, user: User, message: InboundMessage) -> None:
        answer = asyncio.create_task(
            self._router.complete(
                user=user, task=CHAT, system=SYSTEM_PROMPT, prompt=message.text.strip()
            )
        )
        finished, _ = await asyncio.wait({answer}, timeout=self._ack_after_seconds)
        if not finished:
            await self._reply(message, WORKING_REPLY)  # W1: within 10 seconds
        try:
            reply = await answer
        except BudgetExceeded as stop:
            await self._reply(message, str(stop))  # R4: say what still works
            return
        except ModelError as error:
            logger.warning("no answer: %s", error)
            await self._reply(message, APOLOGY_REPLY)
            return
        await self._reply(message, reply.text)
        if reply.notice:
            await self._reply(message, reply.notice)

    async def _reply(self, message: InboundMessage, body: str) -> None:
        try:
            await self._whatsapp.send_text(
                phone=message.phone, wa_user_id=message.wa_user_id, body=body
            )
        except WhatsAppSendError as error:
            logger.warning("sending a WhatsApp message failed: %s", error)
