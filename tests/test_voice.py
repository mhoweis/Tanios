"""Milestone 3: voice notes in, voice notes out (W2)."""

import asyncio
import json
import shutil
import subprocess
from dataclasses import replace
from decimal import Decimal

import httpx2
import pytest

from tanios import assistant as replies
from tanios.assistant import Assistant, needs_text_copy, parse_command, spoken_part
from tanios.audio import AudioError, FfmpegConverter, wav_seconds
from tanios.catalog import CatalogError, Provider, parse_catalog
from tanios.llm import ModelError
from tanios.router import BudgetExceeded, NoModelAvailable
from tanios.speech import AudioApiGateway
from tanios.whatsapp.client import CloudApiClient, WhatsAppSendError
from tests.fakes import (
    OWNER,
    STRANGER,
    TEST_CATALOG,
    FakeConverter,
    FakeGateway,
    FakeSpeech,
    FakeWhatsApp,
    InMemoryStore,
    make_router,
    text_message,
    voice_message,
)


class World:
    """One assistant with voice switched on, the owner registered, one voice note waiting."""

    def __init__(
        self,
        *,
        answer: str = "The capital of Jordan is Amman.",
        speech: FakeSpeech | None = None,
        converter: FakeConverter | None = None,
        cap: str | None = None,
        ack_after_seconds: float = 8.0,
        gateway: FakeGateway | None = None,
        **user,
    ) -> None:
        self.store = InMemoryStore()
        self.owner_id = self.store.add_user(OWNER, monthly_cap_usd=cap, **user)
        self.whatsapp = FakeWhatsApp()
        self.whatsapp.media["MEDIA1"] = b"recording"
        self.model = gateway or FakeGateway(answer=answer)
        self.speech = speech or FakeSpeech()
        self.router = make_router(self.store, self.model, speech=self.speech)
        self.assistant = Assistant(
            self.store,
            self.whatsapp,
            self.router,
            audio=converter or FakeConverter(),
            ack_after_seconds=ack_after_seconds,
        )

    def receive(self, *messages) -> None:
        async def scenario() -> None:
            for message in messages:
                await self.assistant.handle(message)

        asyncio.run(scenario())

    @property
    def ledger(self) -> list[tuple[str, str, str, str]]:
        return [(e.task, e.tier, e.model, e.outcome) for e in self.store.ledger]


# W2: understand a voice note and reply with a voice note ------------------------------


def test_w2_a_voice_note_is_answered_with_a_voice_note():
    world = World()

    world.receive(voice_message())

    assert world.whatsapp.downloads == ["MEDIA1"]
    assert world.model.prompts == ["what is the capital of Jordan"]
    assert world.whatsapp.voices == [(OWNER, b"OGG:mp3:The capital of Jordan is Amman.")]
    assert world.whatsapp.bodies == []  # nothing to copy, so no text


def test_w2_the_recording_is_converted_before_it_is_transcribed():
    world = World()

    world.receive(voice_message())

    [heard] = world.speech.transcriptions
    assert heard["audio"].startswith(b"WAV:recording")
    assert heard["format"] == "wav"
    assert heard["language"] == "en"
    assert heard["model"] == "listen-model"


def test_w2_the_reply_uses_the_voice_named_in_the_models_file():
    world = World()

    world.receive(voice_message())

    [spoken] = world.speech.spoken
    assert spoken["voice"] == "alloy"
    assert spoken["model"] == "voice-model"
    assert spoken["text"] == "The capital of Jordan is Amman."


def test_w2_the_model_is_told_its_answer_will_be_read_aloud():
    world = World()

    world.receive(voice_message())

    assert "read aloud" in world.model.calls[0]["system"]


def test_w2_a_typed_question_is_still_answered_in_text_only():
    world = World()

    world.receive(text_message("what is the capital of Jordan"))

    assert world.whatsapp.bodies == ["The capital of Jordan is Amman."]
    assert world.whatsapp.voices == []
    assert world.speech.spoken == []
    assert "read aloud" not in world.model.calls[0]["system"]


