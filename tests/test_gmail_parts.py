"""Milestone 5: the pieces under the mailroom (E1, E2, E4, E6, E7).

The Gmail client, Google sign-in, message reading and writing, token encryption, the
command recogniser, and the web pages for signing in.
"""

import asyncio
import base64
import email
import json
import logging
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tanios.config import Settings
from tanios.crypto import SealError, TokenBox
from tanios.gmail.client import (
    GmailApi,
    GmailAuthError,
    GmailError,
    GmailNotFound,
    allowed,
)
from tanios.gmail.message import (
    addresses,
    build_reply,
    html_to_text,
    is_address,
    parse_message,
    recipients_of,
    reply_subject,
)
from tanios.gmail.oauth import GMAIL_SCOPE, GoogleOAuth
from tanios.mailtalk import MailRequest, parse_mail
from tanios.main import NoQueryStrings, _mail_clock, _open_mailroom, create_app

API = "https://gmail.example"


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# The Gmail client ---------------------------------------------------------------------


class Google:
    """Records what the client asks for and answers from a script."""

    def __init__(self, answers=None) -> None:
        self.requests: list[httpx2.Request] = []
        self.answers = answers or {}
        self.tokens_asked: list[bool] = []
        self.token_number = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        answer = self.answers.get(key, httpx2.Response(200, json={}))
        return answer(request) if callable(answer) else answer

    async def access_token(self, fresh: bool = False) -> str:
        self.tokens_asked.append(fresh)
        if fresh:
            self.token_number += 1
        return f"token-{self.token_number}"

    @property
    def client(self) -> GmailApi:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))
        return GmailApi(http, api_base=API, access_token=self.access_token)

    @property
    def seen(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


ME = "/gmail/v1/users/me"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/messages/abc"),
        ("DELETE", "/drafts/abc"),
        ("DELETE", "/threads/abc"),
        ("DELETE", "/labels/Label_1"),
        ("POST", "/messages/abc/trash"),
        ("POST", "/threads/abc/trash"),
        ("POST", "/threads/abc/modify"),
        ("POST", "/messages/batchModify"),
        ("POST", "/messages/batchDelete"),
        ("POST", "/messages/send"),
        ("POST", "/messages/import"),
        ("POST", "/settings/filters"),
        ("POST", "/settings/forwardingAddresses"),
        ("PUT", "/settings/autoForwarding"),
        ("PUT", "/settings/vacation"),
        ("POST", "/settings/delegates"),
        ("PATCH", "/labels/Label_1"),
        ("GET", "/settings/filters"),
        ("POST", "/messages/abc/modify/../../settings/filters"),
    ],
)
def test_e2_gmail_calls_that_delete_archive_forward_or_change_settings_are_refused(method, path):
    assert allowed(method, path) is False
    google = Google()

    with pytest.raises(GmailError, match="not allowed"):
        asyncio.run(google.client._call(method, path, json={}))

    assert google.requests == []  # refused before anything is sent


def test_e2_the_only_calls_tanios_can_make():
    for method, path in [
        ("GET", "/profile"),
        ("GET", "/messages"),
        ("GET", "/messages/18c2a"),
        ("GET", "/threads/18c2a"),
        ("GET", "/labels"),
        ("POST", "/labels"),
        ("POST", "/messages/18c2a/modify"),
        ("POST", "/drafts"),
        ("GET", "/drafts/r-123"),
        ("PUT", "/drafts/r-123"),
        ("POST", "/drafts/send"),
    ]:
        assert allowed(method, path) is True


def test_e2_labelling_only_ever_adds_a_label_so_mail_is_never_archived():
    google = Google()

    asyncio.run(google.client.add_label("18c2a", "Label_7"))

    [request] = google.requests
    assert f"{request.method} {request.url.path}" == f"POST {ME}/messages/18c2a/modify"
    assert json.loads(request.content) == {"addLabelIds": ["Label_7"]}

    for body in (
        {"removeLabelIds": ["INBOX"]},
        {"addLabelIds": ["x"], "removeLabelIds": ["UNREAD"]},
    ):
        with pytest.raises(GmailError, match="only adding labels"):
            asyncio.run(google.client._call("POST", "/messages/18c2a/modify", json=body))
    assert len(google.requests) == 1


