"""The model catalog: providers, tiers and which tier each task starts on.

It lives in a settings file (config/models.toml), not in code, because model names and
prices change far more often than Tanios does.
"""

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

# The order answers escalate in (R5). Speech is a separate kind of model, not a step up.
ESCALATION = ("free", "small", "medium", "top")
KNOWN_TIERS = (*ESCALATION, "speech")

DEFAULT_FILE = Path("config/models.toml")
LOCAL_FILE = Path("config/models.local.toml")


class CatalogError(Exception):
    """The settings file is missing or wrong. The message says what to fix."""


@dataclass(frozen=True)
class Provider:
    name: str
    api_base: str


@dataclass(frozen=True)
class ModelEntry:
    provider: str
    model: str
    free: bool = False
    # US dollars per million tokens. Only a fallback for providers that do not report cost.
    input_price: Decimal | None = None
    output_price: Decimal | None = None


@dataclass(frozen=True)
class SpeechEntry:
    """A model that turns a voice note into text ("transcribe") or text into voice ("speak")."""

    provider: str
    model: str
    free: bool = False
    # transcribe: a language hint such as "en". Empty lets the model detect it.
    language: str | None = None
    # speak: the voice name, and the audio format to ask the provider for (mp3 or pcm).
    voice: str | None = None
    format: str = "mp3"
    # Fallback prices in US dollars, used when the provider does not report cost.
    price_per_minute: Decimal | None = None
    price_per_million_chars: Decimal | None = None


@dataclass(frozen=True)
class Catalog:
    providers: dict[str, Provider]
    tiers: dict[str, tuple[ModelEntry, ...]]
    tasks: dict[str, str]
    transcribers: tuple[SpeechEntry, ...] = ()
    speakers: tuple[SpeechEntry, ...] = ()

    def tier_for(self, task_name: str) -> str:
        try:
            return self.tasks[task_name]
        except KeyError:
            raise CatalogError(f"no tier is set for the task '{task_name}' under [tasks]") from None


def resolve_path(configured: str | None) -> Path:
    """The owner's own file when there is one, otherwise the defaults shipped with Tanios."""
    if configured:
        return Path(configured)
    return LOCAL_FILE if LOCAL_FILE.exists() else DEFAULT_FILE


def load_catalog(path: Path) -> Catalog:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CatalogError(f"the models file {path} does not exist") from None
    except tomllib.TOMLDecodeError as error:
        raise CatalogError(f"the models file {path} is not valid TOML: {error}") from None
    return parse_catalog(data)


def parse_catalog(data: dict) -> Catalog:
    providers: dict[str, Provider] = {}
    for name, settings in _table(data, "providers").items():
        api_base = settings.get("api_base") if isinstance(settings, dict) else None
        if not isinstance(api_base, str) or not api_base.startswith(("http://", "https://")):
            raise CatalogError(f"provider '{name}' needs an api_base that starts with https://")
        providers[name.lower()] = Provider(name=name.lower(), api_base=api_base.rstrip("/"))
    if not providers:
        raise CatalogError("at least one provider is needed under [providers]")

    tiers: dict[str, tuple[ModelEntry, ...]] = {}
    for tier, entries in _table(data, "tiers").items():
        if tier not in KNOWN_TIERS:
            raise CatalogError(f"unknown tier '{tier}'; the tiers are {', '.join(KNOWN_TIERS)}")
        if not isinstance(entries, list):
            raise CatalogError(f"tier '{tier}' must be written as [[tiers.{tier}]] entries")
        tiers[tier] = tuple(_entry(tier, raw, providers) for raw in entries)

    tasks: dict[str, str] = {}
    for task, tier in _table(data, "tasks").items():
        if tier not in KNOWN_TIERS:
            raise CatalogError(f"task '{task}' points at unknown tier '{tier}'")
        tasks[task] = tier

    speech = _table(data, "speech")
    for key in speech:
        if key not in ("transcribe", "speak"):
            raise CatalogError(f"[speech] has 'transcribe' and 'speak', not '{key}'")
    return Catalog(
        providers=providers,
        tiers=tiers,
        tasks=tasks,
        transcribers=_speech_entries(speech, "transcribe", providers),
        speakers=_speech_entries(speech, "speak", providers),
    )


def _speech_entries(
    speech: dict, kind: str, providers: dict[str, Provider]
) -> tuple[SpeechEntry, ...]:
    entries = speech.get(kind, [])
    if not isinstance(entries, list):
        raise CatalogError(f"speech.{kind} must be written as [[speech.{kind}]] entries")
    parsed = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise CatalogError(f"an entry in speech.{kind} is not a table")
        provider = str(raw.get("provider", "")).lower()
        model = raw.get("model")
        if provider not in providers:
            raise CatalogError(f"speech.{kind} uses provider '{provider}', which is not defined")
        if not isinstance(model, str) or not model.strip():
            raise CatalogError(f"an entry in speech.{kind} has no model")
        free = raw.get("free", False)
        if not isinstance(free, bool):
            raise CatalogError(f"'free' for {model} must be true or false")
        voice = raw.get("voice")
        audio_format = raw.get("format", "mp3")
        if kind == "speak":
            if not isinstance(voice, str) or not voice.strip():
                raise CatalogError(f"{model} under speech.speak needs a voice")
            if audio_format not in ("mp3", "pcm"):
                raise CatalogError(f"format for {model} must be mp3 or pcm")
        language = raw.get("language")
        if language is not None and not isinstance(language, str):
            raise CatalogError(f'language for {model} must be text such as "en"')
        parsed.append(
            SpeechEntry(
                provider=provider,
                model=model.strip(),
                free=free,
                language=language or None,
                voice=voice.strip() if isinstance(voice, str) else None,
                format=audio_format,
                price_per_minute=_price(raw, "price_per_minute", model),
                price_per_million_chars=_price(raw, "price_per_million_chars", model),
            )
        )
    return tuple(parsed)


def _entry(tier: str, raw: object, providers: dict[str, Provider]) -> ModelEntry:
    if not isinstance(raw, dict):
        raise CatalogError(f"an entry in tier '{tier}' is not a table")
    provider = str(raw.get("provider", "")).lower()
    model = raw.get("model")
    if provider not in providers:
        raise CatalogError(f"tier '{tier}' uses provider '{provider}', which is not defined")
    if not isinstance(model, str) or not model.strip():
        raise CatalogError(f"an entry in tier '{tier}' has no model")
    free = raw.get("free", False)
    if not isinstance(free, bool):
        raise CatalogError(f"'free' for {model} must be true or false")
    return ModelEntry(
        provider=provider,
        model=model.strip(),
        free=free,
        input_price=_price(raw, "input_price", model),
        output_price=_price(raw, "output_price", model),
    )


def _price(raw: dict, key: str, model: str) -> Decimal | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise CatalogError(f"{key} for {model} must be a number, zero or more")
    return Decimal(str(value))


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise CatalogError(f"[{key}] must be a table")
    return value