@pytest.mark.parametrize(
    "answer",
    [
        "The form is at https://example.com/apply and takes five minutes.",
        "Write to rana@example.com to book it.",
        "The reference number is 4471903.",
        "Here is the draft:\n\nDear Sam,\nThursday works for me.\nBest, M",
    ],
)
def test_w2_anything_to_copy_also_comes_as_text(answer):
    world = World(answer=answer)

    world.receive(voice_message())

    assert len(world.whatsapp.voices) == 1
    assert world.whatsapp.bodies == [answer]


def test_w2_a_long_answer_is_spoken_in_part_and_sent_in_full_as_text():
    long_answer = " ".join(f"Sentence number {n} of a long answer." for n in range(1, 80))
    world = World(answer=long_answer)

    world.receive(voice_message())

    [spoken] = world.speech.spoken
    assert len(spoken["text"]) <= replies.MAX_SPOKEN_CHARS
    assert spoken["text"].endswith(".")
    assert long_answer.startswith(spoken["text"])
    assert world.whatsapp.bodies == [long_answer]


def test_w2_a_spoken_command_works_and_is_answered_in_text():
    world = World(speech=FakeSpeech(hears="Status."))

    world.receive(voice_message())

    assert world.whatsapp.bodies[0].startswith(replies.STATUS_ACTIVE)
    assert world.whatsapp.voices == []
    assert world.model.prompts == []


def test_w2_a_slow_voice_note_gets_working_on_it_first():
    world = World(speech=FakeSpeech(delay_seconds=0.2), ack_after_seconds=0.05)

    world.receive(voice_message())

    assert world.whatsapp.bodies == [replies.WORKING_REPLY]
    assert len(world.whatsapp.voices) == 1


def test_w2_the_same_voice_note_delivered_twice_is_handled_once():
    world = World()

    world.receive(voice_message(message_id="wamid.A"), voice_message(message_id="wamid.A"))

    assert len(world.whatsapp.voices) == 1
    assert len(world.speech.transcriptions) == 1


# When part of the chain fails ---------------------------------------------------------


def test_w2_if_the_voice_reply_cannot_be_made_the_answer_still_arrives_as_text():
    world = World(speech=FakeSpeech(speak_fails=True))

    world.receive(voice_message())

    assert world.whatsapp.voices == []
    assert world.whatsapp.bodies == ["The capital of Jordan is Amman."]


def test_w2_if_whatsapp_refuses_the_voice_note_the_answer_still_arrives_as_text():
    world = World()
    world.whatsapp.voice_fails = True

    world.receive(voice_message())

    assert world.whatsapp.bodies == ["The capital of Jordan is Amman."]


def test_w2_a_voice_note_that_cannot_be_transcribed_gets_a_clear_reply():
    world = World(speech=FakeSpeech(transcribe_fails=True))

    world.receive(voice_message())

    assert world.whatsapp.bodies == [replies.VOICE_FAILED_REPLY]
    assert world.model.prompts == []


def test_w2_a_recording_that_cannot_be_downloaded_gets_a_clear_reply():
    world = World()

    world.receive(voice_message(media_id="GONE"))

    assert world.whatsapp.bodies == [replies.VOICE_FAILED_REPLY]
    assert world.speech.transcriptions == []


def test_w2_audio_that_cannot_be_converted_gets_a_clear_reply():
    world = World(converter=FakeConverter(fails=True))

    world.receive(voice_message())

    assert world.whatsapp.bodies == [replies.VOICE_FAILED_REPLY]


def test_w2_a_silent_voice_note_is_not_sent_to_the_model():
    world = World(speech=FakeSpeech(hears="  "))

    world.receive(voice_message())

    assert world.whatsapp.bodies == [replies.VOICE_EMPTY_REPLY]
    assert world.model.prompts == []