def test_an_id_that_is_not_plain_letters_and_digits_never_reaches_a_web_address():
    google = Google()

    for bad in ("../settings/filters", "abc/trash", "", "a b"):
        with pytest.raises(GmailError):
            asyncio.run(google.client.add_label(bad, "Label_1"))
        with pytest.raises(GmailError):
            asyncio.run(google.client.send_draft(bad))
    assert google.requests == []


def test_e2_new_mail_is_looked_for_in_the_inbox_after_a_given_moment():
    google = Google(
        {
            f"GET {ME}/messages": httpx2.Response(
                200, json={"messages": [{"id": "b2"}, {"id": "a1"}]}
            )
        }
    )
    after = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

    ids = asyncio.run(google.client.inbox_ids(after=after, limit=100))

    assert ids == ["b2", "a1"]
    [request] = google.requests
    assert request.url.params["q"] == f"in:inbox after:{int(after.timestamp())}"
    assert request.url.params["maxResults"] == "100"
    assert request.headers["Authorization"] == "Bearer token-0"


def test_e2_missing_labels_are_created_and_existing_ones_reused():
    def create(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, json={"id": "Label_9", "name": json.loads(request.content)["name"]}
        )

    google = Google(
        {
            f"GET {ME}/labels": httpx2.Response(
                200,
                json={
                    "labels": [
                        {"id": "Label_1", "name": "Tanios"},
                        {"id": "INBOX", "name": "INBOX"},
                    ]
                },
            ),
            f"POST {ME}/labels": create,
        }
    )

    ids = asyncio.run(google.client.ensure_labels(["Tanios", "Tanios/FYI"]))

    assert ids == {"Tanios": "Label_1", "Tanios/FYI": "Label_9"}
    assert google.seen == [f"GET {ME}/labels", f"POST {ME}/labels"]
    assert json.loads(google.requests[1].content)["name"] == "Tanios/FYI"


def test_e1_a_refused_access_token_is_renewed_once_and_the_call_repeated():
    answers = iter(
        [
            httpx2.Response(401, json={}),
            httpx2.Response(200, json={"emailAddress": "Me@Example.com"}),
        ]
    )
    google = Google({f"GET {ME}/profile": lambda request: next(answers)})

    address = asyncio.run(google.client.address())

    assert address == "me@example.com"
    assert google.tokens_asked == [False, True]
    assert [r.headers["Authorization"] for r in google.requests] == [
        "Bearer token-0",
        "Bearer token-1",
    ]


def test_e1_if_google_still_refuses_after_renewing_access_is_treated_as_gone():
    google = Google({f"GET {ME}/profile": httpx2.Response(401, json={})})

    with pytest.raises(GmailAuthError):
        asyncio.run(google.client.address())

    assert len(google.requests) == 2  # one renewal, never a loop


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx2.Response(404, json={}), GmailNotFound),
        (
            httpx2.Response(
                403,
                json={
                    "error": {"status": "PERMISSION_DENIED", "message": "Insufficient Permission"}
                },
            ),
            GmailAuthError,
        ),
        (httpx2.Response(403, json={"error": {"message": "Rate Limit Exceeded"}}), GmailError),
        (httpx2.Response(429, json={}), GmailError),
        (httpx2.Response(500, text="oops"), GmailError),
        (httpx2.Response(200, text="not json"), GmailError),
        (httpx2.Response(200, json=["a list"]), GmailError),
    ],
)
def test_gmail_failures_become_plain_errors_without_mail_content(response, error):
    google = Google({f"GET {ME}/drafts/r-1": response})

    with pytest.raises(error) as caught:
        asyncio.run(google.client.draft_recipients("r-1"))

    assert type(caught.value) is error
    assert "token" not in str(caught.value).lower()


