"""Asking a language model for an answer.

Milestone 1 uses one model for everything. The router that picks the cheapest capable
model per task, and the usage ledger, arrive in milestone 2; this interface already
returns what they need (which model answered, and what it cost).
"""

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx2

logger = logging.getLogger(__name__)


class ModelError(Exception):
    """The model could not give an answer. The message never contains prompt text or keys."""


@dataclass(frozen=True)
class ModelReply:
    text: str
    model: str
    cost_usd: float | None


class ModelClient(Protocol):
    """Tests use a stand-in with this shape (tests/fakes.py)."""

    async def complete(self, *, system: str, user: str) -> ModelReply: ...


class ChatCompletionsClient:
    """Any provider that speaks the OpenAI chat-completions format. OpenRouter by default."""

    def __init__(
        self,
        http: httpx2.AsyncClient,
        *,
        api_key: str,
        api_base: str,
        model: str,
        max_tokens: int,
    ) -> None:
        self._http = http
        self._url = f"{api_base.rstrip('/')}/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._model = model
        self._max_tokens = max_tokens

    async def complete(self, *, system: str, user: str) -> ModelReply:
        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            response = await self._http.post(self._url, json=payload, headers=self._headers)
        except httpx2.HTTPError as error:
            raise ModelError(type(error).__name__) from None
        if response.status_code >= 400:
            raise ModelError(f"HTTP {response.status_code}")

        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("unreadable response") from None
        if not isinstance(text, str) or not text.strip():
            raise ModelError("empty answer")

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cost = usage.get("cost")
        reply = ModelReply(
            text=text.strip(),
            model=str(data.get("model") or self._model),
            cost_usd=float(cost) if isinstance(cost, int | float) else None,
        )
        logger.info("model call: model=%s cost_usd=%s", reply.model, reply.cost_usd)
        return reply
