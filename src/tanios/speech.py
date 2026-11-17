"""Talking to speech models: voice to text, and text to voice.

Like tanios/llm.py this only knows how to make one call. The router decides which
model to use and whether the budget allows it.
"""

import base64
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import httpx2

from tanios.catalog import Provider
from tanios.llm import ModelError, _cost


@dataclass(frozen=True)
class Transcript:
    text: str
    cost_usd: Decimal | None  # None when the provider does not report cost


class SpeechGateway(Protocol):
    """Tests use a stand-in with this shape (tests/fakes.py)."""

    async def transcribe(
        self,
        *,
        provider: Provider,
        api_key: str,
        model: str,
        audio: bytes,
        audio_format: str,
        language: str | None,
    ) -> Transcript: ...

    async def speak(
        self,
        *,
        provider: Provider,
        api_key: str,
        model: str,
        voice: str,
        text: str,
        audio_format: str,
    ) -> bytes: ...


class AudioApiGateway:
    """OpenRouter's audio endpoints: /audio/transcriptions and /audio/speech."""

    def __init__(self, http: httpx2.AsyncClient) -> None:
        self._http = http

    async def transcribe(
        self,
        *,
        provider: Provider,
        api_key: str,
        model: str,
        audio: bytes,
        audio_format: str,
        language: str | None,
    ) -> Transcript:
        payload: dict = {
            "model": model,
            "input_audio": {
                "data": base64.b64encode(audio).decode("ascii"),
                "format": audio_format,
            },
        }
        if language:
            payload["language"] = language
        response = await self._post(provider, api_key, "/audio/transcriptions", payload)
        try:
            data = response.json()
            text = data["text"]
        except (ValueError, KeyError, TypeError):
            raise ModelError("unreadable response") from None
        if not isinstance(text, str):
            raise ModelError("unreadable response")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return Transcript(text=text.strip(), cost_usd=_cost(usage.get("cost")))

    async def speak(
        self,
        *,
        provider: Provider,
        api_key: str,
        model: str,
        voice: str,
        text: str,
        audio_format: str,
    ) -> bytes:
        payload = {"model": model, "input": text, "voice": voice, "response_format": audio_format}
        response = await self._post(provider, api_key, "/audio/speech", payload)
        content_type = response.headers.get("content-type", "")
        # An error can arrive as JSON with a 200. Audio never has a JSON content type.
        if not response.content or "json" in content_type:
            raise ModelError("no audio in the response")
        return response.content

    async def _post(
        self, provider: Provider, api_key: str, path: str, payload: dict
    ) -> httpx2.Response:
        try:
            response = await self._http.post(
                f"{provider.api_base}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx2.HTTPError as error:
            raise ModelError(type(error).__name__) from None
        if response.status_code >= 400:
            raise ModelError(f"HTTP {response.status_code}")
        return response
