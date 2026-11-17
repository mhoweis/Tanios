"""Settings, read from environment variables that start with TANIOS_.

Secrets never get a default here and are never logged. On a server the values come
from the environment (see docker-compose.yml); on a developer machine they may come
from a local .env file, which git ignores.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TANIOS_", env_file=".env", extra="ignore", env_nested_delimiter="__"
    )

    env: str = "development"
    log_level: str = "INFO"
    # SQLAlchemy URL, for example postgresql+asyncpg://user:password@host:5432/tanios.
    # Left unset, the app still starts and /readyz reports the database as not configured.
    database_url: str | None = None

    # WhatsApp Cloud API. All four must be set before the webhook accepts anything.
    whatsapp_verify_token: SecretStr | None = None  # you invent it; Meta echoes it back once
    whatsapp_app_secret: SecretStr | None = None  # signs every webhook Meta sends
    whatsapp_access_token: SecretStr | None = None  # lets Tanios send messages
    whatsapp_phone_number_id: str | None = None  # Meta's ID for Tanios's number
    whatsapp_graph_version: str = "v26.0"
    whatsapp_api_base: str = "https://graph.facebook.com"

    # The owner's own WhatsApp number, digits only with country code. Registered at start.
    owner_number: str | None = None

    # Model keys. OpenRouter's has its own setting; a provider named "example" in the
    # models file reads its key from TANIOS_KEYS__EXAMPLE.
    openrouter_api_key: SecretStr | None = None
    keys: dict[str, SecretStr] = {}
    # Where the model catalog is. Empty means config/models.local.toml when it exists,
    # otherwise config/models.toml.
    models_file: str | None = None
    max_reply_tokens: int = 700

    # The owner's budget, in US dollars per calendar month. Empty means no cap. From
    # milestone 7 each user sets their own on the setup page.
    monthly_cap_usd: Decimal | None = Field(default=None, ge=0)
    # R3: may free models see the owner's email, notes and files?
    allow_free_for_private: bool = False

    # W1: say "working on it" when an answer takes longer than this. Must stay under 10.
    ack_after_seconds: float = 8.0

    # Memory (M1). The folder that holds one Obsidian vault per user, as user-1, user-2
    # and so on. Left unset, memory is off and Tanios says so when asked to remember.
    vaults_dir: Path | None = None
    # Dates in note names and the daily log use this time zone, for example Asia/Amman.
    timezone: str = "UTC"

    # M6: the largest file Tanios will save, in megabytes. Obsidian Sync has its own
    # limit per file, which depends on the plan.
    max_file_mb: int = Field(default=25, ge=1, le=100)

    # Gmail (E1). All four must be set before email switches on. docs/SETUP-GMAIL.md
    # says where each comes from.
    domain: str | None = None  # the server's web address, as used for HTTPS
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    # Encrypts the Google sign-in tokens kept in the database.
    encryption_key: SecretStr | None = None
    # Only for running on a developer machine: the address to use instead of
    # https://<domain> in sign-in links.
    public_url: str | None = None
    gmail_api_base: str = "https://gmail.googleapis.com"
    google_auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_url: str = "https://oauth2.googleapis.com/token"
    google_revoke_url: str = "https://oauth2.googleapis.com/revoke"

    @field_validator("*", mode="before")
    @classmethod
    def blank_means_unset(cls, value: object, info: ValidationInfo) -> object:
        """A line like TANIOS_OWNER_NUMBER= in .env means "not set", not an empty value."""
        if isinstance(value, str) and not value.strip():
            # Not set means the setting's own default, which is None for the secrets.
            return cls.model_fields[info.field_name].get_default(call_default_factory=True)
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                "TANIOS_TIMEZONE must be a time zone name such as Asia/Amman or UTC"
            ) from None
        return value

    def missing_for_messaging(self) -> list[str]:
        """Names of the settings still needed before WhatsApp chat can work."""
        required = {
            "TANIOS_DATABASE_URL": self.database_url,
            "TANIOS_WHATSAPP_VERIFY_TOKEN": self.whatsapp_verify_token,
            "TANIOS_WHATSAPP_APP_SECRET": self.whatsapp_app_secret,
            "TANIOS_WHATSAPP_ACCESS_TOKEN": self.whatsapp_access_token,
            "TANIOS_WHATSAPP_PHONE_NUMBER_ID": self.whatsapp_phone_number_id,
            "TANIOS_OWNER_NUMBER": self.owner_number,
            "TANIOS_OPENROUTER_API_KEY": self.provider_keys(),
        }
        return [name for name, value in required.items() if not value]

    def base_url(self) -> str | None:
        """The address people reach this server at, without a trailing slash."""
        if self.public_url:
            return self.public_url.rstrip("/")
        return f"https://{self.domain}" if self.domain else None

    def missing_for_gmail(self) -> list[str]:
        """Names of the settings still needed before email can work."""
        required = {
            "TANIOS_DOMAIN": self.base_url(),
            "TANIOS_GOOGLE_CLIENT_ID": self.google_client_id,
            "TANIOS_GOOGLE_CLIENT_SECRET": self.google_client_secret,
            "TANIOS_ENCRYPTION_KEY": self.encryption_key,
        }
        return [name for name, value in required.items() if not value]

    def provider_keys(self) -> dict[str, str]:
        """Provider name to key, for every key that is set. Never log the result."""
        keys = {
            name.lower(): secret.get_secret_value()
            for name, secret in self.keys.items()
            if secret.get_secret_value().strip()
        }
        if self.openrouter_api_key:
            keys["openrouter"] = self.openrouter_api_key.get_secret_value()
        return keys


@lru_cache
def get_settings() -> Settings:
    return Settings()