def test_a_network_failure_is_a_gmail_error_that_names_only_its_kind():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("could not connect to gmail.example with Bearer token-0")

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    client = GmailApi(http, api_base=API, access_token=Google().access_token)

    with pytest.raises(GmailError) as caught:
        asyncio.run(client.address())

    assert str(caught.value) == "ConnectError"


def test_e4_a_draft_is_created_in_its_thread_and_sent_by_its_id():
    google = Google(
        {
            f"POST {ME}/drafts": httpx2.Response(200, json={"id": "r-77", "message": {"id": "m9"}}),
            f"POST {ME}/drafts/send": httpx2.Response(200, json={"id": "m10"}),
        }
    )

    async def scenario():
        client = google.client
        draft_id = await client.create_draft(thread_id="t-1", raw="UkFX")
        await client.update_draft(draft_id, thread_id="t-1", raw="TkVX")
        await client.send_draft(draft_id)
        return draft_id

    assert asyncio.run(scenario()) == "r-77"
    assert google.seen == [f"POST {ME}/drafts", f"PUT {ME}/drafts/r-77", f"POST {ME}/drafts/send"]
    assert json.loads(google.requests[0].content) == {"message": {"raw": "UkFX", "threadId": "t-1"}}
    assert json.loads(google.requests[1].content) == {"message": {"raw": "TkVX", "threadId": "t-1"}}
    assert json.loads(google.requests[2].content) == {"id": "r-77"}


def test_e6_the_recipients_of_a_draft_are_read_back_from_gmail_including_bcc():
    draft = {
        "id": "r-1",
        "message": {
            "payload": {
                "headers": [
                    {"name": "To", "value": "Rana <rana@example.com>, sami@example.com"},
                    {"name": "Cc", "value": "lina@example.com"},
                    {"name": "Bcc", "value": "hidden@example.com"},
                    {"name": "Subject", "value": "Re: x"},
                ]
            }
        },
    }
    google = Google({f"GET {ME}/drafts/r-1": httpx2.Response(200, json=draft)})

    to, others = asyncio.run(google.client.draft_recipients("r-1"))

    assert to == ("rana@example.com", "sami@example.com")
    assert others == ("lina@example.com", "hidden@example.com")
    assert google.requests[0].url.params["format"] == "metadata"


def test_a_thread_is_read_without_the_drafts_sitting_in_it():
    def message(gmail_id: str, labels: list[str]) -> dict:
        return {
            "id": gmail_id,
            "threadId": "t-1",
            "labelIds": labels,
            "internalDate": "1794744000000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "From", "value": "rana@example.com"}],
                "body": {"data": b64(f"text of {gmail_id}")},
            },
        }

    thread = {
        "messages": [message("m1", ["INBOX"]), message("d1", ["DRAFT"]), message("m2", ["SENT"])]
    }
    google = Google({f"GET {ME}/threads/t-1": httpx2.Response(200, json=thread)})

    emails = asyncio.run(google.client.thread("t-1"))

    assert [item.id for item in emails] == ["m1", "m2"]


# Reading and writing messages -----------------------------------------------------------


def resource(payload: dict, **extra) -> dict:
    return {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1794744000000",
        "payload": payload,
        **extra,
    }


def test_a_plain_email_is_read_into_its_parts():
    parsed = parse_message(
        resource(
            {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": '"Rana Haddad" <Rana@Example.com>'},
                    {"name": "To", "value": "owner@example.com, Sami <sami@example.com>"},
                    {"name": "Cc", "value": "lina@example.com"},
                    {"name": "Reply-To", "value": "accounts@example.com"},
                    {"name": "Subject", "value": "Invoice\r\n for October"},
                    {"name": "Message-ID", "value": "<abc@mail.example.com>"},
                    {"name": "References", "value": "<a@x> <b@x>"},
                ],
                "body": {"data": b64("Can you approve this by Friday?")},
            }
        )
    )

    assert parsed.sender == "Rana Haddad"
    assert parsed.sender_address == "rana@example.com"
    assert parsed.to == ("owner@example.com", "sami@example.com")
    assert parsed.cc == ("lina@example.com",)
    assert parsed.reply_to == "accounts@example.com"
    assert parsed.subject == "Invoice for October"
    assert parsed.message_id == "<abc@mail.example.com>"
    assert parsed.text == "Can you approve this by Friday?"
    assert parsed.received_at == datetime.fromtimestamp(1794744000, tz=UTC)
    assert parsed.people == {
        "rana@example.com",
        "owner@example.com",
        "sami@example.com",
        "lina@example.com",
    }


def test_the_plain_text_part_is_preferred_and_attachments_are_skipped():
    parsed = parse_message(
        resource(
            {
                "mimeType": "multipart/mixed",
                "headers": [{"name": "From", "value": "rana@example.com"}],
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {"mimeType": "text/plain", "body": {"data": b64("Plain words.")}},
                            {"mimeType": "text/html", "body": {"data": b64("<p>Rich words.</p>")}},
                        ],
                    },
                    {
                        "mimeType": "text/plain",
                        "filename": "notes.txt",
                        "body": {"data": b64("attached file text")},
                    },
                ],
            }
        )
    )

    assert parsed.text == "Plain words."
    assert parsed.sender == "rana@example.com"


def test_an_html_only_email_is_read_as_text_without_scripts_or_hidden_parts():
    html = (
        "<html><head><title>t</title><style>p{color:red}</style></head><body>"
        "<p>Hello&nbsp;Mo,</p><script>steal()</script><div>Your order <b>shipped</b>.</div>"
        "</body></html>"
    )
    parsed = parse_message(
        resource({"mimeType": "text/html", "headers": [], "body": {"data": b64(html)}})
    )

    assert parsed.text == "Hello Mo,\nYour order shipped."
    assert "steal" not in parsed.text and "color" not in parsed.text
    assert parsed.subject == "(no subject)"
    assert parsed.sender == "Unknown sender"


def test_an_email_with_no_readable_body_falls_back_to_gmails_snippet():
    parsed = parse_message(
        resource(
            {"mimeType": "text/plain", "headers": [], "body": {"data": "!!!not base64!!!"}},
            snippet="Short preview",
        )
    )

    assert parsed.text == "Short preview"


def test_a_malformed_message_is_refused_not_guessed_at():
    for bad in (None, [], {"id": "m1"}, {"threadId": "t1", "payload": {}}):
        with pytest.raises(ValueError):
            parse_message(bad)


def test_html_to_text_survives_broken_markup():
    assert html_to_text("<p>One<br>Two</p><p>Three") == "One\nTwo\nThree"
    assert html_to_text("") == ""


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("rana@example.com", True),
        ("first.last+tag@sub.example.co", True),
        ("no-at-sign.example.com", False),
        ("two@@example.com", False),
        ("spaces in@example.com", False),
        ("x@localhost", False),
        ("x@example.com\r\nBcc: thief@evil.example", False),
        ("<x@example.com>", False),
        ("x@example.com, y@example.com", False),
    ],
)
def test_e6_only_a_plain_single_address_counts_as_a_recipient(text, ok):
    assert is_address(text) is ok


def test_addresses_are_read_from_a_header_lower_cased_and_once_each():
    header = 'Rana <Rana@Example.com>, "Doe, Sami" <sami@example.com>, rana@example.com, broken'

    assert addresses(header) == ("rana@example.com", "sami@example.com")


