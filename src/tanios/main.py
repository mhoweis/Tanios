"""The web application.

Run with: uvicorn tanios.main:create_app --factory
"""

import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

import httpx2
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from tanios import __version__
from tanios.assistant import Assistant
from tanios.catalog import CatalogError, load_catalog, resolve_path
from tanios.config import Settings, get_settings
from tanios.db import Database, DatabaseLike
from tanios.llm import ChatCompletionsGateway
from tanios.router import CHAT, Router
from tanios.store import SqlStore
from tanios.whatsapp.client import CloudApiClient
from tanios.whatsapp.parsing import InboundMessage, parse_webhook
from tanios.whatsapp.signature import is_valid_signature

logger = logging.getLogger(__name__)


class MessageHandler(Protocol):
    async def handle(self, message: InboundMessage) -> None: ...


def create_app(
    settings: Settings | None = None,
    database: DatabaseLike | None = None,
    handler: MessageHandler | None = None,
) -> FastAPI:
    """Build the app. Tests pass their own settings, a stand-in database and handler."""
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    # The HTTP library logs every address it calls. Those can carry identifiers, so keep
    # only its warnings.
    logging.getLogger("httpx2").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = database
        if db is None and settings.database_url:
            db = Database(settings.database_url)
        app.state.database = db
        app.state.handler = handler

        http: httpx2.AsyncClient | None = None
        missing = settings.missing_for_messaging()
        if handler is None and isinstance(db, Database) and not missing:
            http = httpx2.AsyncClient(timeout=httpx2.Timeout(60.0, connect=10.0))
            try:
                app.state.handler = await _build_assistant(settings, db, http)
            except CatalogError as error:
                # The server stays up so /healthz works and the log can be read.
                logger.error("WhatsApp chat is off. The models file needs fixing: %s", error)
        elif handler is None:
            # Names only, never values.
            logger.warning("WhatsApp chat is off. Still to set: %s", ", ".join(missing))

        yield

        if http is not None:
            await http.aclose()
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

        messaging = "on" if app.state.handler is not None else "off"
        if database_state != "ok":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not ready", "database": database_state, "whatsapp": messaging}
        return {"status": "ready", "database": database_state, "whatsapp": messaging}

    @app.get("/webhooks/whatsapp", response_class=PlainTextResponse)
    async def verify_webhook(
        mode: str = Query("", alias="hub.mode"),
        token: str = Query("", alias="hub.verify_token"),
        challenge: str = Query("", alias="hub.challenge"),
    ) -> str:
        """Meta calls this once, when the webhook address is saved in its dashboard."""
        expected = settings.whatsapp_verify_token
        if expected is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WhatsApp is not configured")
        matches = hmac.compare_digest(token.encode(), expected.get_secret_value().encode())
        if mode != "subscribe" or not matches:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "verification failed")
        return challenge

    @app.post("/webhooks/whatsapp")
    async def receive_webhook(request: Request, background: BackgroundTasks) -> dict[str, str]:
        """Every incoming WhatsApp message arrives here."""
        active_handler: MessageHandler | None = app.state.handler
        secret = settings.whatsapp_app_secret
        if active_handler is None or secret is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WhatsApp is not configured")

        body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        if not is_valid_signature(body, signature, secret.get_secret_value()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")

        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "not JSON") from None

        for message in parse_webhook(payload):
            # A webhook for some other number on the same Meta app is not ours to answer.
            ours = settings.whatsapp_phone_number_id
            if ours and message.phone_number_id and message.phone_number_id != ours:
                continue
            # Answer Meta straight away; the real work happens after the response.
            background.add_task(active_handler.handle, message)
        return {"status": "received"}

    return app


async def _build_assistant(
    settings: Settings, database: Database, http: httpx2.AsyncClient
) -> Assistant:
    catalog = load_catalog(resolve_path(settings.models_file))
    store = SqlStore(database.sessions)
    router = Router(
        catalog,
        settings.provider_keys(),
        ChatCompletionsGateway(http),
        store,
        max_tokens=settings.max_reply_tokens,
    )
    if not router.can_answer(CHAT):
        raise CatalogError("no model for the chat task can be reached with the keys that are set")

    await store.ensure_owner(
        settings.owner_number,
        monthly_cap_usd=settings.monthly_cap_usd,
        allow_free_for_private=settings.allow_free_for_private,
    )
    whatsapp = CloudApiClient(
        http,
        access_token=settings.whatsapp_access_token.get_secret_value(),
        phone_number_id=settings.whatsapp_phone_number_id,
        api_base=settings.whatsapp_api_base,
        graph_version=settings.whatsapp_graph_version,
    )
    return Assistant(store, whatsapp, router, ack_after_seconds=settings.ack_after_seconds)
