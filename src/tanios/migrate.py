"""Bring the database tables up to date.

Run with: python -m tanios.migrate
The server's start command runs this before the app. With no database configured it
does nothing, so the app can still start and answer /healthz.
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from tanios.config import get_settings

logger = logging.getLogger(__name__)


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return config


def upgrade(database_url: str | None = None) -> bool:
    """Apply every migration not yet applied. Returns False when there is no database."""
    url = database_url or get_settings().database_url
    if not url:
        logger.warning("no database configured; skipping migrations")
        return False
    config = alembic_config()
    # Passed in memory, never written to a file or a log.
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    return True


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    upgrade()
