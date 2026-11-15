"""Milestone 1: signature check, webhook parsing and the two outgoing HTTP clients."""

import asyncio
import json

import httpx2
import pytest

from tanios.llm import ChatCompletionsClient, ModelError
from tanios.whatsapp.client import CloudApiClient, WhatsAppSendError, split_text
from tanios.whatsapp.parsing import parse_webhook
from tanios.whatsapp.signature import is_valid_signature, sign
from tests.fakes import OWNER, webhook_payload

# Signature ----------------------------------------------------------------------------


def test_signature_matches_a_known_hmac_sha256_value():
    # HMAC-SHA256("secret", "hello"), computed independently.
    known = "sha256=88aab3ede8d3adf94d26ab90d3bafd4a2083070c3bcce9c014ee04a443847c0b"

    assert sign(b"hello", "secret") == known
    assert is_valid_signature(b"hello", known, "secret")


@pytest.mark.parametrize(
    "header",
    [None, "", "sha256=", "sha1=88aab3ede8d3adf94d26ab90d3bafd4a2083070c3bcce9c014ee04a443847c0b"],
)
def test_missing_or_malformed_signatures_are_invalid(header):
    assert not is_valid_signature(b"hello", header, "secret")


def test_a_signature_from_another_secret_is_invalid():
    assert not is_valid_signature(b"hello", sign(b"hello", "other"), "secret")


# Parsing ------------------------------------------------------------------------------


def test_parses_a_text_message_from_metas_payload():
    [message] = parse_webhook(webhook_payload("Hi!", message_id="wamid.7", wa_user_id="JO.1"))

    assert message.id == "wamid.7"
    assert message.kind == "text"
    assert message.text == "Hi!"
    assert message.phone == OWNER
    assert message.wa_user_id == "JO.1"
    assert message.phone_number_id == "1234567890"


def test_parses_a_message_whose_phone_number_meta_left_out():
    [message] = parse_webhook(webhook_payload("Hi!", sender=None, wa_user_id="JO.1"))

    assert message.phone is None
    assert message.sender_key == "JO.1"


def test_a_voice_note_is_parsed_as_audio_with_no_text():
    [message] = parse_webhook(webhook_payload(kind="audio"))

    assert message.kind == "audio"
    assert message.text is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "text",
        {},
        {"object": "page", "entry": []},
        {"object": "whatsapp_business_account"},
        {"object": "whatsapp_business_account", "entry": "nope"},
        {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages"}]}]},
        {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages", "value": {"messages": [{"id": 5}]}}]}],
        },
    ],
)
def test_odd_payloads_yield_no_messages_and_no_errors(payload):
    assert parse_webhook(payload) == []


# Sending WhatsApp messages ------------------------------------------------------------


def whatsapp_client(handler) -> CloudApiClient:
    return CloudApiClient(
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        access_token="token-abc",
        phone_number_id="1234567890",
        api_base="https://graph.example",
        graph_version="v26.0",
    )


def test_send_text_posts_metas_documented_request():
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"messages": [{"id": "wamid.out"}]})

    asyncio.run(whatsapp_client(handler).send_text(phone=OWNER, wa_user_id=None, body="Hello"))

    [request] = requests
    assert str(request.url) == "https://graph.example/v26.0/1234567890/messages"
    assert request.headers["Authorization"] == "Bearer token-abc"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": OWNER,
        "type": "text",
        "text": {"preview_url": False, "body": "Hello"},
    }


def test_send_text_uses_the_whatsapp_user_id_when_there_is_no_phone_number():
    bodies: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={})

    asyncio.run(whatsapp_client(handler).send_text(phone=None, wa_user_id="JO.1", body="Hello"))

    assert bodies[0]["recipient"] == "JO.1"
    assert "to" not in bodies[0]


def test_a_refused_send_raises_without_leaking_the_token_or_the_text():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": {"code": 190, "type": "OAuthException"}})

    with pytest.raises(WhatsAppSendError) as raised:
        asyncio.run(
            whatsapp_client(handler).send_text(phone=OWNER, wa_user_id=None, body="private words")
        )

    assert "401" in str(raised.value)
    assert "token-abc" not in str(raised.value)
    assert "private words" not in str(raised.value)


def test_long_replies_are_split_into_parts_whatsapp_accepts():
    long_text = "\n\n".join(f"Paragraph {number}. " + "word " * 150 for number in range(12))

    parts = split_text(long_text)

    assert len(parts) > 1
    assert all(len(part) <= 4000 for part in parts)
    assert "".join(parts).replace(" ", "").replace("\n", "") == long_text.strip().replace(
        " ", ""
    ).replace("\n", "")


def test_a_short_reply_is_one_part():
    assert split_text("Hello") == ["Hello"]


# Asking the model ---------------------------------------------------------------------


def model_client(handler) -> ChatCompletionsClient:
    return ChatCompletionsClient(
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        api_key="sk-or-test",
        api_base="https://models.example/api/v1",
        model="vendor/small-model",
        max_tokens=700,
    )


def test_complete_posts_a_chat_completions_request_and_reads_the_answer_and_cost():
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            json={
                "model": "vendor/small-model",
                "choices": [{"message": {"role": "assistant", "content": " Paris. "}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.00014},
            },
        )

    reply = asyncio.run(model_client(handler).complete(system="Be brief.", user="Capital?"))

    [request] = requests
    assert str(request.url) == "https://models.example/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-or-test"
    assert json.loads(request.content) == {
        "model": "vendor/small-model",
        "max_tokens": 700,
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Capital?"},
        ],
    }
    assert reply.text == "Paris."
    assert reply.model == "vendor/small-model"
    assert reply.cost_usd == 0.00014


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(500, text="upstream exploded"),
        httpx2.Response(402, json={"error": {"message": "no credits"}}),
        httpx2.Response(200, text="not json"),
        httpx2.Response(200, json={"choices": []}),
        httpx2.Response(200, json={"choices": [{"message": {"content": "   "}}]}),
    ],
)
def test_every_kind_of_bad_model_response_becomes_a_model_error(response):
    with pytest.raises(ModelError):
        asyncio.run(model_client(lambda request: response).complete(system="s", user="u"))


def test_a_network_failure_becomes_a_model_error_without_the_key():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("could not reach https://models.example with sk-or-test")

    with pytest.raises(ModelError) as raised:
        asyncio.run(model_client(handler).complete(system="s", user="private question"))

    assert "sk-or-test" not in str(raised.value)
    assert "private question" not in str(raised.value)


def test_the_model_call_log_has_model_and_cost_but_no_text(caplog):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": "secret answer"}}], "usage": {"cost": 0.5}},
        )

    with caplog.at_level("INFO"):
        asyncio.run(model_client(handler).complete(system="s", user="secret question"))

    assert "vendor/small-model" in caplog.text
    assert "0.5" in caplog.text
    assert "secret" not in caplog.text
