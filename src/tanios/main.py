"""The web application.

Run with: uvicorn tanios.main:create_app --factory
"""

import asyncio
import contextlib
import hmac
import html
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx2
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from tanios import __version__
from tanios.assistant import Assistant
from tanios.audio import FfmpegConverter
from tanios.catalog import CatalogError, load_catalog, resolve_path
from tanios.config import Settings, get_settings
from tanios.crypto import SealError, TokenBox
from tanios.db import Database, DatabaseLike
from tanios.gmail.client import GmailApi
from tanios.gmail.oauth import GoogleOAuth
from tanios.keys import SqlUserKeys
from tanios.llm import ChatCompletionsGateway
from tanios.mail import DRAFT, LABEL, Mailroom
from tanios.mailstore import SqlMailStore
from tanios.memory import Memories
from tanios.people import Team
from tanios.router import CHAT, Router
from tanios.setup import COOKIE, SESSION_LIFETIME, SetupDesk, SqlSetupTokens, check_csrf, plain_page
from tanios.speech import AudioApiGateway
from tanios.store import SqlStore
from tanios.vault import VaultError
from tanios.whatsapp.client import CloudApiClient
from tanios.whatsapp.parsing import InboundMessage, parse_webhook
from tanios.whatsapp.signature import is_valid_signature

logger = logging.getLogger(__name__)


MAIL_TICK_SECONDS = 60

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tanios</title>
<style>body {{ font-family: system-ui, sans-serif; max-width: 30rem; margin: 3rem auto;
padding: 0 1rem }} h1 {{ font-size: 1.3rem }}</style></head>
<body><h1>{title}</h1><p>{text}</p></body></html>"""


def page(title: str, text: str, status_code: int = 200) -> HTMLResponse:
    body = PAGE.format(title=html.escape(title), text=html.escape(text))
    # Sign-in pages carry one-time values in their address: keep them out of caches and
    # out of the Referer header of anything the browser opens next.
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    return HTMLResponse(body, status_code=status_code, headers=headers)


class MessageHandler(Protocol):
    async def handle(self, message: InboundMessage) -> None: ...


class MailDesk(Protocol):
    """The part of the mailroom the web pages and the clock use."""

    async def consent_url(self, token: str) -> str | None: ...

    async def finish_connection(self, state: str, code: str) -> str | None: ...

    async def run_due(self) -> None: ...


class NoQueryStrings(logging.Filter):
    """Keeps what follows "?" in a web address out of the access log.

    Sign-in links, Google's reply and Meta's webhook check all carry one-time values or
    tokens there.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path, mark, _ = args[2].partition("?")
            if mark:
                record.args = (*args[:2], f"{path}?[hidden]", *args[3:])
        return True


