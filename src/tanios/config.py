"""Settings, read from environment variables that start with TANIOS_.

Secrets never get a default here and are never logged. On a server the values come
from the environment (see docker-compose.yml); on a developer machine they may come
from a local .env file, which git ignores.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TANIOS_", env_file=".env", extra="ignore")

    env: str = "development"
    log_level: str = "INFO"
    # SQLAlchemy URL, for example postgresql+asyncpg://user:password@host:5432/tanios.
    # Left unset, the app still starts and /readyz reports the database as not configured.
    database_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
