"""Stand-ins for the outside world, shaped like the real interfaces."""

import asyncio
from dataclasses import dataclass, field

from tanios.llm import ModelError, ModelReply
from tanios.store import User, normalize_phone
from tanios.whatsapp.parsing import InboundMessage


class InMemoryStore:
    def __init__(self) -> None:
        self.users: dict[int, dict] = {}
        self.seen_messages: set[str] = set()
        self.strangers: set[str] = set()

    def add_user(self, phone: str, *, wa_user_id: str | None = None, paused: bool = False) -> int:
        user_id = len(self.users) + 1
        self.users[user_id] = {
            "phone": normalize_phone(phone),
            "wa_user_id": wa_user_id,
            "paused": paused,
        }
        return user_id

    async def ensure_owner(self, phone: str) -> None:
        if not any(user["phone"] == normalize_phone(phone) for user in self.users.values()):
            self.add_user(phone)

    async def claim_message(self, wa_message_id: str) -> bool:
        if wa_message_id in self.seen_messages:
            return False
        self.seen_messages.add(wa_message_id)
        return True

    async def find_user(self, phone: str | None, wa_user_id: str | None) -> User | None:
        for user_id, user in self.users.items():
            if phone and user["phone"] == normalize_phone(phone):
                if wa_user_id:
                    user["wa_user_id"] = wa_user_id
                return User(id=user_id, phone=user["phone"], paused=user["paused"])
        if not phone and wa_user_id:
            for user_id, user in self.users.items():
                if user["wa_user_id"] == wa_user_id:
                    return User(id=user_id, phone=user["phone"], paused=user["paused"])
        return None

    async def first_contact_from_stranger(self, sender_key: str) -> bool:
        if sender_key in self.strangers:
            return False
        self.strangers.add(sender_key)
        return True

    async def set_paused(self, user_id: int, paused: bool) -> None:
        self.users[user_id]["paused"] = paused


@dataclass
class SentMessage:
    phone: str | None
    wa_user_id: str | None
    body: str


class FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[SentMessage] = []

    async def send_text(self, *, phone: str | None, wa_user_id: str | None, body: str) -> None:
        self.sent.append(SentMessage(phone, wa_user_id, body))

    @property
    def bodies(self) -> list[str]:
        return [message.body for message in self.sent]


@dataclass
class FakeModel:
    answer: str = "Here is your answer."
    delay_seconds: float = 0.0
    fails: bool = False
    prompts: list[str] = field(default_factory=list)

    async def complete(self, *, system: str, user: str) -> ModelReply:
        self.prompts.append(user)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fails:
            raise ModelError("HTTP 500")
        return ModelReply(text=self.answer, model="fake/model", cost_usd=0.0001)


class RecordingHandler:
    def __init__(self) -> None:
        self.messages: list[InboundMessage] = []

    async def handle(self, message: InboundMessage) -> None:
        self.messages.append(message)


OWNER = "962790000001"
STRANGER = "15550009999"


def text_message(
    body: str,
    *,
    sender: str | None = OWNER,
    message_id: str = "wamid.1",
    wa_user_id: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        id=message_id,
        kind="text",
        text=body,
        phone=sender,
        wa_user_id=wa_user_id,
        phone_number_id="1234567890",
    )


def webhook_payload(
    body: str = "Hi!",
    *,
    sender: str | None = OWNER,
    message_id: str = "wamid.1",
    phone_number_id: str = "1234567890",
    kind: str = "text",
    wa_user_id: str | None = None,
) -> dict:
    """The JSON Meta posts for one incoming message."""
    message: dict = {"id": message_id, "timestamp": "1758254144", "type": kind}
    if sender:
        message["from"] = sender
    if wa_user_id:
        message["from_user_id"] = wa_user_id
    if kind == "text":
        message["text"] = {"body": body}
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "215589313241560883",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15551797781",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Someone"}, "wa_id": sender}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }
