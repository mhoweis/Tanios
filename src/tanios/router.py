"""The model router: every model call goes through here (R1).

For each call it picks the cheapest model the task allows, keeps private content away
from free models (R3), enforces the user's monthly cap (R4), retries once on the next
tier up when an answer fails its check (R5), and writes every attempt to the usage
ledger.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from tanios.catalog import ESCALATION, Catalog, ModelEntry, SpeechEntry
from tanios.keys import UserKeys
from tanios.llm import ModelError, ModelGateway, ModelReply
from tanios.speech import SpeechGateway
from tanios.store import Store, UsageEntry, UsageSummary, User

logger = logging.getLogger(__name__)

WARN_AT = Decimal("0.8")
MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Task:
    """A kind of work. `private` marks content from email, the vault or files (R3)."""

    name: str
    private: bool = False


CHAT = Task("chat")

Check = Callable[[str], bool]


def answered(text: str) -> bool:
    """The default check: the model said something."""
    return bool(text.strip())


@dataclass(frozen=True)
class RoutedReply:
    text: str
    model: str
    tier: str
    retried: bool
    # Something the user should be told about their budget, at most once a month each.
    notice: str | None = None


@dataclass(frozen=True)
class SpokenAudio:
    data: bytes
    format: str  # "mp3" or "pcm", as the speech model returned it


class BudgetExceeded(Exception):
    """The monthly cap is reached and nothing free can do the job. The text is for the user."""


class NoModelAvailable(ModelError):
    """No model in the catalog can be reached with the keys that are set."""


@dataclass(frozen=True)
class _Choice:
    tier: str
    entry: ModelEntry


def utc_now() -> datetime:
    return datetime.now(UTC)


def start_of_month(moment: datetime) -> datetime:
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def start_of_next_month(moment: datetime) -> datetime:
    first = start_of_month(moment)
    if first.month == 12:
        return first.replace(year=first.year + 1, month=1)
    return first.replace(month=first.month + 1)


def money(amount: Decimal) -> str:
    """$1.50, $0.42, and four places for amounts under ten cents so they are not $0.00."""
    if amount == 0 or amount >= Decimal("0.10"):
        return f"${amount:.2f}"
    if amount < Decimal("0.0001"):
        return "under $0.0001"
    return f"${amount:.4f}"


class Router:
    def __init__(
        self,
        catalog: Catalog,
        keys: Mapping[str, str],
        gateway: ModelGateway,
        store: Store,
        *,
        speech: SpeechGateway | None = None,
        user_keys: UserKeys | None = None,
        max_tokens: int = 700,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        # Keys users added themselves. With this set, the server's own keys are the
        # owner's alone: a tester's calls only ever use the tester's keys (U2).
        self._user_keys = user_keys
        self._speech = speech
        self._catalog = catalog
        self._keys = {name.lower(): key for name, key in keys.items() if key}
        self._gateway = gateway
        self._store = store
        self._max_tokens = max_tokens
        self._now = now

    # What status needs ----------------------------------------------------------------

    def key_names(self) -> list[str]:
        """The providers that have a key and appear in the catalog (R2). Names only."""
        return sorted(name for name in self._keys if name in self._catalog.providers)

    def can_answer(self, task: Task) -> bool:
        """Is there any model at all for this task with the keys that are set?"""
        start = self._start_index(self._catalog.tier_for(task.name))
        return any(
            entry.provider in self._keys
            for tier in ESCALATION[start:]
            for entry in self._catalog.tiers.get(tier, ())
        )

    def voice_ready(self) -> bool:
        """Can voice notes be understood at all with the keys that are set?"""
        return self._speech is not None and any(
            entry.provider in self._keys for entry in self._catalog.transcribers
        )

    # The same questions for one user, with the keys that user may use ------------------

    async def _keys_for(self, user: User) -> dict[str, str]:
        if self._user_keys is None:
            return self._keys
        own = {
            name.lower(): key
            for name, key in (await self._user_keys.keys_for(user.id)).items()
            if key and name.lower() in self._catalog.providers
        }
        # A user's own key for a provider is used in place of the server's.
        return {**self._keys, **own} if user.is_owner else own

    async def key_names_for(self, user: User) -> list[str]:
        """R2: the providers this user has a key for, marking the ones they added."""
        own = set() if self._user_keys is None else set(await self._user_keys.keys_for(user.id))
        names = []
        for name in sorted(await self._keys_for(user)):
            if name in self._catalog.providers:
                names.append(f"{name} (added by you)" if name in own else name)
        return names

    async def can_answer_for(self, user: User, task: Task) -> bool:
        keys = await self._keys_for(user)
        start = self._start_index(self._catalog.tier_for(task.name))
        return any(
            entry.provider in keys
            for tier in ESCALATION[start:]
            for entry in self._catalog.tiers.get(tier, ())
        )

    async def voice_ready_for(self, user: User) -> bool:
        keys = await self._keys_for(user)
        return self._speech is not None and any(
            entry.provider in keys for entry in self._catalog.transcribers
        )

    async def month_usage(self, user: User) -> tuple[str, UsageSummary]:
        now = self._now()
        return f"{now:%B}", await self._store.usage_summary(user.id, start_of_month(now))

    # The one way to call a model ------------------------------------------------------

    async def complete(
        self,
        *,
        user: User,
        task: Task,
        system: str,
        prompt: str,
        check: Check = answered,
    ) -> RoutedReply:
        now = self._now()
        month = f"{now:%Y-%m}"
        cap = user.monthly_cap_usd
        spent_before = await self._store.spend_since(user.id, start_of_month(now))
        over_cap = cap is not None and spent_before >= cap

        # Over the cap, only free models are left, wherever in the tiers they sit.
        keys = await self._keys_for(user)
        start = 0 if over_cap else self._start_index(self._catalog.tier_for(task.name))
        choice = self._pick(start, task, user, keys, paid_allowed=not over_cap)
        if choice is None:
            if over_cap:
                if user.cap_reached_month != month:
                    await self._store.mark_cap_reached(user.id, month)
                raise BudgetExceeded(self._cap_reached_text(cap, now, free_left=False))
            raise NoModelAvailable("no model can be reached for this task")

        reply, cost, ok = await self._attempt(choice, user, task, system, prompt, check, now, keys)
        spent_after = spent_before + cost
        retried = False

        if not ok:
            # R5: one retry, one tier up, never more.
            higher = self._pick(
                ESCALATION.index(choice.tier) + 1, task, user, keys, paid_allowed=not over_cap
            )
            if higher is None:
                raise ModelError("no usable answer, and no higher tier to retry on")
            retried = True
            choice = higher
            reply, cost, ok = await self._attempt(
                choice, user, task, system, prompt, check, now, keys
            )
            spent_after += cost
            if not ok:
                raise ModelError("no usable answer after one retry")

        notice = await self._budget_notice(user, task, spent_after, now)
        return RoutedReply(
            text=reply.text, model=reply.model, tier=choice.tier, retried=retried, notice=notice
        )

    # Speech (W2) ----------------------------------------------------------------------

    async def transcribe(
        self, *, user: User, audio: bytes, audio_format: str, seconds: float
    ) -> str:
        """A voice note as text. Raises BudgetExceeded over the cap, ModelError on failure."""
        now = self._now()
        keys = await self._keys_for(user)
        entry = await self._pick_speech(self._catalog.transcribers, user, now, keys)
        try:
            transcript = await self._speech.transcribe(
                provider=self._catalog.providers[entry.provider],
                api_key=keys[entry.provider],
                model=entry.model,
                audio=audio,
                audio_format=audio_format,
                language=entry.language,
            )
        except ModelError as error:
            await self._record_speech(user, "transcribe", entry, Decimal(0), "error", now)
            logger.warning("transcription failed: model=%s reason=%s", entry.model, error)
            raise
        cost = transcript.cost_usd
        if cost is None:
            cost = Decimal(str(seconds)) / 60 * (entry.price_per_minute or Decimal(0))
        await self._record_speech(user, "transcribe", entry, cost, "ok", now)
        return transcript.text

    async def speak(self, *, user: User, text: str) -> SpokenAudio:
        """An answer as audio. Raises BudgetExceeded over the cap, ModelError on failure."""
        now = self._now()
        keys = await self._keys_for(user)
        entry = await self._pick_speech(self._catalog.speakers, user, now, keys)
        try:
            data = await self._speech.speak(
                provider=self._catalog.providers[entry.provider],
                api_key=keys[entry.provider],
                model=entry.model,
                voice=entry.voice,
                text=text,
                audio_format=entry.format,
            )
        except ModelError as error:
            await self._record_speech(user, "speak", entry, Decimal(0), "error", now)
            logger.warning("speech failed: model=%s reason=%s", entry.model, error)
            raise
        # Speech endpoints return audio bytes with no cost attached, so this is an
        # estimate from the listed price.
        cost = len(text) * (entry.price_per_million_chars or Decimal(0)) / MILLION
        await self._record_speech(user, "speak", entry, cost, "ok", now)
        return SpokenAudio(data=data, format=entry.format)

    async def _pick_speech(
        self, entries: tuple[SpeechEntry, ...], user: User, now: datetime, keys: Mapping[str, str]
    ) -> SpeechEntry:
        if self._speech is None:
            raise NoModelAvailable("speech is not set up")
        cap = user.monthly_cap_usd
        over_cap = (
            cap is not None and await self._store.spend_since(user.id, start_of_month(now)) >= cap
        )
        for entry in entries:
            if entry.provider in keys and (entry.free or not over_cap):
                return entry
        if over_cap:
            raise BudgetExceeded(
                f"You have reached your monthly cap of {money(cap)}, so voice notes are off "
                f"until {start_of_next_month(now).day} {start_of_next_month(now):%B}. "
                "Text messages and commands still work."
            )
        raise NoModelAvailable("no speech model can be reached")

    async def _record_speech(
        self,
        user: User,
        task: str,
        entry: SpeechEntry,
        cost: Decimal,
        outcome: str,
        now: datetime,
    ) -> None:
        await self._store.record_usage(
            UsageEntry(
                user_id=user.id,
                at=now,
                task=task,
                tier="speech",
                provider=entry.provider,
                model=entry.model,
                cost_usd=cost,
                prompt_tokens=0,
                completion_tokens=0,
                is_free=entry.free,
                private=False,
                outcome=outcome,
            )
        )
        logger.info(
            "model call: task=%s tier=speech model=%s cost_usd=%s outcome=%s",
            task,
            entry.model,
            cost,
            outcome,
        )

    # Choosing -------------------------------------------------------------------------

    @staticmethod
    def _start_index(tier: str) -> int:
        if tier not in ESCALATION:
            raise NoModelAvailable(f"tier '{tier}' cannot be used for text answers")
        return ESCALATION.index(tier)

    def _pick(
        self, start: int, task: Task, user: User, keys: Mapping[str, str], *, paid_allowed: bool
    ) -> _Choice | None:
        """The first usable model, searching from tier `start` upward."""
        for tier in ESCALATION[start:]:
            for entry in self._catalog.tiers.get(tier, ()):
                if entry.provider in keys and self._usable(
                    entry, task, user, paid_allowed=paid_allowed
                ):
                    return _Choice(tier, entry)
        return None

    def _usable(self, entry: ModelEntry, task: Task, user: User, *, paid_allowed: bool) -> bool:
        if entry.free:
            # R3: private content stays away from free models unless the user allows it.
            return not task.private or user.allow_free_for_private
        return paid_allowed

    # Calling and recording ------------------------------------------------------------

    async def _attempt(
        self,
        choice: _Choice,
        user: User,
        task: Task,
        system: str,
        prompt: str,
        check: Check,
        now: datetime,
        keys: Mapping[str, str],
    ) -> tuple[ModelReply | None, Decimal, bool]:
        entry = choice.entry
        reply: ModelReply | None = None
        try:
            reply = await self._gateway.call(
                provider=self._catalog.providers[entry.provider],
                api_key=keys[entry.provider],
                model=entry.model,
                system=system,
                user=prompt,
                max_tokens=self._max_tokens,
            )
            cost = self._cost(entry, reply)
            outcome = "ok" if check(reply.text) else "failed_check"
        except ModelError as error:
            cost = Decimal(0)
            outcome = "error"
            logger.warning("model call failed: model=%s reason=%s", entry.model, error)

        await self._store.record_usage(
            UsageEntry(
                user_id=user.id,
                at=now,
                task=task.name,
                tier=choice.tier,
                provider=entry.provider,
                model=reply.model if reply else entry.model,
                cost_usd=cost,
                prompt_tokens=reply.prompt_tokens if reply else 0,
                completion_tokens=reply.completion_tokens if reply else 0,
                is_free=entry.free,
                private=task.private,
                outcome=outcome,
            )
        )
        # R1: the log shows which model handled each task. Never the text.
        logger.info(
            "model call: task=%s tier=%s model=%s cost_usd=%s outcome=%s",
            task.name,
            choice.tier,
            reply.model if reply else entry.model,
            cost,
            outcome,
        )
        return reply, cost, outcome == "ok"

    @staticmethod
    def _cost(entry: ModelEntry, reply: ModelReply) -> Decimal:
        if reply.cost_usd is not None:
            return reply.cost_usd
        if entry.input_price is not None and entry.output_price is not None:
            return (
                reply.prompt_tokens * entry.input_price
                + reply.completion_tokens * entry.output_price
            ) / MILLION
        if not entry.free:
            logger.warning("no cost reported and no price set for %s; counted as 0", entry.model)
        return Decimal(0)

    # Budget ---------------------------------------------------------------------------

    async def _budget_notice(
        self, user: User, task: Task, spent: Decimal, now: datetime
    ) -> str | None:
        cap = user.monthly_cap_usd
        month = f"{now:%Y-%m}"
        if cap is None:
            return None
        if spent >= cap:
            if user.cap_reached_month == month:
                return None
            await self._store.mark_cap_reached(user.id, month)
            keys = await self._keys_for(user)
            free_left = self._pick(0, task, user, keys, paid_allowed=False) is not None
            return self._cap_reached_text(cap, now, free_left=free_left)
        if spent >= cap * WARN_AT and user.cap_warned_month != month:
            await self._store.mark_cap_warned(user.id, month)
            percent = int(spent * 100 / cap)
            return (
                f"Heads up: you have used {money(spent)} of your {money(cap)} "
                f"monthly cap ({percent}%)."
            )
        return None

    @staticmethod
    def _cap_reached_text(cap: Decimal, now: datetime, *, free_left: bool) -> str:
        reset = start_of_next_month(now)
        text = (
            f"You have reached your monthly cap of {money(cap)}. "
            f"Paid models are off until {reset.day} {reset:%B}. Commands still work"
        )
        if free_left:
            return text + ", and I can still answer with free models, which are less capable."
        return text + ", but I cannot answer questions until then unless the cap is raised."