def test_e4_a_reply_is_a_whole_email_threaded_onto_the_one_it_answers():
    raw = build_reply(
        to=["rana@example.com"],
        cc=["sami@example.com"],
        subject="Re: Invoice for October",
        body="Yes, Thursday works.\n\nMo",
        in_reply_to="<abc@mail.example.com>",
        references="<a@x>",
    )

    message = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert message["To"] == "rana@example.com"
    assert message["Cc"] == "sami@example.com"
    assert message["Subject"] == "Re: Invoice for October"
    assert message["In-Reply-To"] == "<abc@mail.example.com>"
    assert message["References"] == "<a@x> <abc@mail.example.com>"
    assert message["From"] is None  # Gmail fills in the user's own address
    assert message["Bcc"] is None
    # Email lines end in CRLF.
    assert message.get_payload(decode=True) == b"Yes, Thursday works.\r\n\r\nMo\r\n"


def test_e6_a_reply_cannot_be_built_with_a_recipient_that_is_not_an_address():
    for to, cc in (
        ([], []),
        (["rana@example.com\nBcc: thief@evil.example"], []),
        (["rana@example.com"], ["not an address"]),
    ):
        with pytest.raises(ValueError):
            build_reply(to=to, cc=cc, subject="Re: x", body="hi", in_reply_to="", references="")


def test_e7_a_subject_from_an_email_cannot_add_headers_to_the_reply():
    raw = build_reply(
        to=["rana@example.com"],
        cc=[],
        subject="Re: Hello\r\nBcc: thief@evil.example\r\nX-Injected: yes",
        body="hi",
        in_reply_to="",
        references="",
    )

    message = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert message["Bcc"] is None and message["X-Injected"] is None
    assert message["Subject"] == "Re: Hello Bcc: thief@evil.example X-Injected: yes"


def test_reply_subjects_get_one_re_and_no_more():
    assert reply_subject("Invoice") == "Re: Invoice"
    assert reply_subject("RE: Invoice") == "RE: Invoice"


def test_recipients_of_a_strange_draft_are_empty_not_a_crash():
    assert recipients_of(None) == ((), ())
    assert recipients_of({"message": {"payload": {"headers": "nope"}}}) == ((), ())


# Google sign-in -------------------------------------------------------------------------


def oauth(handler) -> GoogleOAuth:
    return GoogleOAuth(
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        client_id="client-id.apps.example",
        client_secret="client-secret-value",
        redirect_uri="https://tanios.example/oauth/google/callback",
        auth_url="https://accounts.example/o/oauth2/v2/auth",
        token_url="https://oauth.example/token",
        revoke_url="https://oauth.example/revoke",
    )


def form(request: httpx2.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


def test_e1_the_consent_page_is_asked_for_gmail_access_that_lasts_and_nothing_wider():
    url = urlsplit(oauth(lambda request: httpx2.Response(200)).consent_url("state-123"))
    query = {key: values[0] for key, values in parse_qs(url.query).items()}

    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.example/o/oauth2/v2/auth"
    assert query == {
        "client_id": "client-id.apps.example",
        "redirect_uri": "https://tanios.example/oauth/google/callback",
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.modify",
        "access_type": "offline",
        "prompt": "consent",
        "state": "state-123",
    }
    assert "client-secret-value" not in url.query


def test_e1_the_code_from_google_is_exchanged_for_tokens():
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            json={
                "access_token": "ya29.access",
                "expires_in": 3599,
                "refresh_token": "1//refresh",
                "scope": f"openid {GMAIL_SCOPE}",
                "token_type": "Bearer",
            },
        )

    grant = asyncio.run(oauth(handler).exchange("4/code"))

    assert (grant.access_token, grant.expires_in, grant.refresh_token) == (
        "ya29.access",
        3599,
        "1//refresh",
    )
    assert str(requests[0].url) == "https://oauth.example/token"
    assert form(requests[0]) == {
        "grant_type": "authorization_code",
        "code": "4/code",
        "redirect_uri": "https://tanios.example/oauth/google/callback",
        "client_id": "client-id.apps.example",
        "client_secret": "client-secret-value",
    }


