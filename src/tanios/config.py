"""Settings, read from environment variables that start with TANIOS_.

Secrets never get a default here and are never logged. On a server the values come
from the environment (see docker-compose.yml); on a developer machine they may come
from a local .env file, which git ignores.
"""

from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TANIOS_", env_file=".env", extra="ignore")

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

    # The model provider. Any address that speaks the OpenAI chat-completions format works.
    openrouter_api_key: SecretStr | None = None
    model_api_base: str = "https://openrouter.ai/api/v1"
    # Model names change often, so there is no default in code. See .env.example.
    default_model: str | None = None
    max_reply_tokens: int = 700

    # W1: say "working on it" when an answer takes longer than this. Must stay under 10.
    ack_after_seconds: float = 8.0

    @field_validator("*", mode="before")
    @classmethod
    def blank_means_unset(cls, value: object) -> object:
        """A line like TANIOS_OWNER_NUMBER= in .env means "not set", not an empty value."""
        if isinstance(value, str) and not value.strip():
            return None
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
            "TANIOS_OPENROUTER_API_KEY": self.openrouter_api_key,
            "TANIOS_DEFAULT_MODEL": self.default_model,
        }
        return [name for name, value in required.items() if not value]


@lru_cache
def get_settings() -> Settings:
    return Settings()