def test_w2_without_speech_models_the_user_is_told_to_send_text():
    catalog = parse_catalog(
        {
            "providers": {"openrouter": {"api_base": "https://models.example/api/v1"}},
            "tiers": {"small": [{"provider": "openrouter", "model": "small-model"}]},
            "tasks": {"chat": "small"},
        }
    )
    world = World()
    router = make_router(world.store, world.model, catalog=catalog, speech=world.speech)
    assistant = Assistant(world.store, world.whatsapp, router, audio=FakeConverter())

    asyncio.run(assistant.handle(voice_message()))

    assert world.whatsapp.bodies == [replies.VOICE_OFF_REPLY]
    assert world.whatsapp.downloads == []


# Who may use it, and what it costs ----------------------------------------------------


def test_w3_a_strangers_voice_note_is_never_downloaded_or_transcribed():
    world = World()

    world.receive(voice_message(sender=STRANGER))

    assert world.whatsapp.bodies == [replies.STRANGER_REPLY]
    assert world.whatsapp.downloads == []
    assert world.speech.transcriptions == []


def test_w4_a_paused_user_gets_the_paused_reply_and_nothing_is_spent():
    world = World(paused=True)

    world.receive(voice_message())

    assert world.whatsapp.bodies == [replies.PAUSED_REPLY]
    assert world.store.ledger == []


def test_r1_a_voice_note_writes_three_ledger_lines_on_the_right_tiers():
    world = World()

    world.receive(voice_message())

    assert world.ledger == [
        ("transcribe", "speech", "listen-model", "ok"),
        ("chat", "small", "small-model", "ok"),
        ("speak", "speech", "voice-model", "ok"),
    ]


def test_r4_voice_costs_count_toward_the_monthly_spend():
    world = World(answer="Amman.", gateway=FakeGateway(answer="Amman.", cost=Decimal("0.01")))

    world.receive(voice_message())

    costs = {entry.task: entry.cost_usd for entry in world.store.ledger}
    assert costs["transcribe"] == Decimal("0.0003")  # reported by the provider
    assert costs["chat"] == Decimal("0.01")
    assert costs["speak"] == Decimal("0.006")  # 6 characters at $1000 per million


def test_r4_transcription_cost_falls_back_to_the_listed_price_per_minute():
    world = World(speech=FakeSpeech(transcribe_cost=None))

    world.receive(voice_message())

    # FakeConverter makes two seconds of audio; the test catalog charges $0.06 a minute.
    assert world.store.ledger[0].cost_usd == Decimal("0.002")


def test_r4_over_the_cap_voice_notes_stop_with_a_message_and_text_still_works():
    world = World(cap="0.005", gateway=FakeGateway(cost=Decimal("1")))
    world.receive(text_message("spend the budget", message_id="wamid.t1"))  # now over the cap

    world.receive(voice_message())

    last = world.whatsapp.bodies[-1]
    assert "voice notes are off until 1 December" in last
    assert "Text messages and commands still work" in last
    assert world.speech.transcriptions == []
    assert world.whatsapp.voices == []


def test_r4_router_refuses_paid_speech_over_the_cap_and_allows_it_under():
    world = World(cap="1")
    user = world.store.user(world.owner_id)

    assert asyncio.run(world.router.speak(user=user, text="hello")).format == "mp3"

    # Push the month's spend over the cap.
    asyncio.run(world.store.record_usage(replace(world.store.ledger[-1], cost_usd=Decimal("5"))))

    with pytest.raises(BudgetExceeded):
        asyncio.run(world.router.speak(user=user, text="hello"))
    with pytest.raises(BudgetExceeded):
        asyncio.run(world.router.transcribe(user=user, audio=b"x", audio_format="wav", seconds=1))


def test_speech_without_a_gateway_is_reported_as_not_available():
    store = InMemoryStore()
    user_id = store.add_user(OWNER)
    router = make_router(store, FakeGateway(), speech=None)

    assert router.voice_ready() is False
    with pytest.raises(NoModelAvailable):
        asyncio.run(router.speak(user=store.user(user_id), text="hello"))


# Small helpers ------------------------------------------------------------------------


@pytest.mark.parametrize("heard", ["Status.", "status", "Pause!", " Help? ", "Resume."])
def test_w4_spoken_commands_are_recognised_despite_punctuation(heard):
    assert parse_command(heard) is not None