def test_e1_sign_in_without_the_gmail_permission_ticked_is_refused():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "access_token": "a",
                "refresh_token": "r",
                "scope": "openid email",
                "expires_in": 3599,
            },
        )

    with pytest.raises(GmailAuthError, match="not granted"):
        asyncio.run(oauth(handler).exchange("4/code"))


def test_e1_sign_in_that_returns_no_lasting_token_is_refused():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"access_token": "a", "scope": GMAIL_SCOPE})

    with pytest.raises(GmailError, match="refresh token"):
        asyncio.run(oauth(handler).exchange("4/code"))


def test_e1_a_renewed_access_token_comes_from_the_stored_one():
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"access_token": "ya29.new", "expires_in": 3599})

    assert asyncio.run(oauth(handler).refresh("1//refresh")) == ("ya29.new", 3599)
    assert form(requests[0])["grant_type"] == "refresh_token"
    assert form(requests[0])["refresh_token"] == "1//refresh"


def test_e1_googles_invalid_grant_means_access_was_revoked_or_expired():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Token has been expired or revoked.",
            },
        )

    with pytest.raises(GmailAuthError):
        asyncio.run(oauth(handler).refresh("1//refresh"))


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(500, text="oops"),
        httpx2.Response(400, json={"error": "invalid_client"}),
        httpx2.Response(200, json={"expires_in": 3599}),
        httpx2.Response(503, json={"error": "invalid_grant?"}),
    ],
)
def test_e1_other_token_failures_do_not_count_as_revoked(response):
    with pytest.raises(GmailError) as caught:
        asyncio.run(oauth(lambda request: response).refresh("1//refresh"))

    assert type(caught.value) is GmailError  # so the connection is kept and tried again
    assert "1//refresh" not in str(caught.value)


def test_e1_disconnecting_tells_google_to_cancel_the_token():
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={})

    asyncio.run(oauth(handler).revoke("1//refresh"))

    assert str(requests[0].url) == "https://oauth.example/revoke"
    assert form(requests[0]) == {"token": "1//refresh"}

    # Google answering "already gone" is fine; Google being down is not.
    asyncio.run(
        oauth(lambda request: httpx2.Response(400, json={"error": "invalid_token"})).revoke("x")
    )
    with pytest.raises(GmailError):
        asyncio.run(oauth(lambda request: httpx2.Response(503)).revoke("x"))


# Encryption of stored tokens --------------------------------------------------------------


def test_e1_a_sealed_token_can_only_be_opened_with_the_same_key():
    box = TokenBox(Fernet.generate_key().decode())

    sealed = box.seal("1//refresh")

    assert "1//refresh" not in sealed
    assert box.open(sealed) == "1//refresh"
    assert box.seal("1//refresh") != sealed  # never the same text twice
    with pytest.raises(SealError):
        TokenBox(Fernet.generate_key().decode()).open(sealed)
    with pytest.raises(SealError):
        box.open("not sealed at all")


def test_a_key_that_is_not_a_real_key_is_refused_at_once():
    for bad in ("", "too-short", "x" * 44):
        with pytest.raises(SealError):
            TokenBox(bad)


