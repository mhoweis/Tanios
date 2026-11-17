"""Sending WhatsApp messages through Meta's Cloud API."""

import logging
from typing import Protocol

import httpx2

logger = logging.getLogger(__name__)

# WhatsApp refuses text bodies over 4096 characters.
MAX_TEXT_LENGTH = 4000


class WhatsAppSendError(Exception):
    pass


class WhatsAppClient(Protocol):
    """Tests use a stand-in with this shape (tests/fakes.py)."""

    async def send_text(self, *, phone: str | None, wa_user_id: str | None, body: str) -> None:
        """Send to a phone number when we have one, otherwise to the WhatsApp user ID."""
        ...


def split_text(body: str, limit: int = MAX_TEXT_LENGTH) -> list[str]:
    """Break a long reply into parts WhatsApp accepts, at a paragraph or word when possible."""
    parts: list[str] = []
    rest = body.strip()
    while len(rest) > limit:
        cut = max(rest.rfind("\n\n", 0, limit), rest.rfind("\n", 0, limit))
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


class CloudApiClient:
    def __init__(
        self,
        http: httpx2.AsyncClient,
        *,
        access_token: str,
        phone_number_id: str,
        api_base: str,
        graph_version: str,
    ) -> None:
        self._http = http
        self._url = f"{api_base.rstrip('/')}/{graph_version}/{phone_number_id}/messages"
        self._headers = {"Authorization": f"Bearer {access_token}"}

    async def send_text(self, *, phone: str | None, wa_user_id: str | None, body: str) -> None:
        if not phone and not wa_user_id:
            raise WhatsAppSendError("no recipient")
        recipient = {"to": phone} if phone else {"recipient": wa_user_id}
        for part in split_text(body):
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                **recipient,
                "type": "text",
                "text": {"preview_url": False, "body": part},
            }
            try:
                response = await self._http.post(self._url, json=payload, headers=self._headers)
            except httpx2.HTTPError as error:
                raise WhatsAppSendError(type(error).__name__) from None
            if response.status_code >= 400:
                # Meta's error body names the problem without echoing our token or the text.
                raise WhatsAppSendError(f"HTTP {response.status_code}: {_error_summary(response)}")


def _error_summary(response: httpx2.Response) -> str:
    try:
        error = response.json().get("error", {})
        return f"code {error.get('code')}, {error.get('type')}"
    except Exception:
        return "unreadable error body"