def test_w4_a_spoken_sentence_is_not_mistaken_for_a_command():
    assert parse_command("Help me plan my week.") is None


@pytest.mark.parametrize(
    ("answer", "copy"),
    [
        ("The capital of Jordan is Amman.", False),
        ("It opens at 9 and closes at 5.", False),
        ("Call 0790000001 tomorrow.", True),
        ("See www.example.com for details.", True),
        ("One.\nTwo.\nThree.", True),
    ],
)
def test_w2_needs_text_copy_spots_links_addresses_numbers_and_lists(answer, copy):
    assert needs_text_copy(answer) is copy


def test_spoken_part_keeps_short_answers_whole_and_cuts_long_ones_at_a_sentence():
    assert spoken_part("Short answer.") == "Short answer."

    long_answer = "First sentence. " * 200
    part = spoken_part(long_answer, limit=100)
    assert len(part) <= 100
    assert part.endswith(".")


# The catalog --------------------------------------------------------------------------


def test_the_test_catalog_and_the_shipped_file_both_define_speech():
    from pathlib import Path

    from tanios.catalog import load_catalog

    shipped = load_catalog(Path(__file__).parent.parent / "config" / "models.toml")

    assert TEST_CATALOG.transcribers and TEST_CATALOG.speakers
    assert shipped.transcribers[0].language == "en"
    assert shipped.speakers[0].voice
    assert shipped.speakers[0].format in ("mp3", "pcm")


@pytest.mark.parametrize(
    ("speech", "message"),
    [
        ({"listen": []}, "not 'listen'"),
        ({"speak": [{"provider": "openrouter", "model": "m"}]}, "needs a voice"),
        (
            {"speak": [{"provider": "openrouter", "model": "m", "voice": "v", "format": "wav"}]},
            "mp3 or pcm",
        ),
        ({"transcribe": [{"provider": "nobody", "model": "m"}]}, "provider 'nobody'"),
        ({"transcribe": [{"provider": "openrouter"}]}, "has no model"),
    ],
)
def test_mistakes_in_the_speech_section_are_reported_in_plain_words(speech, message):
    base = {
        "providers": {"openrouter": {"api_base": "https://models.example/api/v1"}},
        "tiers": {},
        "tasks": {},
        "speech": speech,
    }
    with pytest.raises(CatalogError, match=message):
        parse_catalog(base)


# The speech provider calls ------------------------------------------------------------

PROVIDER = Provider(name="openrouter", api_base="https://models.example/api/v1")


def speech_gateway(handler) -> AudioApiGateway:
    return AudioApiGateway(httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


def test_transcribe_posts_base64_audio_as_json_and_reads_text_and_cost():
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200, json={"text": " Hello there. ", "usage": {"seconds": 2.0, "cost": 0.000006}}
        )

    transcript = asyncio.run(
        speech_gateway(handler).transcribe(
            provider=PROVIDER,
            api_key="sk-or-test",
            model="openai/whisper-large-v3-turbo",
            audio=b"RIFFdata",
            audio_format="wav",
            language="en",
        )
    )

    [request] = requests
    assert str(request.url) == "https://models.example/api/v1/audio/transcriptions"
    assert request.headers["Authorization"] == "Bearer sk-or-test"
    assert json.loads(request.content) == {
        "model": "openai/whisper-large-v3-turbo",
        "input_audio": {"data": "UklGRmRhdGE=", "format": "wav"},
        "language": "en",
    }
    assert transcript.text == "Hello there."
    assert transcript.cost_usd == Decimal("0.000006")