# Recognising mail commands ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "request_"),
    [
        ("connect gmail", MailRequest("connect")),
        ("Connect my Gmail.", MailRequest("connect")),
        ("disconnect gmail", MailRequest("disconnect")),
        ("check mail", MailRequest("check")),
        ("Hey Tanios, please check my email", MailRequest("check")),
        ("check my inbox now", MailRequest("check")),
        ("what's new?", MailRequest("news", certain=False)),
        ("Whats new", MailRequest("news", certain=False)),
        ("what's new in my inbox?", MailRequest("news")),
        ("any new mail?", MailRequest("news")),
        ("summarise my email", MailRequest("news")),
        ("mail summary at 08:00 18:00", MailRequest("digest", text="at 08:00 18:00")),
        ("mail summary off", MailRequest("digest", text="off")),
        (
            "Draft a reply to 2 saying yes, Thursday works",
            MailRequest("draft", number=2, text="yes, Thursday works"),
        ),
        ("reply to 3: thanks, got it", MailRequest("draft", number=3, text="thanks, got it")),
        ("draft a reply to #1", MailRequest("draft", number=1)),
        (
            "write a reply all to email 2 saying approved",
            MailRequest("draft", number=2, text="approved", reply_all=True),
        ),
        ("send", MailRequest("send")),
        ("Send.", MailRequest("send")),
        ("send 3", MailRequest("send", number=3)),
        ("send it", MailRequest("send")),
        ("send draft 2", MailRequest("send", number=2)),
        ("cancel", MailRequest("cancel")),
        ("cancel 2", MailRequest("cancel", number=2)),
        ("change: make it shorter", MailRequest("change", text="make it shorter")),
        ("change 2: add a thank you", MailRequest("change", number=2, text="add a thank you")),
    ],
)
def test_mail_commands_are_recognised_by_the_shape_of_the_whole_message(said, request_):
    assert parse_mail(said) == request_


@pytest.mark.parametrize(
    "said",
    [
        "send me a summary of the news",
        "can you send flowers to my mother?",
        "send 3 reasons to learn Arabic",
        "sending love",
        "cancel my gym membership",
        "change my flight to Tuesday",
        "change is hard",
        "what's new in physics?",
        "check the weather",
        "how do I connect gmail to outlook?",
        "reply to my question please",
        "what is the capital of Jordan?",
        "",
    ],
)
def test_e5_an_ordinary_sentence_is_never_taken_for_a_mail_command(said):
    assert parse_mail(said) is None


# The sign-in pages ------------------------------------------------------------------------


class Desk:
    def __init__(self) -> None:
        self.finished: list[tuple[str, str]] = []
        self.ticks = 0

    async def consent_url(self, token: str) -> str | None:
        return "https://accounts.example/auth?state=live" if token == "live" else None

    async def finish_connection(self, state: str, code: str) -> str | None:
        self.finished.append((state, code))
        return "<b>owner</b>@example.com" if code == "good" else None

    async def run_due(self) -> None:
        self.ticks += 1


def app_with(desk):
    return TestClient(create_app(Settings(_env_file=None), mailroom=desk), follow_redirects=False)


def test_e1_a_live_link_sends_the_browser_to_googles_sign_in_page():
    with app_with(Desk()) as client:
        response = client.get("/connect/gmail", params={"t": "live"})

    assert response.status_code == 303
    assert response.headers["location"] == "https://accounts.example/auth?state=live"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


def test_e1_a_dead_or_missing_link_gets_a_plain_page_and_no_redirect():
    with app_with(Desk()) as client:
        for params in ({"t": "used-up"}, {}):
            response = client.get("/connect/gmail", params=params)
            assert response.status_code == 404
            assert "This link no longer works" in response.text
            assert "location" not in response.headers


def test_e1_googles_reply_finishes_the_connection_and_shows_a_page():
    desk = Desk()
    with app_with(desk) as client:
        response = client.get("/oauth/google/callback", params={"state": "live", "code": "good"})

    assert response.status_code == 200
    assert "Gmail is connected" in response.text
    # Text from outside is shown as text, never as markup.
    assert "&lt;b&gt;owner&lt;/b&gt;@example.com" in response.text
    assert desk.finished == [("live", "good")]


def test_e1_a_refusal_or_a_bad_reply_from_google_connects_nothing():
    desk = Desk()
    with app_with(desk) as client:
        refused = client.get(
            "/oauth/google/callback", params={"state": "live", "error": "access_denied"}
        )
        both = client.get(
            "/oauth/google/callback", params={"state": "live", "code": "good", "error": "x"}
        )
        no_state = client.get("/oauth/google/callback", params={"code": "good"})
        bad_code = client.get("/oauth/google/callback", params={"state": "live", "code": "bad"})

    assert [r.status_code for r in (refused, both, no_state, bad_code)] == [400] * 4
    assert "Gmail was not connected" in refused.text
    assert desk.finished == [("live", "bad")]


