"""Milestone 1: the address Meta calls, and who is allowed through it."""

import json

from fastapi.testclient import TestClient

from tanios.config import Settings
from tanios.main import create_app
from tanios.whatsapp.signature import sign
from tests.fakes import RecordingHandler, webhook_payload

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


def configured(**overrides) -> Settings:
    values = {
        "whatsapp_verify_token": VERIFY_TOKEN,
        "whatsapp_app_secret": APP_SECRET,
        "whatsapp_phone_number_id": "1234567890",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def post_signed(client: TestClient, payload: dict, secret: str = APP_SECRET):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": sign(body, secret), "Content-Type": "application/json"},
    )


# Meta's one-time address check --------------------------------------------------------


def test_meta_can_verify_the_address_with_the_right_token():
    with TestClient(create_app(configured(), handler=RecordingHandler())) as client:
        response = client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "1158201444",
            },
        )

    assert response.status_code == 200
    assert response.text == "1158201444"


def test_verification_fails_with_the_wrong_token():
    with TestClient(create_app(configured(), handler=RecordingHandler())) as client:
        response = client.get(
            "/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "guess", "hub.challenge": "1"},
        )

    assert response.status_code == 403


def test_a_blank_verify_token_setting_never_matches_a_blank_request():
    settings = configured(whatsapp_verify_token="")
    with TestClient(create_app(settings, handler=RecordingHandler())) as client:
        response = client.get(
            "/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"},
        )

    assert response.status_code == 503


# Incoming messages --------------------------------------------------------------------


def test_a_correctly_signed_message_is_accepted_and_handed_on():
    handler = RecordingHandler()
    with TestClient(create_app(configured(), handler=handler)) as client:
        response = post_signed(client, webhook_payload("Hi!", message_id="wamid.X"))

    assert response.status_code == 200
    assert [message.id for message in handler.messages] == ["wamid.X"]
    assert handler.messages[0].text == "Hi!"


def test_a_message_with_a_bad_signature_is_refused_and_never_handled():
    handler = RecordingHandler()
    with TestClient(create_app(configured(), handler=handler)) as client:
        response = post_signed(client, webhook_payload("Hi!"), secret="someone-elses-secret")

    assert response.status_code == 401
    assert handler.messages == []


def test_a_message_with_no_signature_is_refused():
    handler = RecordingHandler()
    with TestClient(create_app(configured(), handler=handler)) as client:
        response = client.post("/webhooks/whatsapp", json=webhook_payload("Hi!"))

    assert response.status_code == 401
    assert handler.messages == []


def test_a_signature_made_for_different_content_is_refused():
    handler = RecordingHandler()
    signed_for = json.dumps(webhook_payload("harmless")).encode()
    with TestClient(create_app(configured(), handler=handler)) as client:
        response = client.post(
            "/webhooks/whatsapp",
            content=json.dumps(webhook_payload("something else")).encode(),
            headers={"X-Hub-Signature-256": sign(signed_for, APP_SECRET)},
        )

    assert response.status_code == 401
    assert handler.messages == []


def test_delivery_receipts_are_accepted_and_ignored():
    handler = RecordingHandler()
    receipt = webhook_payload()
    value = receipt["entry"][0]["changes"][0]["value"]
    del value["messages"]
    value["statuses"] = [{"id": "wamid.1", "status": "delivered"}]

    with TestClient(create_app(configured(), handler=handler)) as client:
        response = post_signed(client, receipt)

    assert response.status_code == 200
    assert handler.messages == []


def test_a_message_sent_to_a_different_business_number_is_ignored():
    handler = RecordingHandler()
    with TestClient(create_app(configured(), handler=handler)) as client:
        response = post_signed(client, webhook_payload(phone_number_id="9999999999"))

    assert response.status_code == 200
    assert handler.messages == []


def test_signed_but_unreadable_content_is_rejected_cleanly():
    body = b"not json at all"
    with TestClient(create_app(configured(), handler=RecordingHandler())) as client:
        response = client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": sign(body, APP_SECRET)},
        )

    assert response.status_code == 400


# Not set up yet -----------------------------------------------------------------------


def test_the_webhook_is_closed_until_whatsapp_is_configured():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        assert client.get("/webhooks/whatsapp").status_code == 503
        assert client.post("/webhooks/whatsapp", json=webhook_payload()).status_code == 503


def test_startup_names_the_missing_settings_without_their_values(caplog):
    settings = Settings(_env_file=None, whatsapp_app_secret="super-secret-value")

    with caplog.at_level("WARNING"), TestClient(create_app(settings)):
        pass

    assert "TANIOS_WHATSAPP_ACCESS_TOKEN" in caplog.text
    assert "TANIOS_WHATSAPP_APP_SECRET" not in caplog.text  # that one is set
    assert "super-secret-value" not in caplog.text
