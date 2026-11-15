"""The web application.

Run with: uvicorn tanios.main:create_app --factory
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

from tanios import __version__
from tanios.config import Settings, get_settings
from tanios.db import Database, DatabaseLike


def create_app(settings: Settings | None = None, database: DatabaseLike | None = None) -> FastAPI:
    """Build the app. Tests pass their own settings and a stand-in database."""
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = database
        if db is None and settings.database_url:
            db = Database(settings.database_url)
        app.state.database = db
        yield
        if db is not None:
            await db.dispose()

    # The interactive API pages are for development only.
    in_production = settings.env == "production"
    app = FastAPI(
        title="Tanios",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if in_production else "/docs",
        redoc_url=None,
        openapi_url=None if in_production else "/openapi.json",
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Is the server process up? Does not touch the database."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        """Can the server do real work? Checks the database."""
        db: DatabaseLike | None = app.state.database
        if db is None:
            database_state = "not configured"
        elif await db.ping():
            database_state = "ok"
        else:
            database_state = "down"

        if database_state != "ok":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not ready", "database": database_state}
        return {"status": "ready", "database": database_state}

    return app