def test_with_email_off_the_sign_in_pages_do_nothing():
    with app_with(None) as client:
        assert client.get("/connect/gmail", params={"t": "live"}).status_code == 404
        assert (
            client.get("/oauth/google/callback", params={"state": "s", "code": "c"}).status_code
            == 400
        )


def test_e1_one_time_values_in_web_addresses_are_kept_out_of_the_access_log():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:5000",
            "GET",
            "/oauth/google/callback?state=SECRET-STATE&code=4/SECRET",
            "1.1",
            200,
        ),
        None,
    )

    assert NoQueryStrings().filter(record) is True

    line = record.getMessage()
    assert line == '127.0.0.1:5000 - "GET /oauth/google/callback?[hidden] HTTP/1.1" 200'
    assert "SECRET" not in line


def test_the_access_log_filter_leaves_other_lines_alone():
    plain = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:1", "GET", "/healthz", "1.1", 200),
        None,
    )
    odd = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "started", None, None)

    assert NoQueryStrings().filter(plain) and NoQueryStrings().filter(odd)
    assert plain.getMessage() == '1.2.3.4:1 - "GET /healthz HTTP/1.1" 200'


def test_e2_the_servers_clock_keeps_calling_the_mailroom(monkeypatch):
    monkeypatch.setattr("tanios.main.MAIL_TICK_SECONDS", 0.01)
    desk = Desk()

    async def scenario():
        clock = asyncio.create_task(_mail_clock(desk))
        await asyncio.sleep(0.06)
        clock.cancel()

    asyncio.run(scenario())

    assert desk.ticks >= 3


# Switching email on -----------------------------------------------------------------------


def gmail_settings(**overrides) -> Settings:
    values = {
        "domain": "tanios.example",
        "google_client_id": "client-id.apps.example",
        "google_client_secret": "client-secret-value",
        "encryption_key": Fernet.generate_key().decode(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_email_needs_four_settings_and_reports_only_their_names():
    assert gmail_settings().missing_for_gmail() == []
    assert Settings(_env_file=None).missing_for_gmail() == [
        "TANIOS_DOMAIN",
        "TANIOS_GOOGLE_CLIENT_ID",
        "TANIOS_GOOGLE_CLIENT_SECRET",
        "TANIOS_ENCRYPTION_KEY",
    ]
    assert gmail_settings(encryption_key=None).missing_for_gmail() == ["TANIOS_ENCRYPTION_KEY"]


def test_sign_in_links_use_https_on_the_servers_own_domain():
    assert gmail_settings().base_url() == "https://tanios.example"
    assert gmail_settings(public_url="http://127.0.0.1:8000/").base_url() == "http://127.0.0.1:8000"
    assert Settings(_env_file=None).base_url() is None


def test_email_stays_off_with_missing_settings_or_a_bad_key(caplog):
    from tests.fakes import FakeGateway, InMemoryStore, make_router

    store = InMemoryStore()
    router = make_router(store, FakeGateway())

    class NoDatabase:
        sessions = None

    def open_with(settings):
        return _open_mailroom(settings, NoDatabase(), None, store, router, None, None)

    with caplog.at_level("WARNING"):
        assert open_with(Settings(_env_file=None)) is None
        assert open_with(gmail_settings(encryption_key="not-a-key")) is None
        assert open_with(gmail_settings()) is not None

    assert "email is off. Still to set: TANIOS_DOMAIN" in caplog.text
    assert "TANIOS_ENCRYPTION_KEY is not a valid key" in caplog.text
    assert "client-secret-value" not in caplog.text and "not-a-key" not in caplog.text
