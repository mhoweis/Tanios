"""Stand-ins for the outside world, shaped like the real interfaces."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from tanios.audio import AudioError
from tanios.catalog import Catalog, Provider, parse_catalog
from tanios.llm import ModelError, ModelReply
from tanios.router import Router
from tanios.speech import Transcript
from tanios.store import UsageEntry, UsageSummary, User, normalize_phone
from tanios.whatsapp.client import WhatsAppSendError
from tanios.whatsapp.parsing import InboundMessage


class InMemoryStore:
    def __init__(self) -> None:
        self.users: dict[int, dict] = {}
        self.seen_messages: set[str] = set()
        self.strangers: set[str] = set()
        self.ledger: list[UsageEntry] = []

    def add_user(
        self,
        phone: str,
        *,
        wa_user_id: str | None = None,
        paused: bool = False,
        monthly_cap_usd: Decimal | str | None = None,
        allow_free_for_private: bool = False,
    ) -> int:
        user_id = len(self.users) + 1
        self.users[user_id] = {
            "phone": normalize_phone(phone),
            "wa_user_id": wa_user_id,
            "paused": paused,
            "monthly_cap_usd": None if monthly_cap_usd is None else Decimal(monthly_cap_usd),
            "allow_free_for_private": allow_free_for_private,
            "cap_warned_month": None,
            "cap_reached_month": None,
        }
        return user_id

    def user(self, user_id: int) -> User:
        fields = {key: value for key, value in self.users[user_id].items() if key != "wa_user_id"}
        return User(id=user_id, **fields)

    async def ensure_owner(
        self, phone: str, *, monthly_cap_usd: Decimal | None, allow_free_for_private: bool
    ) -> None:
        for user in self.users.values():
            if user["phone"] == normalize_phone(phone):
                user["monthly_cap_usd"] = monthly_cap_usd
                user["allow_free_for_private"] = allow_free_for_private
                return
        self.add_user(
            phone, monthly_cap_usd=monthly_cap_usd, allow_free_for_private=allow_free_for_private
        )

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
                return self.user(user_id)
        if not phone and wa_user_id:
            for user_id, user in self.users.items():
                if user["wa_user_id"] == wa_user_id:
                    return self.user(user_id)
        return None

    async def first_contact_from_stranger(self, sender_key: str) -> bool:
        if sender_key in self.strangers:
            return False
        self.strangers.add(sender_key)
        return True

    async def set_paused(self, user_id: int, paused: bool) -> None:
        self.users[user_id]["paused"] = paused

    async def record_usage(self, entry: UsageEntry) -> None:
        self.ledger.append(entry)

    async def spend_since(self, user_id: int, since: datetime) -> Decimal:
        return sum(
            (e.cost_usd for e in self.ledger if e.user_id == user_id and e.at >= since),
            Decimal(0),
        )

    async def usage_summary(self, user_id: int, since: datetime) -> UsageSummary:
        rows = [e for e in self.ledger if e.user_id == user_id and e.at >= since]
        tasks: dict[str, tuple[Decimal, int]] = {}
        models: dict[str, int] = {}
        for entry in rows:
            cost, calls = tasks.get(entry.task, (Decimal(0), 0))
            tasks[entry.task] = (cost + entry.cost_usd, calls + 1)
            models[entry.model] = models.get(entry.model, 0) + 1
        return UsageSummary(
            total_usd=sum((e.cost_usd for e in rows), Decimal(0)),
            by_task=tuple(
                sorted(
                    ((task, cost, calls) for task, (cost, calls) in tasks.items()),
                    key=lambda row: (-row[1], row[0]),
                )
            ),
            by_model=tuple(sorted(models.items(), key=lambda row: (-row[1], row[0]))),
        )

    async def mark_cap_warned(self, user_id: int, month: str) -> None:
        self.users[user_id]["cap_warned_month"] = month

    async def mark_cap_reached(self, user_id: int, month: str) -> None:
        self.users[user_id]["cap_reached_month"] = month


@dataclass
class SentMessage:
    phone: str | None
    wa_user_id: str | None
    body: str


class FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.voices: list[tuple[str | None, bytes]] = []
        self.media: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.voice_fails = False

    async def send_text(self, *, phone: str | None, wa_user_id: str | None, body: str) -> None:
        self.sent.append(SentMessage(phone, wa_user_id, body))

    async def download_media(self, media_id: str) -> bytes:
        self.downloads.append(media_id)
        if media_id not in self.media:
            raise WhatsAppSendError("HTTP 404")
        return self.media[media_id]

    async def send_voice(self, *, phone: str | None, wa_user_id: str | None, ogg: bytes) -> None:
        if self.voice_fails:
            raise WhatsAppSendError("HTTP 500")
        self.voices.append((phone or wa_user_id, ogg))

    @property
    def bodies(self) -> list[str]:
        return [message.body for message in self.sent]


@dataclass
class FakeSpeech:
    """Stands in for speech models. The "audio" it returns is readable text in bytes."""

    hears: str = "what is the capital of Jordan"
    transcribe_cost: Decimal | None = Decimal("0.0003")
    transcribe_fails: bool = False
    speak_fails: bool = False
    delay_seconds: float = 0.0
    transcriptions: list[dict] = field(default_factory=list)
    spoken: list[dict] = field(default_factory=list)

    async def transcribe(self, *, provider, api_key, model, audio, audio_format, language):
        self.transcriptions.append(
            {"model": model, "audio": audio, "format": audio_format, "language": language}
        )
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.transcribe_fails:
            raise ModelError("HTTP 500")
        return Transcript(text=self.hears, cost_usd=self.transcribe_cost)

    async def speak(self, *, provider, api_key, model, voice, text, audio_format):
        self.spoken.append({"model": model, "voice": voice, "text": text, "format": audio_format})
        if self.speak_fails:
            raise ModelError("HTTP 500")
        return f"{audio_format}:{text}".encode()


class FakeConverter:
    """Marks what it was given instead of converting, so tests can follow the audio."""

    def __init__(self, fails: bool = False) -> None:
        self.fails = fails

    async def to_wav(self, audio: bytes) -> bytes:
        if self.fails:
            raise AudioError("ffmpeg could not convert the audio")
        # 44 header bytes and two seconds of 16 kHz mono audio, so wav_seconds() says 2.0.
        return b"WAV:" + audio.ljust(40 + 64000, b"\0")

    async def to_voice_note(self, audio: bytes, audio_format: str) -> bytes:
        if self.fails:
            raise AudioError("ffmpeg could not convert the audio")
        return b"OGG:" + audio


# A small catalog with every tier, two providers, and one free model.
TEST_CATALOG: Catalog = parse_catalog(
    {
        "providers": {
            "openrouter": {"api_base": "https://models.example/api/v1"},
            "other": {"api_base": "https://other.example/v1"},
        },
        "tiers": {
            "free": [{"provider": "openrouter", "model": "free-model", "free": True}],
            "small": [
                {"provider": "openrouter", "model": "small-model"},
                {"provider": "other", "model": "other-small-model"},
            ],
            "medium": [{"provider": "openrouter", "model": "medium-model"}],
            "top": [{"provider": "openrouter", "model": "top-model"}],
        },
        "tasks": {"chat": "small", "sort": "free", "draft": "medium", "plan": "top"},
        "speech": {
            "transcribe": [
                {
                    "provider": "openrouter",
                    "model": "listen-model",
                    "language": "en",
                    "price_per_minute": 0.06,
                }
            ],
            "speak": [
                {
                    "provider": "openrouter",
                    "model": "voice-model",
                    "voice": "alloy",
                    "format": "mp3",
                    "price_per_million_chars": 1000,
                }
            ],
        },
    }
)

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@dataclass
class FakeGateway:
    """Stands in for every model provider. `by_model` scripts what a given model does:
    a string is its answer, an exception is raised, a Decimal is the cost of its call."""

    answer: str = "Here is your answer."
    delay_seconds: float = 0.0
    fails: bool = False
    cost: Decimal = Decimal("0.0100")
    by_model: dict[str, object] = field(default_factory=dict)
    costs: dict[str, Decimal] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)

    @property
    def prompts(self) -> list[str]:
        return [call["user"] for call in self.calls]

    @property
    def models(self) -> list[str]:
        return [call["model"] for call in self.calls]

    async def call(
        self,
        *,
        provider: Provider,
        api_key: str,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
    ) -> ModelReply:
        self.calls.append(
            {
                "provider": provider.name,
                "api_key": api_key,
                "model": model,
                "user": user,
                "system": system,
            }
        )
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        behaviour = self.by_model.get(model, self.answer)
        if self.fails or isinstance(behaviour, Exception):
            raise behaviour if isinstance(behaviour, Exception) else ModelError("HTTP 500")
        default_cost = Decimal(0) if "free" in model else self.cost
        return ModelReply(
            text=str(behaviour),
            model=model,
            cost_usd=self.costs.get(model, default_cost),
            prompt_tokens=100,
            completion_tokens=50,
        )


def make_router(
    store: InMemoryStore,
    gateway: FakeGateway,
    *,
    catalog: Catalog = TEST_CATALOG,
    keys: dict[str, str] | None = None,
    now: datetime = NOW,
    speech: FakeSpeech | None = None,
) -> Router:
    return Router(
        catalog,
        {"openrouter": "sk-or-test"} if keys is None else keys,
        gateway,
        store,
        speech=speech,
        now=lambda: now,
    )


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


def voice_message(
    media_id: str = "MEDIA1",
    *,
    sender: str | None = OWNER,
    message_id: str = "wamid.v1",
    wa_user_id: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        id=message_id,
        kind="audio",
        text=None,
        phone=sender,
        wa_user_id=wa_user_id,
        phone_number_id="1234567890",
        media_id=media_id,
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
    if kind == "audio":
        message["audio"] = {
            "mime_type": "audio/ogg; codecs=opus",
            "sha256": "wvqXMe6n7n1W0zphvLPoLj+s/NtKqmr3zZ7YzTP7xFI=",
            "id": "1908647269898587",
            "voice": True,
        }
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