def test_speak_posts_text_and_voice_and_returns_the_audio_bytes():
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200, content=b"\xff\xf3audio", headers={"content-type": "audio/mpeg"}
        )

    audio = asyncio.run(
        speech_gateway(handler).speak(
            provider=PROVIDER,
            api_key="sk-or-test",
            model="openai/gpt-4o-mini-tts-2025-12-15",
            voice="alloy",
            text="Hello.",
            audio_format="mp3",
        )
    )

    [request] = requests
    assert str(request.url) == "https://models.example/api/v1/audio/speech"
    assert json.loads(request.content) == {
        "model": "openai/gpt-4o-mini-tts-2025-12-15",
        "input": "Hello.",
        "voice": "alloy",
        "response_format": "mp3",
    }
    assert audio == b"\xff\xf3audio"


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(500, text="boom"),
        httpx2.Response(200, json={"error": {"message": "bad voice"}}),
        httpx2.Response(200, content=b"", headers={"content-type": "audio/mpeg"}),
    ],
)
def test_speak_failures_become_model_errors(response):
    with pytest.raises(ModelError):
        asyncio.run(
            speech_gateway(lambda request: response).speak(
                provider=PROVIDER,
                api_key="k",
                model="m",
                voice="v",
                text="private words",
                audio_format="mp3",
            )
        )


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(400, json={"error": {"message": "unsupported format"}}),
        httpx2.Response(200, text="not json"),
        httpx2.Response(200, json={"words": []}),
    ],
)
def test_transcribe_failures_become_model_errors(response):
    with pytest.raises(ModelError):
        asyncio.run(
            speech_gateway(lambda request: response).transcribe(
                provider=PROVIDER,
                api_key="k",
                model="m",
                audio=b"x",
                audio_format="wav",
                language=None,
            )
        )


# WhatsApp media -----------------------------------------------------------------------


def whatsapp_client(handler) -> CloudApiClient:
    return CloudApiClient(
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        access_token="token-abc",
        phone_number_id="1234567890",
        api_base="https://graph.example",
        graph_version="v26.0",
    )


def test_download_asks_meta_for_the_address_then_fetches_the_file_with_the_token():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((str(request.url), request.headers.get("Authorization", "")))
        if request.url.host == "graph.example":
            return httpx2.Response(
                200,
                json={
                    "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1",
                    "mime_type": "audio/ogg",
                    "file_size": 9,
                },
            )
        return httpx2.Response(200, content=b"OggS-data")

    data = asyncio.run(whatsapp_client(handler).download_media("1908647269898587"))

    assert data == b"OggS-data"
    assert seen == [
        ("https://graph.example/v26.0/1908647269898587", "Bearer token-abc"),
        ("https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1", "Bearer token-abc"),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/steal",
        "http://lookaside.fbsbx.com/plain-http",
        "https://fbsbx.com.evil.example/x",
        "https://notfbsbx.com/x",
        None,
    ],
)
def test_the_access_token_is_never_sent_to_a_host_that_is_not_metas(url):
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.host)
        return httpx2.Response(200, json={"url": url, "file_size": 5})

    with pytest.raises(WhatsAppSendError):
        asyncio.run(whatsapp_client(handler).download_media("123"))

    assert calls == ["graph.example"]


def test_a_file_over_whatsapps_size_limit_is_not_downloaded():
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.host)
        return httpx2.Response(
            200, json={"url": "https://lookaside.fbsbx.com/x", "file_size": 17 * 1024 * 1024}
        )

    with pytest.raises(WhatsAppSendError, match="too large"):
        asyncio.run(whatsapp_client(handler).download_media("123"))

    assert calls == ["graph.example"]


def test_a_media_id_that_is_not_plain_letters_and_digits_is_refused():
    with pytest.raises(WhatsAppSendError):
        asyncio.run(whatsapp_client(lambda request: None).download_media("../me/accounts"))


def test_send_voice_uploads_the_ogg_then_sends_it_marked_as_a_voice_message():
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path.endswith("/media"):
            return httpx2.Response(200, json={"id": "1013859600285441"})
        return httpx2.Response(200, json={"messages": [{"id": "wamid.out"}]})

    asyncio.run(whatsapp_client(handler).send_voice(phone=OWNER, wa_user_id=None, ogg=b"OggS123"))

    upload, send = requests
    assert str(upload.url) == "https://graph.example/v26.0/1234567890/media"
    assert upload.headers["Authorization"] == "Bearer token-abc"
    body = upload.content
    assert b'name="messaging_product"' in body and b"whatsapp" in body
    assert b'filename="voice.ogg"' in body and b"audio/ogg" in body and b"OggS123" in body
    assert str(send.url) == "https://graph.example/v26.0/1234567890/messages"
    assert json.loads(send.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": OWNER,
        "type": "audio",
        "audio": {"id": "1013859600285441", "voice": True},
    }


