"""Stand-ins for the outside world, shaped like the real interfaces."""

import asyncio
import base64
import email
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal

from tanios.audio import AudioError
from tanios.catalog import Catalog, Provider, parse_catalog
from tanios.gmail.client import GmailAuthError, GmailError, GmailNotFound
from tanios.gmail.message import Attachment, Email, addresses
from tanios.gmail.oauth import Grant
from tanios.llm import ModelError, ModelReply
from tanios.mailstore import Connection, Draft, SortedEmail
from tanios.router import Router
from tanios.speech import Transcript
from tanios.store import UsageEntry, UsageSummary, User, normalize_phone
from tanios.whatsapp.client import MediaTooLarge, WhatsAppSendError
from tanios.whatsapp.parsing import InboundMessage


class InMemoryStore:
    def __init__(self) -> None:
        self.users: dict[int, dict] = {}
        self.seen_messages: set[str] = set()
        self.strangers: set[str] = set()
        self.ledger: list[UsageEntry] = []
        # Like the database's cascade: other stand-ins drop a deleted user's rows.
        self.on_delete: list[Callable[[int], None]] = []

    def add_user(
        self,
        phone: str,
        *,
        wa_user_id: str | None = None,
        paused: bool = False,
        monthly_cap_usd: Decimal | str | None = None,
        allow_free_for_private: bool = False,
        is_owner: bool = False,
    ) -> int:
        user_id = max(self.users, default=0) + 1
        self.users[user_id] = {
            "phone": normalize_phone(phone),
            "wa_user_id": wa_user_id,
            "paused": paused,
            "monthly_cap_usd": None if monthly_cap_usd is None else Decimal(monthly_cap_usd),
            "allow_free_for_private": allow_free_for_private,
            "cap_warned_month": None,
            "cap_reached_month": None,
            "last_message_at": None,
            "is_owner": is_owner,
            "template_sent_at": None,
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
            phone,
            monthly_cap_usd=monthly_cap_usd,
            allow_free_for_private=allow_free_for_private,
            is_owner=True,
        )

    async def add_tester(self, phone: str, monthly_cap_usd: Decimal | None) -> User | None:
        if any(user["phone"] == normalize_phone(phone) for user in self.users.values()):
            return None
        return self.user(self.add_user(phone, monthly_cap_usd=monthly_cap_usd))

    async def testers(self) -> list[User]:
        return [self.user(i) for i in sorted(self.users) if not self.users[i]["is_owner"]]

    async def delete_user(self, user_id: int) -> None:
        self.users.pop(user_id, None)
        self.ledger = [entry for entry in self.ledger if entry.user_id != user_id]
        for hook in self.on_delete:
            hook(user_id)

    async def set_budget(
        self, user_id: int, monthly_cap_usd: Decimal | None, allow_free_for_private: bool
    ) -> None:
        self.users[user_id].update(
            monthly_cap_usd=monthly_cap_usd,
            allow_free_for_private=allow_free_for_private,
            cap_warned_month=None,
            cap_reached_month=None,
        )

    async def mark_template(self, user_id: int, at: datetime) -> None:
        self.users[user_id]["template_sent_at"] = at

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

    async def user_by_id(self, user_id: int) -> User | None:
        return self.user(user_id) if user_id in self.users else None

    async def note_message(self, user_id: int, at: datetime) -> None:
        self.users[user_id]["last_message_at"] = at

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
        self.templates: list[tuple[str | None, str, str, str]] = []  # to, name, language, text

    async def send_text(self, *, phone: str | None, wa_user_id: str | None, body: str) -> None:
        self.sent.append(SentMessage(phone, wa_user_id, body))

    async def download_media(self, media_id: str, max_bytes: int = 16 * 1024 * 1024) -> bytes:
        self.downloads.append(media_id)
        if media_id not in self.media:
            raise WhatsAppSendError("HTTP 404")
        if len(self.media[media_id]) > max_bytes:
            raise MediaTooLarge("media file is too large")
        return self.media[media_id]

    async def send_voice(self, *, phone: str | None, wa_user_id: str | None, ogg: bytes) -> None:
        if self.voice_fails:
            raise WhatsAppSendError("HTTP 500")
        self.voices.append((phone or wa_user_id, ogg))

    async def send_template(
        self, *, phone: str | None, wa_user_id: str | None, name: str, language: str, text: str
    ) -> None:
        self.templates.append((phone or wa_user_id, name, language, text))

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
            {
                "model": model,
                "audio": audio,
                "format": audio_format,
                "language": language,
                "api_key": api_key,
            }
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
        "tasks": {
            "chat": "small",
            "sort": "free",
            "label": "small",
            "draft": "medium",
            "plan": "top",
        },
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
    # When set, it writes the answer from the call (model, system, user).
    script: Callable[[dict], str] | None = None

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
        if self.script is not None and model not in self.by_model:
            behaviour = self.script(self.calls[-1])
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


