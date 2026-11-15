"""Turning Meta's webhook JSON into the few fields Tanios needs.

A webhook can carry several messages, or none at all: delivery receipts arrive on the
same address and are ignored here.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboundMessage:
    id: str
    kind: str  # "text", "audio", "image", ...
    text: str | None
    # At least one of these two is always present. Meta leaves the phone number out when
    # the sender uses a WhatsApp username and has not shared their number with us.
    phone: str | None
    wa_user_id: str | None
    # Which of our numbers it was sent to.
    phone_number_id: str | None
    # For audio, image and other media messages: Meta's ID for the file.
    media_id: str | None = None

    @property
    def sender_key(self) -> str:
        return self.phone or self.wa_user_id or ""


def parse_webhook(payload: Any) -> list[InboundMessage]:
    """Never raises on odd input: anything that is not a readable message is skipped."""
    messages: list[InboundMessage] = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return messages

    for entry in _dicts(payload.get("entry")):
        for change in _dicts(entry.get("changes")):
            if change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            for raw in _dicts(value.get("messages")):
                message = _parse_message(raw, metadata.get("phone_number_id"))
                if message is not None:
                    messages.append(message)
    return messages


def _parse_message(raw: dict, phone_number_id: Any) -> InboundMessage | None:
    message_id = raw.get("id")
    kind = raw.get("type")
    phone = raw.get("from")
    wa_user_id = raw.get("from_user_id")
    if not isinstance(message_id, str) or not isinstance(kind, str):
        return None
    phone = phone if isinstance(phone, str) and phone else None
    wa_user_id = wa_user_id if isinstance(wa_user_id, str) and wa_user_id else None
    if phone is None and wa_user_id is None:
        return None

    text = None
    if kind == "text":
        body = raw.get("text", {}).get("body") if isinstance(raw.get("text"), dict) else None
        text = body if isinstance(body, str) else None

    media = raw.get(kind) if kind != "text" and isinstance(raw.get(kind), dict) else {}
    media_id = media.get("id")

    return InboundMessage(
        id=message_id,
        kind=kind,
        text=text,
        phone=phone,
        wa_user_id=wa_user_id,
        phone_number_id=phone_number_id if isinstance(phone_number_id, str) else None,
        media_id=media_id if isinstance(media_id, str) and media_id else None,
    )


def _dicts(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