def test_a_failed_upload_means_no_message_is_sent():
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(400, json={"error": {"code": 131053, "type": "OAuthException"}})

    with pytest.raises(WhatsAppSendError, match="400"):
        asyncio.run(whatsapp_client(handler).send_voice(phone=OWNER, wa_user_id=None, ogg=b"x"))

    assert len(requests) == 1


# Real audio conversion (needs ffmpeg) -------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_audio(tmp_path, name: str, *extra: str) -> bytes:
    """Three seconds of a test tone in the requested format, made with ffmpeg itself."""
    target = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=3", "-ac", "1", *extra, str(target)],
        check=True,
    )  # fmt: skip
    return target.read_bytes()


def probe(tmp_path, data: bytes, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    result = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-show_entries",
         "stream=codec_name,channels,sample_rate:format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return result.stdout


@needs_ffmpeg
def test_w2_a_whatsapp_voice_note_becomes_16khz_mono_wav(tmp_path):
    voice_note = make_audio(tmp_path, "in.ogg", "-c:a", "libopus", "-b:a", "24k")

    wav = asyncio.run(FfmpegConverter().to_wav(voice_note))

    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert "pcm_s16le,16000,1" in probe(tmp_path, wav, "out.wav")
    assert wav_seconds(wav) == pytest.approx(3.0, abs=0.1)


@needs_ffmpeg
def test_w2_mp3_from_a_speech_model_becomes_an_ogg_opus_voice_note(tmp_path):
    mp3 = make_audio(tmp_path, "in.mp3")

    ogg = asyncio.run(FfmpegConverter().to_voice_note(mp3, "mp3"))

    assert ogg[:4] == b"OggS" and b"OpusHead" in ogg[:100]
    info = probe(tmp_path, ogg, "out.ogg")
    assert info.startswith("opus,48000,1")  # Opus always reports 48 kHz; mono is what matters
    assert len(ogg) < 512 * 1024  # small enough for WhatsApp's play button


@needs_ffmpeg
def test_w2_raw_pcm_from_a_speech_model_becomes_an_ogg_opus_voice_note(tmp_path):
    pcm = make_audio(tmp_path, "in.raw", "-f", "s16le", "-ar", "24000")

    ogg = asyncio.run(FfmpegConverter().to_voice_note(pcm, "pcm"))

    assert ogg[:4] == b"OggS"
    # Read as 24 kHz mono, three seconds of PCM must still be three seconds of voice note.
    duration = float(probe(tmp_path, ogg, "out.ogg").strip().splitlines()[-1])
    assert duration == pytest.approx(3.0, abs=0.2)


@needs_ffmpeg
def test_w2_a_very_long_voice_note_is_cut_at_five_minutes(tmp_path):
    target = tmp_path / "long.ogg"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=330", "-ac", "1", "-c:a", "libopus", "-b:a", "8k",
         str(target)],
        check=True,
    )  # fmt: skip

    wav = asyncio.run(FfmpegConverter().to_wav(target.read_bytes()))

    assert wav_seconds(wav) == pytest.approx(300, abs=1)


@needs_ffmpeg
def test_w2_a_file_that_is_not_audio_is_an_audio_error_not_a_crash():
    with pytest.raises(AudioError):
        asyncio.run(FfmpegConverter().to_wav(b"this is not audio at all"))


def test_empty_audio_and_a_missing_ffmpeg_are_audio_errors():
    with pytest.raises(AudioError, match="empty"):
        asyncio.run(FfmpegConverter().to_wav(b""))
    with pytest.raises(AudioError, match="not installed"):
        asyncio.run(FfmpegConverter(ffmpeg="ffmpeg-that-does-not-exist").to_wav(b"x"))
