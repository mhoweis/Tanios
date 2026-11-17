"""The Gmail API, reduced to the few things Tanios is allowed to do.

There is no method here that deletes, archives, trashes, forwards or changes settings,
and the one place that makes requests refuses any call outside a short list. Tanios adds
labels, reads mail, and creates, updates and sends drafts. Nothing else.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

import httpx2

from tanios.gmail.message import Email, parse_message, recipients_of

logger = logging.getLogger(__name__)

_ID = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

# Method and path, relative to /gmail/v1/users/me. Anything else is refused before it
# is sent. Note what is absent: DELETE, trash, batchModify, settings, filters, forwarding.
ALLOWED_CALLS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", r"^/profile$"),
        ("GET", r"^/messages$"),
        ("GET", r"^/messages/[A-Za-z0-9_\-]+$"),
        ("GET", r"^/threads/[A-Za-z0-9_\-]+$"),
        ("GET", r"^/labels$"),
        ("POST", r"^/labels$"),
        ("POST", r"^/messages/[A-Za-z0-9_\-]+/modify$"),
        ("POST", r"^/drafts$"),
        ("GET", r"^/drafts/[A-Za-z0-9_\-]+$"),
        ("PUT", r"^/drafts/[A-Za-z0-9_\-]+$"),
        ("POST", r"^/drafts/send$"),
    )
)


class GmailError(Exception):
    """Gmail could not do what was asked. The text never holds mail content or tokens."""


class GmailAuthError(GmailError):
    """Google no longer accepts Tanios's access: revoked, expired, or never complete (E1)."""


class GmailNotFound(GmailError):
    """The message or draft is gone."""


class Gmail(Protocol):
    """One user's mailbox. Tests use a stand-in with this shape (tests/fakes.py)."""

    async def address(self) -> str: ...

    async def inbox_ids(self, *, after: datetime, limit: int) -> list[str]:
        """IDs of inbox messages received after a moment, newest first."""
        ...

    async def message(self, message_id: str) -> Email: ...

    async def thread(self, thread_id: str) -> list[Email]: ...

    async def ensure_labels(self, names: list[str]) -> dict[str, str]:
        """Label name to label ID, creating the labels that do not exist yet."""
        ...

    async def add_label(self, message_id: str, label_id: str) -> None: ...

    async def create_draft(self, *, thread_id: str, raw: str) -> str: ...

    async def update_draft(self, draft_id: str, *, thread_id: str, raw: str) -> None: ...

    async def draft_recipients(self, draft_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Who the draft would go to as it stands in Gmail now: To, then Cc and Bcc."""
        ...

    async def send_draft(self, draft_id: str) -> None: ...


def allowed(method: str, path: str) -> bool:
    return any(method == verb and pattern.match(path) for verb, pattern in ALLOWED_CALLS)


def _checked(identifier: str) -> str:
    if not isinstance(identifier, str) or not _ID.match(identifier):
        raise GmailError("unexpected ID")
    return identifier


class GmailApi:
    def __init__(
        self,
        http: httpx2.AsyncClient,
        *,
        api_base: str,
        access_token: Callable[[bool], Awaitable[str]],
    ) -> None:
        self._http = http
        self._base = f"{api_base.rstrip('/')}/gmail/v1/users/me"
        # Called with True to force a fresh token after Google refuses the current one.
        self._access_token = access_token

    async def _call(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> dict:
        if not allowed(method, path):
            raise GmailError("this Gmail call is not allowed")
        if path.endswith("/modify") and set(json or {}) != {"addLabelIds"}:
            # Removing a label is how mail gets archived or marked read. Never.
            raise GmailError("only adding labels is allowed")

        response = None
        for fresh in (False, True):
            token = await self._access_token(fresh)
            try:
                response = await self._http.request(
                    method,
                    self._base + path,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx2.HTTPError as error:
                raise GmailError(type(error).__name__) from None
            if response.status_code != 401:
                break
        if response.status_code == 401:
            raise GmailAuthError("Google refused the access token")
        if response.status_code == 403 and "insufficient" in response.text.lower():
            raise GmailAuthError("permission for Gmail is missing")
        if response.status_code == 404:
            raise GmailNotFound("not found")
        if response.status_code >= 400:
            raise GmailError(f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise GmailError("unreadable response") from None
        if not isinstance(data, dict):
            raise GmailError("unreadable response")
        return data

    async def address(self) -> str:
        profile = await self._call("GET", "/profile")
        address = str(profile.get("emailAddress") or "").strip().lower()
        if not address:
            raise GmailError("unreadable response")
        return address

    async def inbox_ids(self, *, after: datetime, limit: int) -> list[str]:
        params = {"q": f"in:inbox after:{int(after.timestamp())}", "maxResults": limit}
        listing = await self._call("GET", "/messages", params=params)
        ids = [item.get("id") for item in listing.get("messages") or [] if isinstance(item, dict)]
        return [item for item in ids if isinstance(item, str) and _ID.match(item)]

    async def message(self, message_id: str) -> Email:
        resource = await self._call(
            "GET", f"/messages/{_checked(message_id)}", params={"format": "full"}
        )
        try:
            return parse_message(resource)
        except ValueError:
            raise GmailError("unreadable message") from None

    async def thread(self, thread_id: str) -> list[Email]:
        resource = await self._call(
            "GET", f"/threads/{_checked(thread_id)}", params={"format": "full"}
        )
        emails = []
        for item in resource.get("messages") or []:
            # A draft sitting in the thread is not part of the conversation yet.
            if isinstance(item, dict) and "DRAFT" in (item.get("labelIds") or []):
                continue
            try:
                emails.append(parse_message(item))
            except ValueError:
                continue
        return emails

    async def ensure_labels(self, names: list[str]) -> dict[str, str]:
        listing = await self._call("GET", "/labels")
        existing = {
            str(label.get("name")): str(label.get("id"))
            for label in listing.get("labels") or []
            if isinstance(label, dict) and label.get("name") and label.get("id")
        }
        for name in names:
            if name not in existing:
                created = await self._call(
                    "POST",
                    "/labels",
                    json={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                existing[name] = _checked(str(created.get("id") or ""))
        return {name: existing[name] for name in names}

    async def add_label(self, message_id: str, label_id: str) -> None:
        await self._call(
            "POST",
            f"/messages/{_checked(message_id)}/modify",
            json={"addLabelIds": [_checked(label_id)]},
        )

    async def create_draft(self, *, thread_id: str, raw: str) -> str:
        draft = await self._call(
            "POST", "/drafts", json={"message": {"raw": raw, "threadId": _checked(thread_id)}}
        )
        return _checked(str(draft.get("id") or ""))

    async def update_draft(self, draft_id: str, *, thread_id: str, raw: str) -> None:
        await self._call(
            "PUT",
            f"/drafts/{_checked(draft_id)}",
            json={"message": {"raw": raw, "threadId": _checked(thread_id)}},
        )

    async def draft_recipients(self, draft_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        draft = await self._call(
            "GET", f"/drafts/{_checked(draft_id)}", params={"format": "metadata"}
        )
        return recipients_of(draft)

    async def send_draft(self, draft_id: str) -> None:
        await self._call("POST", "/drafts/send", json={"id": _checked(draft_id)})
