"""Google sign-in for Gmail (E1): the standard OAuth flow for a web server.

The user approves access on Google's own page. Tanios never sees their password; it
receives a refresh token, which is stored encrypted and never logged.
"""

import logging
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import httpx2

from tanios.gmail.client import GmailAuthError, GmailError

logger = logging.getLogger(__name__)

# Read mail, add labels, and create and send drafts. It cannot delete mail for good.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


@dataclass(frozen=True)
class Grant:
    access_token: str
    expires_in: int
    refresh_token: str


class OAuth(Protocol):
    """Tests use a stand-in with this shape (tests/fakes.py)."""

    def consent_url(self, state: str) -> str: ...

    async def exchange(self, code: str) -> Grant: ...

    async def refresh(self, refresh_token: str) -> tuple[str, int]:
        """A fresh access token and its lifetime in seconds. GmailAuthError if revoked."""
        ...

    async def revoke(self, refresh_token: str) -> None: ...


class GoogleOAuth:
    def __init__(
        self,
        http: httpx2.AsyncClient,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        auth_url: str,
        token_url: str,
        revoke_url: str,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._auth_url = auth_url
        self._token_url = token_url
        self._revoke_url = revoke_url

    def consent_url(self, state: str) -> str:
        query = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": GMAIL_SCOPE,
            # offline and consent together make Google hand over a refresh token.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{self._auth_url}?{urlencode(query)}"

    async def _token(self, form: dict[str, str]) -> dict:
        form = {**form, "client_id": self._client_id, "client_secret": self._client_secret}
        try:
            response = await self._http.post(self._token_url, data=form)
        except httpx2.HTTPError as error:
            raise GmailError(type(error).__name__) from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if response.status_code >= 400:
            # invalid_grant is Google's answer for a revoked or expired sign-in.
            if data.get("error") == "invalid_grant":
                raise GmailAuthError("Google no longer accepts this sign-in")
            raise GmailError(f"HTTP {response.status_code}")
        return data

    async def exchange(self, code: str) -> Grant:
        data = await self._token(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri}
        )
        access, refresh = data.get("access_token"), data.get("refresh_token")
        if not isinstance(access, str) or not isinstance(refresh, str) or not access or not refresh:
            raise GmailError("Google did not return a refresh token")
        # Google lets people untick permissions on the consent page.
        if GMAIL_SCOPE not in str(data.get("scope") or "").split():
            raise GmailAuthError("permission for Gmail was not granted")
        return Grant(access, _seconds(data.get("expires_in")), refresh)

    async def refresh(self, refresh_token: str) -> tuple[str, int]:
        data = await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise GmailError("unreadable response")
        return access, _seconds(data.get("expires_in"))

    async def revoke(self, refresh_token: str) -> None:
        try:
            response = await self._http.post(self._revoke_url, data={"token": refresh_token})
        except httpx2.HTTPError as error:
            raise GmailError(type(error).__name__) from None
        # 400 means Google already considers it gone, which is the result we want.
        if response.status_code >= 500:
            raise GmailError(f"HTTP {response.status_code}")


def _seconds(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 3600
