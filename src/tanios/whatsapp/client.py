"""Sending WhatsApp messages through Meta's Cloud API."""

import logging
from typing import Protocol
from urllib.parse import urlsplit

import httpx2

logger = logging.getLogger(__name__)

# WhatsApp refuses text bodies over 4096 characters.
MAX_TEXT_LENGTH = 4000
# WhatsApp's own limit for audio files.
MAX_MEDIA_BYTES = 16 * 1024 * 1024
# Media is downloaded with our access token attached, so only from Meta's own hosts.
MEDIA_HOST_SUFFIXES = (".fbsbx.com", ".facebook.com", ".fbcdn.net", ".whatsapp.net")


class WhatsAppSendError(Exception):
    pass


class MediaTooLarge(WhatsAppSendError):
    """The file a user sent is over the size the caller allows."""


class WhatsAppClient(Protocol):
    """Tests use a stand-in with this shape (tests/fakes.py)."""

    async def send_text(self, *, phone: str | None, wa_user_id: str | None, body: str) -> None:
        """Send to a phone number when we have one, otherwise to the WhatsApp user ID."""
        ...

    async def download_media(self, media_id: str, max_bytes: int = MAX_MEDIA_BYTES) -> bytes:
        """The bytes of a file a user sent, by Meta's media ID. MediaTooLarge if over."""
        ...

    async def send_voice(self, *, phone: str | None, wa_user_id: str | None, ogg: bytes) -> None:
        """Send Ogg Opus audio so it arrives as a voice note."""
        ...

    async def send_template(
        self, *, phone: str | None, wa_user_id: str | None, name: str, language: str, text: str
    ) -> None:
        """W5: send an approved template with one text value, to reach someone first."""
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
        self._graph = f"{api_base.rstrip('/')}/{graph_version}"
        self._api_host = urlsplit(api_base).hostname or ""
        self._url = f"{self._graph}/{phone_number_id}/messages"
        self._media_url = f"{self._graph}/{phone_number_id}/media"
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
            await self._post_message(payload)

    async def download_media(self, media_id: str, max_bytes: int = MAX_MEDIA_BYTES) -> bytes:
        if not media_id.isalnum():
            raise WhatsAppSendError("unexpected media ID")
        # Step 1: ask Meta where the file is. Step 2: fetch it from there.
        details = await self._get(f"{self._graph}/{media_id}")
        try:
            info = details.json()
            url = info["url"]
            size = info.get("file_size", 0)
        except (ValueError, KeyError, TypeError):
            raise WhatsAppSendError("unreadable media details") from None
        if isinstance(size, int) and size > max_bytes:
            raise MediaTooLarge("media file is too large")
        if not self._is_meta_host(url):
            raise WhatsAppSendError("media address is not on a Meta host")
        content = (await self._get(url)).content
        if len(content) > max_bytes:
            raise MediaTooLarge("media file is too large")
        return content

    async def send_voice(self, *, phone: str | None, wa_user_id: str | None, ogg: bytes) -> None:
        if not phone and not wa_user_id:
            raise WhatsAppSendError("no recipient")
        try:
            uploaded = await self._http.post(
                self._media_url,
                data={"messaging_product": "whatsapp", "type": "audio/ogg"},
                files={"file": ("voice.ogg", ogg, "audio/ogg")},
                headers=self._headers,
            )
        except httpx2.HTTPError as error:
            raise WhatsAppSendError(type(error).__name__) from None
        if uploaded.status_code >= 400:
            raise WhatsAppSendError(f"HTTP {uploaded.status_code}: {_error_summary(uploaded)}")
        try:
            media_id = uploaded.json()["id"]
        except (ValueError, KeyError, TypeError):
            raise WhatsAppSendError("unreadable upload response") from None

        recipient = {"to": phone} if phone else {"recipient": wa_user_id}
        await self._post_message(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                **recipient,
                "type": "audio",
                "audio": {"id": media_id, "voice": True},
            }
        )

    def _is_meta_host(self, url: object) -> bool:
        if not isinstance(url, str):
            return False
        parts = urlsplit(url)
        host = parts.hostname or ""
        # The configured API host is allowed too, so local stand-ins work in tests.
        if host == self._api_host:
            return True
        return parts.scheme == "https" and host.endswith(MEDIA_HOST_SUFFIXES)

    async def _get(self, url: str) -> httpx2.Response:
        try:
            response = await self._http.get(url, headers=self._headers)
        except httpx2.HTTPError as error:
            raise WhatsAppSendError(type(error).__name__) from None
        if response.status_code >= 400:
            raise WhatsAppSendError(f"HTTP {response.status_code}: {_error_summary(response)}")
        return response

    async def send_template(
        self, *, phone: str | None, wa_user_id: str | None, name: str, language: str, text: str
    ) -> None:
        if not phone and not wa_user_id:
            raise WhatsAppSendError("no recipient")
        recipient = {"to": phone} if phone else {"recipient": wa_user_id}
        await self._post_message(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                **recipient,
                "type": "template",
                "template": {
                    "name": name,
                    "language": {"code": language},
                    "components": [
                        {"type": "body", "parameters": [{"type": "text", "text": text}]}
                    ],
                },
            }
        )

    async def _post_message(self, payload: dict) -> None:
        # A dropped connection gets one more try: a reply that arrives twice is better
        # than one that never arrives. Timeouts are not retried.
        for attempt in (1, 2):
            try:
                response = await self._http.post(self._url, json=payload, headers=self._headers)
                break
            except (httpx2.NetworkError, httpx2.RemoteProtocolError) as error:
                if attempt == 2:
                    raise WhatsAppSendError(type(error).__name__) from None
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