def create_app(
    settings: Settings | None = None,
    database: DatabaseLike | None = None,
    handler: MessageHandler | None = None,
    mailroom: MailDesk | None = None,
    setup: SetupDesk | None = None,
) -> FastAPI:
    """Build the app. Tests pass their own settings, a stand-in database and handler."""
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    # The HTTP library logs every address it calls. Those can carry identifiers, so keep
    # only its warnings.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, NoQueryStrings) for item in access_log.filters):
        access_log.addFilter(NoQueryStrings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = database
        if db is None and settings.database_url:
            db = Database(settings.database_url)
        app.state.database = db
        app.state.handler = handler
        app.state.mailroom = mailroom
        app.state.setup = setup

        http: httpx2.AsyncClient | None = None
        clock: asyncio.Task | None = None
        missing = settings.missing_for_messaging()
        if handler is None and isinstance(db, Database) and not missing:
            http = httpx2.AsyncClient(timeout=httpx2.Timeout(60.0, connect=10.0))
            try:
                built = await _build_assistant(settings, db, http)
                app.state.handler, app.state.mailroom, app.state.setup = built
                if app.state.mailroom is not None:
                    # E2: the hourly check, and summaries at the times the user set.
                    clock = asyncio.create_task(_mail_clock(app.state.mailroom))
            except CatalogError as error:
                # The server stays up so /healthz works and the log can be read.
                logger.error("WhatsApp chat is off. The models file needs fixing: %s", error)
        elif handler is None:
            # Names only, never values.
            logger.warning("WhatsApp chat is off. Still to set: %s", ", ".join(missing))

        yield

        if clock is not None:
            clock.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await clock
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

    @app.get("/connect/gmail")
    async def connect_gmail(t: str = Query("", max_length=200)) -> Response:
        """The one-time link Tanios sends on WhatsApp. It leads to Google's own sign-in."""
        desk: MailDesk | None = app.state.mailroom
        target = await desk.consent_url(t) if desk is not None and t else None
        if target is None:
            return page(
                "This link no longer works",
                "Links work once, for 15 minutes. Send connect gmail to Tanios on WhatsApp "
                "for a new one.",
                status.HTTP_404_NOT_FOUND,
            )
        return RedirectResponse(
            target,
            status.HTTP_303_SEE_OTHER,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.get("/oauth/google/callback")
    async def google_callback(
        state: str = Query("", max_length=200),
        code: str = Query("", max_length=2000),
        error: str = Query("", max_length=200),
    ) -> Response:
        """Where Google sends the user after they approve or refuse."""
        desk: MailDesk | None = app.state.mailroom
        address = None
        if desk is not None and state and code and not error:
            address = await desk.finish_connection(state, code)
        if address is None:
            return page(
                "Gmail was not connected",
                "Nothing was changed. Send connect gmail to Tanios on WhatsApp to try again.",
                status.HTTP_400_BAD_REQUEST,
            )
        return page(
            "Gmail is connected",
            f"Tanios can now read and label mail for {address}. You can close this page "
            "and go back to WhatsApp.",
        )

    _add_setup_routes(app, settings)

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


def _add_setup_routes(app: FastAPI, settings: Settings) -> None:
    """The private setup page (U1). Every answer comes from SetupDesk; this is plumbing."""
    auth = urlsplit(settings.google_auth_url)
    google = f"{auth.scheme}://{auth.netloc}"
    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        # No scripts at all; forms may only go to this server, or on to Google's sign-in.
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
            f"frame-ancestors 'none'; form-action 'self' {google}"
        ),
    }

    def html_page(body: str, code: int = 200) -> HTMLResponse:
        return HTMLResponse(body, status_code=code, headers=headers)

    def no_entry() -> HTMLResponse:
        return html_page(
            plain_page(
                "This page has expired",
                "Send setup to Tanios on WhatsApp for a new link. Links work once, for 15 "
                "minutes, and a page stays open for 30.",
            ),
            status.HTTP_401_UNAUTHORIZED,
        )

    @app.get("/setup/open")
    async def open_setup(t: str = Query("", max_length=200)) -> Response:
        desk: SetupDesk | None = app.state.setup
        session = await desk.open_link(t) if desk is not None and t else None
        if session is None:
            return no_entry()
        # The one-time value leaves the address bar at once.
        response = RedirectResponse("/setup", status.HTTP_303_SEE_OTHER, headers=headers)
        response.set_cookie(
            COOKIE,
            session,
            max_age=int(SESSION_LIFETIME.total_seconds()),
            httponly=True,
            secure=desk.secure,
            samesite="strict",
            path="/setup",
        )
        return response

    @app.get("/setup")
    async def show_setup(request: Request) -> Response:
        desk: SetupDesk | None = app.state.setup
        session = request.cookies.get(COOKIE, "")
        user = await desk.session_user(session) if desk is not None else None
        if user is None:
            return no_entry()
        return html_page(await desk.page(user, session))

    @app.post("/setup/{action}")
    @app.post("/setup/{action}/{detail}")
    async def setup_form(request: Request, action: str, detail: str = "") -> Response:
        desk: SetupDesk | None = app.state.setup
        session = request.cookies.get(COOKIE, "")
        user = await desk.session_user(session) if desk is not None else None
        if user is None:
            return no_entry()
        form = await request.form()

        def field(name: str) -> str:
            value = form.get(name)
            return value if isinstance(value, str) else ""

        if not check_csrf(session, field("csrf")):
            return html_page(plain_page("Not saved", "Please reload the page."), 403)
        route = f"{action}/{detail}" if detail else action
        if route == "keys":
            outcome = await desk.save_key(user, field("provider"), field("key"))
        elif route == "keys/remove":
            outcome = await desk.remove_key(user, field("provider"))
        elif route == "budget":
            outcome = await desk.save_budget(user, field("cap"), field("free") == "yes")
        elif route == "gmail/connect":
            outcome = await desk.connect_gmail(user)
        elif route == "gmail/disconnect":
            outcome = await desk.disconnect_gmail(user)
        elif route == "delete":
            outcome = await desk.delete_everything(user, field("confirm"))
        else:
            return html_page(plain_page("Not found", "That page does not exist."), 404)

        if outcome.redirect:
            return RedirectResponse(outcome.redirect, status.HTTP_303_SEE_OTHER, headers=headers)
        if outcome.ended:
            await desk.end_session(session)
            response = html_page(plain_page("Your data is deleted", outcome.message))
            response.delete_cookie(COOKIE, path="/setup")
            return response
        # The user may have just been changed (budget), so read them again.
        user = await desk.session_user(session)
        return html_page(await desk.page(user, session, notice=outcome.message))


async def _mail_clock(desk: MailDesk) -> None:
    while True:
        await desk.run_due()
        await asyncio.sleep(MAIL_TICK_SECONDS)