def file_message(
    media_id: str = "FILE1",
    *,
    kind: str = "document",
    caption: str | None = None,
    filename: str | None = "scan0001.pdf",
    mime_type: str | None = "application/pdf",
    sender: str | None = OWNER,
    message_id: str = "wamid.f1",
) -> InboundMessage:
    return InboundMessage(
        id=message_id,
        kind=kind,
        text=None,
        phone=sender,
        wa_user_id=None,
        phone_number_id="1234567890",
        media_id=media_id,
        caption=caption,
        filename=filename if kind == "document" else None,
        mime_type=mime_type,
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


# Gmail ---------------------------------------------------------------------------------


class InMemoryMailStore:
    def __init__(self) -> None:
        self.links: dict[str, dict] = {}
        self.conns: dict[int, Connection] = {}
        self.emails: list[SortedEmail] = []
        self.reported: set[tuple[int, str]] = set()
        self.drafts: list[Draft] = []

    async def add_link(self, token_hash: str, user_id: int, expires_at: datetime) -> None:
        self.links = {k: v for k, v in self.links.items() if v["user_id"] != user_id}
        self.links[token_hash] = {"user_id": user_id, "expires_at": expires_at, "used": False}

    async def link_user(self, token_hash: str, now: datetime) -> int | None:
        link = self.links.get(token_hash)
        if link is None or link["used"] or link["expires_at"] <= now:
            return None
        return link["user_id"]

    async def use_link(self, token_hash: str, now: datetime) -> int | None:
        user_id = await self.link_user(token_hash, now)
        if user_id is not None:
            self.links[token_hash]["used"] = True
        return user_id

    async def save_connection(
        self, user_id: int, address: str, sealed_token: str, now: datetime
    ) -> None:
        self.conns[user_id] = Connection(user_id, address, sealed_token, connected_at=now)

    async def connection(self, user_id: int) -> Connection | None:
        return self.conns.get(user_id)

    async def connections(self) -> list[Connection]:
        return [self.conns[user_id] for user_id in sorted(self.conns)]

    async def drop_connection(self, user_id: int) -> None:
        self.conns.pop(user_id, None)

    async def mark_checked(self, user_id: int, now: datetime) -> None:
        if user_id in self.conns:
            self.conns[user_id] = replace(self.conns[user_id], last_checked_at=now)

    async def set_digest_times(self, user_id: int, times: tuple[str, ...]) -> None:
        self.conns[user_id] = replace(self.conns[user_id], digest_times=times)

    async def mark_digest(self, user_id: int, key: str) -> None:
        self.conns[user_id] = replace(self.conns[user_id], last_digest_key=key)

    async def known_ids(self, user_id: int, gmail_ids: list[str]) -> set[str]:
        return {e.gmail_id for e in self.emails if e.user_id == user_id} & set(gmail_ids)

    async def add_email(self, email: SortedEmail, now: datetime) -> None:
        if not await self.known_ids(email.user_id, [email.gmail_id]):
            self.emails.append(email)

    async def unreported(self, user_id: int) -> list[SortedEmail]:
        rows = [
            e
            for e in self.emails
            if e.user_id == user_id and (user_id, e.gmail_id) not in self.reported
        ]
        return sorted(rows, key=lambda e: e.received_at)

    async def show_list(self, user_id: int, gmail_ids: list[str], also_seen: list[str]) -> None:
        numbers = {gmail_id: number for number, gmail_id in enumerate(gmail_ids, start=1)}
        self.emails = [
            replace(e, list_number=numbers.get(e.gmail_id)) if e.user_id == user_id else e
            for e in self.emails
        ]
        self.reported |= {(user_id, gmail_id) for gmail_id in [*gmail_ids, *also_seen]}

    async def email_number(self, user_id: int, number: int) -> SortedEmail | None:
        for e in self.emails:
            if e.user_id == user_id and e.list_number == number:
                return e
        return None

    async def add_draft(self, *, user_id: int, now: datetime, **fields) -> Draft:
        number = max((d.number for d in self.drafts if d.user_id == user_id), default=0) + 1
        draft = Draft(
            id=len(self.drafts) + 1, user_id=user_id, number=number, status="pending", **fields
        )
        self.drafts.append(draft)
        return draft

    async def open_drafts(self, user_id: int) -> list[Draft]:
        return [d for d in self.drafts if d.user_id == user_id and d.status == "pending"]

    def _swap(self, draft_id: int, **changes) -> None:
        self.drafts = [replace(d, **changes) if d.id == draft_id else d for d in self.drafts]

    async def revise_draft(self, draft_id: int, **changes) -> None:
        self._swap(draft_id, **changes)

    async def move_draft(self, draft_id: int, before: str, after: str, now: datetime) -> bool:
        for draft in self.drafts:
            if draft.id == draft_id and draft.status == before:
                self._swap(draft_id, status=after)
                return True
        return False


class FakeOAuth:
    """Stands in for Google sign-in."""

    def __init__(self) -> None:
        self.revoked = False  # the user removed Tanios in their Google account
        self.permission_granted = True
        self.revoke_fails = False
        self.exchanged: list[str] = []
        self.refreshes = 0
        self.revocations: list[str] = []

    def consent_url(self, state: str) -> str:
        return f"https://accounts.google.example/auth?state={state}"

    async def exchange(self, code: str) -> Grant:
        self.exchanged.append(code)
        if code == "bad-code":
            raise GmailError("HTTP 400")
        if not self.permission_granted:
            raise GmailAuthError("permission for Gmail was not granted")
        return Grant(access_token="access-1", expires_in=3600, refresh_token="refresh-secret")

    async def refresh(self, refresh_token: str) -> tuple[str, int]:
        self.refreshes += 1
        if self.revoked or refresh_token != "refresh-secret":
            raise GmailAuthError("Google no longer accepts this sign-in")
        return f"access-{self.refreshes + 1}", 3600

    async def revoke(self, refresh_token: str) -> None:
        if self.revoke_fails:
            raise GmailError("ConnectError")
        self.revocations.append(refresh_token)
        self.revoked = True


def an_email(
    gmail_id: str,
    subject: str,
    text: str = "Hello.",
    *,
    sender: str = "Rana Haddad",
    sender_address: str = "rana@example.com",
    to: tuple[str, ...] = ("owner@example.com",),
    cc: tuple[str, ...] = (),
    reply_to: str | None = None,
    thread_id: str | None = None,
    received_at: datetime | None = None,
    attachments: tuple[Attachment, ...] = (),
) -> Email:
    return Email(
        id=gmail_id,
        thread_id=thread_id or f"t-{gmail_id}",
        sender=sender,
        sender_address=sender_address,
        to=to,
        cc=cc,
        reply_to=reply_to,
        subject=subject,
        message_id=f"<{gmail_id}@mail.example.com>",
        references="",
        received_at=received_at or datetime(2026, 11, 15, 11, 0, tzinfo=UTC),
        text=text,
        attachments=attachments,
    )


class FakeGmail:
    """One mailbox. It has no way to delete, archive or forward, like the real client."""

    def __init__(self, oauth: FakeOAuth, address: str = "owner@example.com") -> None:
        self._oauth = oauth
        self.own_address = address
        self.inbox: list[Email] = []  # newest first
        self.labels: dict[str, str] = {}
        self.label_creations: list[str] = []
        self.applied: list[tuple[str, str]] = []  # (message id, label name)
        self.drafts: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.calls: list[str] = []
        self.files: dict[str, bytes] = {}  # attachment ID -> bytes
        self.fail_send = False
        self._token: Callable | None = None

    def bind(self, access_token: Callable) -> "FakeGmail":
        self._token = access_token
        return self

    def deliver(self, *emails: Email) -> None:
        self.inbox[:0] = list(reversed(emails))

    async def _enter(self, call: str) -> None:
        self.calls.append(call)
        token = await self._token(False)
        if self._oauth.revoked:
            # Google refuses a revoked token; a real client then asks for a fresh one.
            token = await self._token(True)
        assert token.startswith("access-")

    async def address(self) -> str:
        await self._enter("address")
        return self.own_address

    async def inbox_ids(self, *, after: datetime, limit: int) -> list[str]:
        await self._enter("inbox_ids")
        return [e.id for e in self.inbox if e.received_at > after][:limit]

    async def message(self, message_id: str) -> Email:
        await self._enter("message")
        for item in self.inbox:
            if item.id == message_id:
                return item
        raise GmailNotFound("not found")

    async def thread(self, thread_id: str) -> list[Email]:
        await self._enter("thread")
        found = [e for e in self.inbox if e.thread_id == thread_id]
        if not found:
            raise GmailNotFound("not found")
        return sorted(found, key=lambda e: e.received_at)

    async def attachment(self, message_id: str, attachment_id: str) -> bytes:
        await self._enter("attachment")
        if attachment_id not in self.files:
            raise GmailNotFound("not found")
        return self.files[attachment_id]

    async def ensure_labels(self, names: list[str]) -> dict[str, str]:
        await self._enter("ensure_labels")
        for name in names:
            if name not in self.labels:
                self.labels[name] = f"Label_{len(self.labels) + 1}"
                self.label_creations.append(name)
        return {name: self.labels[name] for name in names}

    async def add_label(self, message_id: str, label_id: str) -> None:
        await self._enter("add_label")
        name = next(name for name, value in self.labels.items() if value == label_id)
        self.applied.append((message_id, name))

    def _read(self, raw: str) -> dict:
        message = email.message_from_bytes(base64.urlsafe_b64decode(raw))
        return {
            "to": addresses(message.get("To", "")),
            "cc": addresses(message.get("Cc", "")),
            "subject": message.get("Subject", ""),
            "in_reply_to": message.get("In-Reply-To", ""),
            "body": message.get_payload(decode=True).decode().strip(),
        }

    async def create_draft(self, *, thread_id: str, raw: str) -> str:
        await self._enter("create_draft")
        draft_id = f"draft-{len(self.drafts) + 1}"
        self.drafts[draft_id] = {"thread_id": thread_id, **self._read(raw)}
        return draft_id

    async def update_draft(self, draft_id: str, *, thread_id: str, raw: str) -> None:
        await self._enter("update_draft")
        if draft_id not in self.drafts:
            raise GmailNotFound("not found")
        self.drafts[draft_id] = {"thread_id": thread_id, **self._read(raw)}

    async def draft_recipients(self, draft_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        await self._enter("draft_recipients")
        if draft_id not in self.drafts:
            raise GmailNotFound("not found")
        return self.drafts[draft_id]["to"], self.drafts[draft_id]["cc"]

    async def send_draft(self, draft_id: str) -> None:
        await self._enter("send_draft")
        if self.fail_send:
            raise GmailError("HTTP 500")
        if draft_id not in self.drafts:
            raise GmailNotFound("not found")
        await asyncio.sleep(0)
        self.sent.append(self.drafts.pop(draft_id))


def mail_model(
    labels: dict[str, str] | None = None, draft: str = "Yes, Thursday works."
) -> Callable:
    """A model script for mail: labels emails by subject, and writes a fixed reply.

    `labels` maps a word in the subject to a label; anything else is fyi.
    """
    labels = labels or {}

    def answer(call: dict) -> str:
        if call["system"].startswith("You sort"):
            lines = []
            blocks = re.findall(r'<email number="(\d+)">\n(.*?)\n</email>', call["user"], re.S)
            for number, block in blocks:
                subject = re.search(r"^Subject: (.*)$", block, re.M).group(1)
                label = next((v for k, v in labels.items() if k in subject), "fyi")
                lines.append(f"{number} | {label} | About {subject.lower()}.")
            return "\n".join(lines)
        if call["system"].startswith("You write an email reply"):
            return draft
        return "Here is your answer."

    return answer


# Keys and the setup page ---------------------------------------------------------------


class InMemoryUserKeys:
    def __init__(self, store: InMemoryStore | None = None) -> None:
        self.keys: dict[int, dict[str, str]] = {}
        if store is not None:
            store.on_delete.append(lambda user_id: self.keys.pop(user_id, None))

    async def keys_for(self, user_id: int) -> dict[str, str]:
        return dict(self.keys.get(user_id, {}))

    async def put(self, user_id: int, provider: str, key: str) -> None:
        self.keys.setdefault(user_id, {})[provider.lower()] = key

    async def remove(self, user_id: int, provider: str) -> None:
        self.keys.get(user_id, {}).pop(provider.lower(), None)


class InMemorySetupTokens:
    def __init__(self, store: InMemoryStore | None = None) -> None:
        self.rows: dict[str, dict] = {}
        if store is not None:
            store.on_delete.append(self._forget)

    def _forget(self, user_id: int) -> None:
        self.rows = {k: v for k, v in self.rows.items() if v["user_id"] != user_id}

    async def add(self, hashed: str, user_id: int, kind: str, expires_at: datetime) -> None:
        if kind == "link":
            self.rows = {
                k: v
                for k, v in self.rows.items()
                if not (v["user_id"] == user_id and v["kind"] == "link")
            }
        self.rows[hashed] = {"user_id": user_id, "kind": kind, "expires": expires_at, "used": False}

    async def holder(self, hashed: str, kind: str, now: datetime) -> int | None:
        row = self.rows.get(hashed)
        if row is None or row["kind"] != kind or row["used"] or row["expires"] <= now:
            return None
        return row["user_id"]

    async def use(self, hashed: str, kind: str, now: datetime) -> int | None:
        user_id = await self.holder(hashed, kind, now)
        if user_id is not None:
            self.rows[hashed]["used"] = True
        return user_id

    async def end(self, hashed: str) -> None:
        self.rows.pop(hashed, None)
