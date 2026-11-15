"""Talking to a model provider.

This module only knows how to make one call. Which model to call, and whether the
budget allows it, is decided by the router (tanios/router.py).
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

import httpx2

from tanios.catalog import Provider


class ModelError(Exception):
    """The model could not give an answer. The message never contains prompt text or keys."""


@dataclass(frozen=True)
class ModelReply:
    text: str
    model: str  # the model that actually answered, as the provider reports it
    cost_usd: Decimal | None  # None when the provider does not report cost
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ModelGateway(Protocol):
    """Tests use a stand-in with this shape (tests/fakes.py)."""

    async def call(
        self,
        *,
        provider: Provider,
        api_key: str,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
    ) -> ModelReply: ...


class ChatCompletionsGateway:
    """Any provider that speaks the OpenAI chat-completions format."""

    def __init__(self, http: httpx2.AsyncClient) -> None:
        self._http = http

    async def call(
        self,
        *,
        provider: Provider,
        api_key: str,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
    ) -> ModelReply:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            response = await self._http.post(
                f"{provider.api_base}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx2.HTTPError as error:
            raise ModelError(type(error).__name__) from None
        if response.status_code >= 400:
            raise ModelError(f"HTTP {response.status_code}")

        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("unreadable response") from None
        if not isinstance(text, str):
            raise ModelError("unreadable response")

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ModelReply(
            text=text.strip(),
            model=str(data.get("model") or model),
            cost_usd=_cost(usage.get("cost")),
            prompt_tokens=_count(usage.get("prompt_tokens")),
            completion_tokens=_count(usage.get("completion_tokens")),
        )


def _cost(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        cost = Decimal(str(value))
    except InvalidOperation:
        return None
    return cost if cost.is_finite() and cost >= 0 else None


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