async def _build_assistant(
    settings: Settings, database: Database, http: httpx2.AsyncClient
) -> tuple[Assistant, Mailroom | None, SetupDesk | None]:
    catalog = load_catalog(resolve_path(settings.models_file))
    store = SqlStore(database.sessions)
    box = _open_box(settings)
    user_keys = None if box is None else SqlUserKeys(database.sessions, box)
    router = Router(
        catalog,
        settings.provider_keys(),
        ChatCompletionsGateway(http),
        store,
        speech=AudioApiGateway(http),
        user_keys=user_keys,
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
    if not router.voice_ready():
        logger.warning("voice notes are off: no speech model can be reached")
    memories = await _open_memories(settings, store)
    mailroom = _open_mailroom(settings, database, http, store, router, whatsapp, memories)

    setup = None
    base_url = settings.base_url()
    if user_keys is not None and base_url:
        setup = SetupDesk(
            tokens=SqlSetupTokens(database.sessions),
            users=store,
            keys=user_keys,
            catalog=catalog,
            public_url=base_url,
            mail=mailroom,
            memory_on=memories is not None,
        )
        logger.info("the setup page is on")
    else:
        logger.warning(
            "the setup page and testers are off. Still to set: TANIOS_DOMAIN and "
            "TANIOS_ENCRYPTION_KEY"
        )
    team = Team(
        store=store,
        memories=memories,
        mail=mailroom,
        setup=setup,
        tester_cap_usd=settings.tester_cap_usd,
    )
    if setup is not None:
        setup.team = team

    assistant = Assistant(
        store,
        whatsapp,
        router,
        audio=FfmpegConverter(),
        memories=memories,
        mail=mailroom,
        team=team,
        ack_after_seconds=settings.ack_after_seconds,
        max_file_bytes=settings.max_file_mb * 1024 * 1024,
    )
    return assistant, mailroom, setup


def _open_box(settings: Settings) -> TokenBox | None:
    """The lock for keys and tokens kept in the database. None until a key is set."""
    if settings.encryption_key is None:
        return None
    try:
        return TokenBox(settings.encryption_key.get_secret_value())
    except SealError as error:
        logger.error("keys and tokens cannot be stored: %s", error)
        return None


def _open_mailroom(
    settings: Settings,
    database: Database,
    http: httpx2.AsyncClient,
    store: SqlStore,
    router: Router,
    whatsapp: CloudApiClient,
    memories: Memories | None,
) -> Mailroom | None:
    """Email is on when the Google settings are complete and models can do the work."""
    missing = settings.missing_for_gmail()
    if missing:
        logger.warning("email is off. Still to set: %s", ", ".join(missing))
        return None
    try:
        box = TokenBox(settings.encryption_key.get_secret_value())
        if not (router.can_answer(LABEL) and router.can_answer(DRAFT)):
            raise CatalogError("no model can be reached for the label and draft tasks")
    except (SealError, CatalogError) as error:
        logger.error("email is off: %s", error)
        return None

    base_url = settings.base_url()
    oauth = GoogleOAuth(
        http,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        redirect_uri=f"{base_url}/oauth/google/callback",
        auth_url=settings.google_auth_url,
        token_url=settings.google_token_url,
        revoke_url=settings.google_revoke_url,
    )
    logger.info("email is on")
    return Mailroom(
        store=SqlMailStore(database.sessions),
        users=store,
        oauth=oauth,
        gmail_for=lambda access_token: GmailApi(
            http, api_base=settings.gmail_api_base, access_token=access_token
        ),
        box=box,
        router=router,
        whatsapp=whatsapp,
        public_url=base_url,
        timezone=ZoneInfo(settings.timezone),
        memories=memories,
        max_file_bytes=settings.max_file_mb * 1024 * 1024,
        alert_template=(
            (settings.whatsapp_alert_template, settings.whatsapp_template_language)
            if settings.whatsapp_alert_template
            else None
        ),
    )


async def _open_memories(settings: Settings, store: SqlStore) -> Memories | None:
    """Memory is on when a vaults folder is set. The owner's vault is laid out at start (M1)."""
    if settings.vaults_dir is None:
        logger.warning("memory is off. Still to set: TANIOS_VAULTS_DIR")
        return None
    memories = Memories(settings.vaults_dir, ZoneInfo(settings.timezone))
    owner = await store.find_user(settings.owner_number, None)
    if owner is not None:
        try:
            await asyncio.to_thread(memories(owner.id).ensure_layout)
        except VaultError as error:
            # Memory stays on: each request that needs the vault reports the problem.
            logger.error("the owner's vault could not be prepared: %s", error)
            return memories
        logger.info("memory is on: the owner's vault is the folder user-%s", owner.id)
    return memories
