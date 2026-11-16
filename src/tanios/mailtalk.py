"""Recognising the mail commands in a WhatsApp message, with plain rules and no model.

A command is recognised from the shape of the whole message, so that an ordinary
sentence that happens to contain "send" or "change" is still just a question.
"""

import re
from dataclasses import dataclass

_LEAD = re.compile(
    r"^(?:(?:hey|hi|hello|ok|okay)\b[\s,]*)?(?:tanios\b[\s,.!:]*)?"
    r"(?:(?:please|can you|could you|will you)\b[\s,]*)*",
    re.IGNORECASE,
)

_MAILBOX = r"(?:g-?mail|e-?mails?|mail|inbox)"
_CONNECT = re.compile(rf"^(dis)?connect\s+(?:my\s+|the\s+)?{_MAILBOX}$", re.IGNORECASE)
_CHECK = re.compile(rf"^check\s+(?:my\s+|the\s+)?{_MAILBOX}(?:\s+now)?$", re.IGNORECASE)
_NEWS_PLAIN = re.compile(r"^(?:what'?s|what\s+is|whats)\s+new$", re.IGNORECASE)
_NEWS_MAIL = re.compile(
    rf"^(?:(?:what'?s|what\s+is|whats|anything)\s+new\s+in\s+(?:my\s+|the\s+)?{_MAILBOX}"
    rf"|any\s+new\s+(?:{_MAILBOX}|messages)"
    rf"|(?:summari[sz]e|sum\s+up)\s+(?:my\s+|the\s+)?(?:new\s+)?{_MAILBOX})$",
    re.IGNORECASE,
)
_DIGEST = re.compile(r"^(?:mail|email|e-mail)\s+summar(?:y|ies)\b(.*)$", re.IGNORECASE | re.DOTALL)
_DRAFT = re.compile(
    r"^(?:(?:draft|write|prepare)\s+(?:me\s+)?(?:a\s+|the\s+)?)?reply(?P<all>[\s-]+all)?\s+to\s+"
    r"(?:e-?mail\s+|number\s+|no\.?\s*|#)?(?P<number>\d{1,2})\b[\s:,.-]*"
    r"(?:(?:saying|that\s+says|to\s+say|and\s+say)\b[\s:,-]*)?(?P<wish>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_NUMBER = r"(?:\s+(?:it|draft|the\s+draft))?(?:\s+#?(\d{1,3}))?"
_SEND = re.compile(rf"^send{_NUMBER}$", re.IGNORECASE)
_CANCEL = re.compile(rf"^cancel{_NUMBER}$", re.IGNORECASE)
_FILE_WORD = r"(?:attachments?|files?|pdfs?|documents?|docs?|photos?|pictures?|images?)"
_AS_NAME = r"(?:\s+(?:as|under|called|named)\b[\s:]*(?P<name>.*))?"
_FROM = r"\s+(?:from|in|of|on)\s+(?:e-?mail\s+|number\s+|no\.?\s*|#)?(?P<number>\d{1,2})"
# "save the attachment from 2 as my lease"
_ATTACHMENT = re.compile(
    rf"^(?:save|keep|store|file)\s+(?:the\s+|an\s+|my\s+)?{_FILE_WORD}{_FROM}{_AS_NAME}$",
    re.IGNORECASE | re.DOTALL,
)
# "save lease-scan.pdf from 2 as my lease": the attachment is named.
_NAMED_ATTACHMENT = re.compile(
    rf"^(?:save|keep|store|file)\s+[\"'“]?(?P<which>[^\"'”\n]{{1,120}}?\.[A-Za-z0-9]{{1,8}})[\"'”]?{_FROM}{_AS_NAME}$",
    re.IGNORECASE | re.DOTALL,
)
# The colon is what makes it a command: "change my flight" is an ordinary sentence.
_CHANGE = re.compile(
    r"^change(?:\s+(?:draft\s+)?#?(\d{1,3}))?\s*:\s*(.*)$", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True)
class MailRequest:
    # connect, disconnect, check, news, digest, draft, send, change, cancel or attachment
    kind: str
    number: int | None = None
    text: str = ""
    reply_all: bool = False
    # For "attachment": which file the user named, when they named one.
    which: str = ""
    # False for a bare "what's new?", which is only about mail once Gmail is connected.
    certain: bool = True


def _number(text: str | None) -> int | None:
    return int(text) if text else None


def parse_mail(said: str) -> MailRequest | None:
    request = _LEAD.sub("", said.strip(), count=1)
    whole = request.strip().rstrip(".!?").strip()

    if match := _CONNECT.match(whole):
        return MailRequest("disconnect" if match.group(1) else "connect")
    if _CHECK.match(whole):
        return MailRequest("check")
    if _NEWS_MAIL.match(whole):
        return MailRequest("news")
    if _NEWS_PLAIN.match(whole):
        return MailRequest("news", certain=False)
    if match := _SEND.match(whole):
        return MailRequest("send", number=_number(match.group(1)))
    if match := _CANCEL.match(whole):
        return MailRequest("cancel", number=_number(match.group(1)))
    if match := _CHANGE.match(request):
        return MailRequest("change", number=_number(match.group(1)), text=match.group(2).strip())
    if match := _ATTACHMENT.match(whole) or _NAMED_ATTACHMENT.match(whole):
        named = match.groupdict().get("which") or ""
        name = (match.group("name") or "").strip().strip("\"'“”‘’").strip()
        return MailRequest(
            "attachment", number=int(match.group("number")), text=name, which=named.strip()
        )
    if match := _DIGEST.match(request):
        return MailRequest("digest", text=match.group(1).strip())
    if match := _DRAFT.match(request):
        return MailRequest(
            "draft",
            number=int(match.group("number")),
            text=match.group("wish").strip(),
            reply_all=bool(match.group("all")),
        )
    return None
