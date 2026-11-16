"""Reading Gmail messages into plain data, and writing replies.

Everything read here was written by someone else. It is data for Tanios to sort and
summarise, never an instruction (E7).
"""

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses
from html.parser import HTMLParser

MAX_TEXT_CHARS = 20_000
_ADDRESS = re.compile(
    r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9\-]+)+$"
)
ADDRESS_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-']+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Attachment:
    filename: str
    mime_type: str
    size: int
    attachment_id: str


@dataclass(frozen=True)
class Email:
    id: str
    thread_id: str
    sender: str  # as shown, for example "Rana Haddad"
    sender_address: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    reply_to: str | None
    subject: str
    message_id: str  # the Message-ID header, used to thread a reply
    references: str
    received_at: datetime
    text: str
    attachments: tuple[Attachment, ...] = ()

    @property
    def people(self) -> set[str]:
        """Every address this message came from or went to."""
        return {self.sender_address, *self.to, *self.cc} - {""}


def is_address(text: str) -> bool:
    return len(text) <= 254 and bool(_ADDRESS.match(text))


def addresses(header: str) -> tuple[str, ...]:
    """The valid email addresses in a To, Cc or From header, lower-cased, in order."""
    found: list[str] = []
    for _, address in getaddresses([header]):
        address = address.strip().lower()
        if is_address(address) and address not in found:
            found.append(address)
    return tuple(found)


def one_line(text: str, limit: int) -> str:
    """Header text made safe to show or reuse: one line, no control characters, shortened."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    cleaned = _SPACE.sub(" ", cleaned).strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


class _TextFromHtml(HTMLParser):
    _SKIP = {"script", "style", "head", "title"}
    _BREAK = {"p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextFromHtml()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return ""
    lines = (_SPACE.sub(" ", line).strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _decode(data: object) -> str:
    if not isinstance(data, str) or not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


def _bodies(part: object, found: dict[str, list[str]], depth: int = 0) -> None:
    """Collect the text/plain and text/html bodies of a message, skipping attachments."""
    if not isinstance(part, dict) or depth > 8:
        return
    mime = str(part.get("mimeType") or "").lower()
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    if mime in found and not part.get("filename"):
        text = _decode(body.get("data"))
        if text:
            found[mime].append(text)
    for child in part.get("parts") or []:
        _bodies(child, found, depth + 1)


def _attachments(part: object, found: list[Attachment], depth: int = 0) -> None:
    """Collect the files attached to a message: their names and sizes, not their bytes."""
    if not isinstance(part, dict) or depth > 8 or len(found) >= 50:
        return
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    filename, attachment_id, size = part.get("filename"), body.get("attachmentId"), body.get("size")
    if isinstance(filename, str) and filename.strip() and isinstance(attachment_id, str):
        found.append(
            Attachment(
                filename=one_line(filename, 200),
                mime_type=str(part.get("mimeType") or "").lower(),
                size=size if isinstance(size, int) and not isinstance(size, bool) else 0,
                attachment_id=attachment_id,
            )
        )
    for child in part.get("parts") or []:
        _attachments(child, found, depth + 1)


def attachment_bytes(resource: object) -> bytes:
    """The bytes from Gmail's answer for one attachment. Raises ValueError if unreadable."""
    data = resource.get("data") if isinstance(resource, dict) else None
    if not isinstance(data, str) or not data:
        raise ValueError("no attachment data")
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("unreadable attachment data") from None


def parse_message(resource: object) -> Email:
    """A Gmail API message (format=full) as an Email. Raises ValueError if it is malformed."""
    if not isinstance(resource, dict):
        raise ValueError("not a message")
    payload = resource.get("payload") if isinstance(resource.get("payload"), dict) else {}
    headers: dict[str, str] = {}
    for header in payload.get("headers") or []:
        if isinstance(header, dict):
            name = str(header.get("name") or "").lower()
            if name and name not in headers:
                headers[name] = str(header.get("value") or "")

    message_id, thread_id = resource.get("id"), resource.get("threadId")
    if not isinstance(message_id, str) or not isinstance(thread_id, str):
        raise ValueError("message has no ID")

    found: dict[str, list[str]] = {"text/plain": [], "text/html": []}
    _bodies(payload, found)
    attached: list[Attachment] = []
    _attachments(payload, attached)
    text = "\n".join(found["text/plain"]).strip()
    if not text:
        text = html_to_text("\n".join(found["text/html"]))
    if not text:
        text = str(resource.get("snippet") or "")

    sender_header = headers.get("from", "")
    sender_addresses = addresses(sender_header)
    names = [name for name, _ in getaddresses([sender_header]) if name.strip()]
    sender_address = sender_addresses[0] if sender_addresses else ""
    reply_to = addresses(headers.get("reply-to", ""))

    try:
        received = datetime.fromtimestamp(int(resource.get("internalDate")) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        received = datetime.fromtimestamp(0, tz=UTC)

    return Email(
        id=message_id,
        thread_id=thread_id,
        sender=one_line(names[0] if names else sender_address or "Unknown sender", 80),
        sender_address=sender_address,
        to=addresses(headers.get("to", "")),
        cc=addresses(headers.get("cc", "")),
        reply_to=reply_to[0] if reply_to else None,
        subject=one_line(headers.get("subject", ""), 200) or "(no subject)",
        message_id=one_line(headers.get("message-id", ""), 500),
        references=one_line(headers.get("references", ""), 4000),
        received_at=received,
        text=text[:MAX_TEXT_CHARS],
        attachments=tuple(attached),
    )


def reply_subject(subject: str) -> str:
    subject = one_line(subject, 250)
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def build_reply(
    *,
    to: list[str],
    cc: list[str],
    subject: str,
    body: str,
    in_reply_to: str,
    references: str,
) -> str:
    """A reply as Gmail wants it: a whole email, base64url encoded.

    Gmail fills in the From address. Every recipient is checked again here, so nothing
    that is not a plain address can reach a header.
    """
    if not to or not all(is_address(address) for address in [*to, *cc]):
        raise ValueError("a recipient is not a valid email address")
    message = EmailMessage(policy=SMTP)
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = one_line(subject, 250)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = f"{references} {in_reply_to}".strip()
    message.set_content(body.strip() + "\n")
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def recipients_of(resource: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The To and Cc (with Bcc) addresses of a draft as Gmail holds it now."""
    message = resource.get("message") if isinstance(resource, dict) else None
    payload = message.get("payload") if isinstance(message, dict) else None
    headers = payload.get("headers") if isinstance(payload, dict) else None
    to: list[str] = []
    others: list[str] = []
    for header in headers or []:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name") or "").lower()
        if name == "to":
            to.extend(addresses(str(header.get("value") or "")))
        elif name in ("cc", "bcc"):
            others.extend(addresses(str(header.get("value") or "")))
    return tuple(dict.fromkeys(to)), tuple(dict.fromkeys(others))
