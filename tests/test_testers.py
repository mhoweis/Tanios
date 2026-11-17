"""Milestone 7: testers, the setup page, deleting data, and template alerts (U1-U3, W5)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx2
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tanios import assistant as replies
from tanios import people
from tanios.assistant import Assistant
from tanios.config import Settings
from tanios.crypto import TokenBox
from tanios.mail import Mailroom
from tanios.main import create_app
from tanios.memory import Memory
from tanios.people import PeopleRequest, Team, parse_people
from tanios.router import Router
from tanios.setup import COOKIE, SetupDesk, csrf_for
from tanios.vault import FolderVault, VaultError
from tests.fakes import (
    OWNER,
    STRANGER,
    TEST_CATALOG,
    FakeConverter,
    FakeGateway,
    FakeGmail,
    FakeOAuth,
    FakeSpeech,
    FakeWhatsApp,
    InMemoryMailStore,
    InMemorySetupTokens,
    InMemoryStore,
    InMemoryUserKeys,
    an_email,
    mail_model,
    text_message,
    voice_message,
)
from tests.test_voice import whatsapp_client

START = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
TESTER = "962790000002"
OWNER_SERVER_KEY = "sk-or-owner-server-key"
TESTER_KEY = "sk-or-v1-tester-own-key-123"
LABELS = {"Invoice": "needs_reply", "Weekly": "newsletter"}


class World:
    """The owner, the server's own key, email and memory on, and the setup page."""

    def __init__(
        self,
        tmp_path,
        *,
        template: tuple[str, str] | None = ("tanios_alert", "en"),
        setup_on: bool = True,
        hears: str = "",
    ) -> None:
        self.clock = START
        self.root = tmp_path / "vaults"
        self.store = InMemoryStore()
        self.owner_id = self.store.add_user(OWNER, is_owner=True)
        self.keys = InMemoryUserKeys(self.store)
        self.tokens = InMemorySetupTokens(self.store)
        self.whatsapp = FakeWhatsApp()
        self.whatsapp.media["MEDIA1"] = b"recording"
        self.model = FakeGateway(script=mail_model(LABELS))
        self.speech = FakeSpeech(hears=hears)
        self.oauth = FakeOAuth()
        self.mailboxes: dict[int, FakeGmail] = {}
        self.router = Router(
            TEST_CATALOG,
            {"openrouter": OWNER_SERVER_KEY},
            self.model,
            self.store,
            speech=self.speech,
            user_keys=self.keys,
            now=lambda: self.clock,
        )
        self.mailstore = InMemoryMailStore()
        self.gmail = FakeGmail(self.oauth)
        self.mail = Mailroom(
            store=self.mailstore,
            users=self.store,
            oauth=self.oauth,
            gmail_for=self.gmail.bind,
            box=TokenBox(Fernet.generate_key().decode()),
            router=self.router,
            whatsapp=self.whatsapp,
            public_url="https://tanios.example",
            timezone=UTC,
            memories=self.memory_for,
            alert_template=template,
            now=lambda: self.clock,
        )
        self.setup = (
            SetupDesk(
                tokens=self.tokens,
                users=self.store,
                keys=self.keys,
                catalog=TEST_CATALOG,
                public_url="https://tanios.example",
                mail=self.mail,
                memory_on=True,
                now=lambda: self.clock,
            )
            if setup_on
            else None
        )
        self.team = Team(
            store=self.store,
            memories=self.memory_for,
            mail=self.mail,
            setup=self.setup,
            tester_cap_usd=Decimal("5"),
            now=lambda: self.clock,
        )
        if self.setup is not None:
            self.setup.team = self.team
        self.assistant = Assistant(
            self.store,
            self.whatsapp,
            self.router,
            audio=FakeConverter(),
            memories=self.memory_for,
            mail=self.mail,
            team=self.team,
            now=lambda: self.clock,
        )
        self._sent = 0

    def memory_for(self, user_id: int) -> Memory:
        return Memory(FolderVault(self.root / f"user-{user_id}"), now=lambda: self.clock)

    def run(self, coroutine):
        return asyncio.run(coroutine)

    def say(self, *texts: str, sender: str = OWNER) -> list[str]:
        before = len(self.whatsapp.bodies)

        async def scenario() -> None:
            for text in texts:
                self._sent += 1
                await self.assistant.handle(
                    text_message(text, sender=sender, message_id=f"wamid.{self._sent}")
                )

        asyncio.run(scenario())
        return self.whatsapp.bodies[before:]

    def later(self, **delta) -> None:
        self.clock += timedelta(**delta)

    def tick(self) -> list:
        before_text, before_templates = len(self.whatsapp.bodies), len(self.whatsapp.templates)
        self.run(self.mail.run_due())
        return self.whatsapp.bodies[before_text:] + self.whatsapp.templates[before_templates:]

    def user(self, phone: str):
        return self.run(self.store.find_user(phone, None))

    def tester(self, *, with_key: bool = True):
        """Invite the tester, let them write once, and give them their own key."""
        [reply] = self.say(f"invite +{TESTER}")
        assert "is invited" in reply
        tester = self.user(TESTER)
        if with_key:
            self.run(self.keys.put(tester.id, "openrouter", TESTER_KEY))
        return tester

    def setup_session(self, phone: str) -> str:
        [reply] = self.say("setup", sender=phone)
        token = reply.split("/setup/open?t=")[1].split()[0]
        return self.run(self.setup.open_link(token))

    def connect_gmail(self, phone: str) -> None:
        user = self.user(phone)
        url = self.run(self.mail.consent_for(user))
        state = url.split("state=")[1]
        assert self.run(self.mail.finish_connection(state, "good-code"))


# Recognising the commands -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "request_"),
    [
        ("invite +962 79 000 0002", PeopleRequest("invite", "962790000002")),
        ("Invite 962-79-000-0002.", PeopleRequest("invite", "962790000002")),
        ("add tester +44 7700 900123", PeopleRequest("invite", "447700900123")),
        ("remove +962790000002", PeopleRequest("remove", "962790000002")),
        ("testers", PeopleRequest("testers")),
        ("Setup", PeopleRequest("setup")),
        ("settings", PeopleRequest("setup")),
        ("Hey Tanios, please delete my data", PeopleRequest("delete")),
        ("delete all my data!", PeopleRequest("delete")),
        ("yes delete everything", PeopleRequest("confirm_delete")),
        ("Yes, delete everything.", PeopleRequest("confirm_delete")),
        ("invite my sister to dinner", None),
        ("remove the stain from my shirt", None),
        ("how do I set up a VPN?", None),
        ("delete my data from facebook", None),
        ("yes", None),
    ],
)
def test_u1_u3_people_commands_are_recognised_from_the_whole_message(said, request_):
    assert parse_people(said) == request_


# U1: the owner invites a tester, who sets up on a private page -------------------------------


def test_u1_the_owner_invites_a_tester_by_phone_number(tmp_path):
    world = World(tmp_path)

    [reply] = world.say("invite +962 79 000 0002")

    assert reply.startswith("+962790000002 is invited, with a $5.00 cap to start.")
    assert "ask them to send any message" in reply
    tester = world.user(TESTER)
    assert tester is not None and tester.is_owner is False
    assert tester.monthly_cap_usd == Decimal("5")


def test_u3_w3_before_the_invite_the_tester_is_a_stranger(tmp_path):
    world = World(tmp_path)

    assert world.say("hello", sender=TESTER) == [replies.STRANGER_REPLY]
    world.say(f"invite +{TESTER}")
    assert world.say("hello", sender=TESTER) == [replies.NEEDS_KEY_REPLY]


def test_u1_only_the_owner_can_invite_list_or_remove(tmp_path):
    world = World(tmp_path)
    world.tester()

    out = world.say("invite +962790000009", "testers", f"remove +{OWNER}", sender=TESTER)

    assert out == [people.OWNER_ONLY] * 3
    assert world.user("962790000009") is None


def test_u1_invites_that_make_no_sense_are_refused(tmp_path):
    world = World(tmp_path)
    world.tester()

    assert world.say(f"invite +{OWNER}", f"invite +{TESTER}", "invite +1234 5678 9012 3456") == [
        "That is your own number.",
        f"+{TESTER} is already registered.",
        people.NOT_A_PHONE,
    ]


def test_u1_there_is_a_limit_on_testers(tmp_path):
    world = World(tmp_path)
    for number in range(people.MAX_TESTERS):
        world.say(f"invite +9627910000{number:02d}")

    [reply] = world.say("invite +962791999999")

    assert reply == f"There are already {people.MAX_TESTERS} testers. Remove one first."


def test_u1_u2_without_the_setup_page_no_tester_can_be_invited(tmp_path):
    world = World(tmp_path, setup_on=False)

    assert world.say(f"invite +{TESTER}", "setup") == [people.SETUP_OFF] * 2
    assert world.user(TESTER) is None


def test_u1_the_owner_can_list_testers(tmp_path):
    world = World(tmp_path)
    assert world.say("testers")[0].startswith("No testers yet.")
    world.tester()
    world.say("hello", sender=TESTER)

    [reply] = world.say("testers")

    assert reply.splitlines()[:2] == ["Testers:", f"- +{TESTER} (has written)"]


def test_u1_setup_sends_a_one_time_link_to_this_server(tmp_path):
    world = World(tmp_path)
    world.tester()

    [reply] = world.say("setup", sender=TESTER)

    assert "https://tanios.example/setup/open?t=" in reply
    assert "Never type a key into WhatsApp" in reply
    token = reply.split("?t=")[1].split()[0]
    assert len(token) >= 40
    assert token not in json.dumps(list(world.tokens.rows))  # only its hash is kept


def test_u1_the_link_opens_a_session_once_and_only_for_15_minutes(tmp_path):
    world = World(tmp_path)
    world.tester()
    [reply] = world.say("setup", sender=TESTER)
    token = reply.split("?t=")[1].split()[0]

    session = world.run(world.setup.open_link(token))

    assert session is not None
    assert world.run(world.setup.open_link(token)) is None  # used up
    assert world.run(world.setup.session_user(session)).phone == TESTER

    [reply] = world.say("setup", sender=TESTER)
    late = reply.split("?t=")[1].split()[0]
    world.later(minutes=16)
    assert world.run(world.setup.open_link(late)) is None


def test_u1_a_setup_session_lasts_30_minutes(tmp_path):
    world = World(tmp_path)
    world.tester()
    session = world.setup_session(TESTER)

    world.later(minutes=29)
    assert world.run(world.setup.session_user(session)) is not None
    world.later(minutes=2)
    assert world.run(world.setup.session_user(session)) is None
    assert world.run(world.setup.session_user("made-up")) is None
    assert world.run(world.setup.session_user(None)) is None


def test_u1_r2_a_key_added_on_the_page_lets_the_tester_use_tanios(tmp_path):
    world = World(tmp_path)
    tester = world.tester(with_key=False)
    assert world.say("hello", sender=TESTER) == [replies.NEEDS_KEY_REPLY]

    outcome = world.run(world.setup.save_key(tester, "OpenRouter", f"  {TESTER_KEY}  "))

    assert outcome.message == "Your openrouter key is saved."
    assert world.say("what is the capital of Jordan?", sender=TESTER) == ["Here is your answer."]
    assert world.model.calls[-1]["api_key"] == TESTER_KEY


def test_r2_status_lists_the_keys_a_user_added(tmp_path):
    world = World(tmp_path)
    world.tester()

    [status] = world.say("status", sender=TESTER)
    [owner_status] = world.say("status")

    assert "Model keys: openrouter (added by you)" in status
    assert "Model keys: openrouter\n" in owner_status


def test_u1_a_saved_key_is_never_shown_again_on_the_page_or_on_whatsapp(tmp_path, caplog):
    world = World(tmp_path)
    tester = world.tester(with_key=False)
    session = world.setup_session(TESTER)

    with caplog.at_level("DEBUG"):
        world.run(world.setup.save_key(tester, "openrouter", TESTER_KEY))
        page = world.run(world.setup.page(tester, session))
        world.say("status", "what is the capital of Jordan?", sender=TESTER)

    assert TESTER_KEY not in page
    assert "<td>openrouter</td><td>added</td>" in page
    assert TESTER_KEY not in " ".join(world.whatsapp.bodies)
    assert TESTER_KEY not in caplog.text


@pytest.mark.parametrize(
    ("provider", "key", "said"),
    [
        ("openrouter", "short", "That does not look like a key."),
        ("openrouter", "has spaces in the middle of it", "That does not look like a key."),
        ("openrouter", "x" * 401, "That does not look like a key."),
        ("evil-provider", TESTER_KEY, "That provider is not in this server's list."),
    ],
)
def test_u1_keys_that_cannot_be_right_are_refused(tmp_path, provider, key, said):
    world = World(tmp_path)
    tester = world.tester(with_key=False)

    outcome = world.run(world.setup.save_key(tester, provider, key))

    assert outcome.message.startswith(said)
    assert world.keys.keys.get(tester.id, {}) == {}


def test_u1_a_key_can_be_removed(tmp_path):
    world = World(tmp_path)
    tester = world.tester()

    world.run(world.setup.remove_key(tester, "openrouter"))

    assert world.say("hello", sender=TESTER) == [replies.NEEDS_KEY_REPLY]


def test_u1_r4_a_tester_sets_their_own_budget_on_the_page(tmp_path):
    world = World(tmp_path)
    tester = world.tester()

    outcome = world.run(world.setup.save_budget(tester, "$12.50", True))

    assert outcome.message == "Saved: $12.50 a month, free models for private content on."
    updated = world.user(TESTER)
    assert updated.monthly_cap_usd == Decimal("12.50") and updated.allow_free_for_private
    assert world.run(world.setup.save_budget(tester, "", False)).message.startswith("Saved: no cap")
    for bad in ("lots", "-1", "5000", "NaN", "Infinity"):
        assert "cap must" in world.run(world.setup.save_budget(tester, bad, False)).message
    assert world.user(TESTER).monthly_cap_usd is None


def test_u1_the_owners_budget_stays_in_the_server_settings(tmp_path):
    world = World(tmp_path)
    owner = world.user(OWNER)
    session = world.setup_session(OWNER)

    outcome = world.run(world.setup.save_budget(owner, "1", True))
    page = world.run(world.setup.page(owner, session))

    assert outcome.message.startswith("As the owner, you set your budget")
    assert world.user(OWNER).monthly_cap_usd is None
    assert "TANIOS_MONTHLY_CAP_USD" in page and "name=cap" not in page
    assert "Delete my data" not in page


def test_u1_e1_gmail_is_connected_from_the_setup_page(tmp_path):
    world = World(tmp_path)
    tester = world.tester()

    outcome = world.run(world.setup.connect_gmail(tester))

    assert outcome.redirect.startswith("https://accounts.google.example/auth?state=")
    state = outcome.redirect.split("state=")[1]
    assert world.run(world.mail.finish_connection(state, "good-code")) == "owner@example.com"
    assert tester.id in world.mailstore.conns
    session = world.setup_session(TESTER)
    assert "Disconnect Gmail" in world.run(world.setup.page(tester, session))


def test_u1_the_page_escapes_everything_it_shows(tmp_path):
    world = World(tmp_path)
    tester = world.tester()
    session = world.setup_session(TESTER)

    page = world.run(world.setup.page(tester, session, notice='<script>alert("x")</script>'))

    assert "<script>" not in page
    assert "&lt;script&gt;" in page


# U2: each user's data stays theirs ------------------------------------------------------------


def test_u2_a_tester_never_uses_the_owners_server_key(tmp_path):
    world = World(tmp_path)
    world.tester(with_key=False)

    out = world.say("hello", "status", sender=TESTER)
    world.run(world.assistant.handle(voice_message(sender=TESTER)))

    assert out[0] == replies.NEEDS_KEY_REPLY
    assert "Model keys: none" in out[1]
    assert world.whatsapp.bodies[-1] == replies.VOICE_OFF_REPLY
    assert world.model.calls == [] and world.speech.transcriptions == []


def test_u2_every_call_uses_the_callers_own_key(tmp_path):
    world = World(tmp_path, hears="What is the capital of Jordan?")
    world.tester()

    world.say("hello")
    world.say("hello", sender=TESTER)
    world.run(world.assistant.handle(voice_message(sender=TESTER, message_id="wamid.v1")))

    keys = [call["api_key"] for call in world.model.calls]
    assert keys == [OWNER_SERVER_KEY, TESTER_KEY, TESTER_KEY]
    assert {call["api_key"] for call in world.speech.transcriptions} == {TESTER_KEY}


def test_u2_the_owners_own_key_replaces_the_servers_for_that_provider(tmp_path):
    world = World(tmp_path)
    world.run(world.keys.put(world.owner_id, "openrouter", "sk-or-owner-personal"))

    world.say("hello")

    assert world.model.calls[-1]["api_key"] == "sk-or-owner-personal"


def test_u2_spend_and_caps_are_counted_per_user(tmp_path):
    world = World(tmp_path)
    world.tester()

    world.say("hello", "hello", "hello")
    [status] = world.say("status", sender=TESTER)

    assert "No model calls yet this month." in status
    assert {entry.user_id for entry in world.store.ledger} == {world.owner_id}


def test_u2_notes_and_files_are_kept_apart(tmp_path):
    world = World(tmp_path)
    tester = world.tester()

    world.say("remember my accountant is Rana", sender=TESTER)
    world.say("who is my accountant?")

    assert (tmp_path / "vaults" / f"user-{tester.id}" / "Memory").exists()
    assert "Rana" not in world.model.calls[-1]["user"]


def test_u2_a_tester_cannot_see_or_send_the_owners_mail(tmp_path):
    world = World(tmp_path)
    world.tester()
    world.connect_gmail(OWNER)
    world.gmail.deliver(an_email("m1", "Invoice for October"))
    world.say("what's new?", "draft a reply to 1 saying yes")

    out = world.say("check mail", "send", "send 1", "draft a reply to 1 saying no", sender=TESTER)

    assert out[0] == "Gmail is not connected. Send connect gmail to connect it."
    assert world.gmail.sent == []
    assert len(world.gmail.drafts) == 1


def test_u2_a_setup_session_only_ever_acts_for_its_own_user(tmp_path):
    world = World(tmp_path)
    world.tester()
    session = world.setup_session(TESTER)

    user = world.run(world.setup.session_user(session))
    page = world.run(world.setup.page(user, session))

    assert user.phone == TESTER
    assert f"Number: +{TESTER}" in page and OWNER not in page
    assert csrf_for(session) in page and csrf_for("another-session") not in page


# U3: delete my data ----------------------------------------------------------------------------


def test_u3_delete_my_data_asks_first_and_deletes_nothing_until_confirmed(tmp_path):
    world = World(tmp_path)
    tester = world.tester()
    world.say("remember my locker is 42", sender=TESTER)

    [warning] = world.say("delete my data", sender=TESTER)

    assert warning == people.DELETE_WARNING
    assert world.user(TESTER) is not None
    assert (tmp_path / "vaults" / f"user-{tester.id}").exists()


def test_u3_confirmed_the_server_holds_nothing_of_the_tester(tmp_path):
    world = World(tmp_path)
    tester = world.tester()
    world.connect_gmail(TESTER)
    world.say("remember my locker is 42", "hello", sender=TESTER)
    session = world.setup_session(TESTER)
    vault = tmp_path / "vaults" / f"user-{tester.id}"
    assert vault.exists() and world.store.ledger

    world.say("delete my data", sender=TESTER)
    [done] = world.say("yes, delete everything", sender=TESTER)

    assert done.startswith("Done. Everything this server held about you is deleted")
    assert not vault.exists()
    assert world.user(TESTER) is None
    assert tester.id not in world.keys.keys
    assert tester.id not in world.mailstore.conns
    assert world.oauth.revocations == ["refresh-secret"]  # cancelled at Google too
    assert all(entry.user_id != tester.id for entry in world.store.ledger)
    assert world.run(world.setup.session_user(session)) is None
    assert all(row["user_id"] != tester.id for row in world.tokens.rows.values())
    # Afterwards they are a stranger again, and the owner is untouched.
    assert world.say("hello", sender=TESTER) == [replies.STRANGER_REPLY]
    assert world.say("hello") == ["Here is your answer."]


def test_u3_the_confirmation_must_come_within_10_minutes(tmp_path):
    world = World(tmp_path)
    world.tester()

    world.say("delete my data", sender=TESTER)
    world.later(minutes=11)

    assert world.say("yes delete everything", sender=TESTER) == [people.NOTHING_TO_CONFIRM]
    assert world.user(TESTER) is not None


def test_u3_a_confirmation_without_a_request_does_nothing(tmp_path):
    world = World(tmp_path)
    world.tester()

    assert world.say("yes delete everything", sender=TESTER) == [people.NOTHING_TO_CONFIRM]
    assert world.user(TESTER) is not None


def test_u3_a_spoken_confirmation_is_refused(tmp_path):
    world = World(tmp_path, hears="Yes, delete everything.")
    world.tester()
    world.say("delete my data", sender=TESTER)

    world.run(world.assistant.handle(voice_message(sender=TESTER)))

    assert world.whatsapp.bodies[-1] == replies.TYPE_IT_REPLY
    assert world.user(TESTER) is not None


def test_u3_the_owner_cannot_delete_themselves_by_message(tmp_path):
    world = World(tmp_path)
    world.say("remember my locker is 42")

    assert world.say("delete my data", "yes delete everything") == [
        people.OWNER_CANNOT_DELETE,
        people.NOTHING_TO_CONFIRM,
    ]
    assert world.user(OWNER) is not None


def test_u3_the_owner_removes_a_tester_and_their_data(tmp_path):
    world = World(tmp_path)
    tester = world.tester()
    world.say("remember my locker is 42", sender=TESTER)

    [reply] = world.say(f"remove +{TESTER}")

    assert reply.startswith(f"Done. Everything about +{TESTER} is deleted")
    assert world.user(TESTER) is None
    assert not (tmp_path / "vaults" / f"user-{tester.id}").exists()
    assert world.say(f"remove +{TESTER}") == [f"+{TESTER} is not registered."]


def test_u3_the_page_can_delete_too_but_only_with_delete_typed(tmp_path):
    world = World(tmp_path)
    tester = world.tester()

    wrong = world.run(world.setup.delete_everything(tester, "delete"))
    right = world.run(world.setup.delete_everything(tester, "DELETE"))

    assert wrong.message.startswith("Nothing was deleted.") and not wrong.ended
    assert right.ended and world.user(TESTER) is None


def test_u3_if_the_vault_cannot_be_deleted_nothing_else_is_removed(tmp_path, monkeypatch):
    world = World(tmp_path)
    world.tester()

    def broken(self):
        raise VaultError("vault could not be deleted")

    monkeypatch.setattr(FolderVault, "destroy", broken)
    world.say("delete my data", sender=TESTER)
    [reply] = world.say("yes delete everything", sender=TESTER)

    assert reply.startswith("Sorry, I could not delete the notes and files")
    assert world.user(TESTER) is not None


def test_u3_a_held_file_is_forgotten_with_the_user(tmp_path):
    world = World(tmp_path)
    tester = world.tester()
    world.assistant._waiting[tester.id] = ("file", world.clock)

    world.say(f"remove +{TESTER}")

    assert tester.id not in world.assistant._waiting


def test_u3_destroying_a_vault_never_follows_a_link_out_of_it(tmp_path):
    outside = tmp_path / "personal"
    outside.mkdir()
    (outside / "diary.md").write_text("mine")
    (tmp_path / "user-9").symlink_to(outside, target_is_directory=True)

    assert FolderVault(tmp_path / "user-9").destroy() is True

    assert (outside / "diary.md").read_text() == "mine"
    assert not (tmp_path / "user-9").exists()
    assert FolderVault(tmp_path / "user-10").destroy() is False


# W5: reaching the user first with a template ----------------------------------------------------


def test_w5_after_25_silent_hours_an_important_email_brings_one_template_alert(tmp_path):
    world = World(tmp_path)
    world.say("hello")
    world.connect_gmail(OWNER)
    world.later(hours=25)
    world.gmail.deliver(an_email("m1", "Invoice for October", received_at=world.clock))

    sent = world.tick()

    assert sent == [(OWNER, "tanios_alert", "en", "1 new email needs a reply")]
    assert world.user(OWNER).template_sent_at == world.clock


def test_w5_only_one_template_goes_out_until_the_user_writes_again(tmp_path):
    world = World(tmp_path)
    world.say("hello")
    world.connect_gmail(OWNER)
    world.later(hours=25)
    world.gmail.deliver(an_email("m1", "Invoice for October", received_at=world.clock))
    world.tick()

    world.later(hours=2)
    world.gmail.deliver(an_email("m2", "Invoice for November", received_at=world.clock))
    assert world.tick() == []

    world.say("thanks")  # inside the window again: plain text from now on
    world.later(minutes=61)
    world.gmail.deliver(an_email("m3", "Invoice for December", received_at=world.clock))
    assert world.tick() == ["1 new email needs a reply. Say what's new? for the list."]

    world.later(hours=26)
    world.gmail.deliver(an_email("m4", "Invoice for January", received_at=world.clock))
    assert world.tick() == [(OWNER, "tanios_alert", "en", "1 new email needs a reply")]


def test_w5_the_template_carries_a_count_never_email_content(tmp_path):
    world = World(tmp_path)
    world.say("hello")
    world.connect_gmail(OWNER)
    world.later(hours=25)
    world.gmail.deliver(
        an_email("m1", "Invoice for October", "Secret 48,112", received_at=world.clock),
        an_email("m2", "Invoice: urgent", received_at=world.clock),
    )

    [(_, _, _, text)] = world.tick()

    assert text == "2 new emails need a reply"


def test_w5_without_a_template_set_up_nothing_is_sent_after_24_hours(tmp_path):
    world = World(tmp_path, template=None)
    world.say("hello")
    world.connect_gmail(OWNER)
    world.later(hours=25)
    world.gmail.deliver(an_email("m1", "Invoice for October", received_at=world.clock))

    assert world.tick() == []
    assert world.gmail.applied  # still sorted


def test_w5_lost_gmail_access_is_reported_by_template_after_24_hours(tmp_path):
    world = World(tmp_path)
    world.say("hello")
    world.connect_gmail(OWNER)
    world.later(hours=25)
    world.oauth.revoked = True

    assert world.tick() == [(OWNER, "tanios_alert", "en", "Gmail was disconnected")]


def test_w5_mail_that_needs_no_reply_is_not_worth_a_template(tmp_path):
    world = World(tmp_path)
    world.say("hello")
    world.connect_gmail(OWNER)
    world.later(hours=25)
    world.gmail.deliver(an_email("m1", "Weekly deals", received_at=world.clock))

    assert world.tick() == []


def test_m8_w5_a_template_alert_is_recorded_in_the_log(tmp_path):
    world = World(tmp_path)
    world.say("hello")
    world.connect_gmail(OWNER)
    world.later(hours=25)
    world.gmail.deliver(an_email("m1", "Invoice for October", received_at=world.clock))

    world.tick()

    log = (tmp_path / "vaults" / "user-1" / "Log" / f"{world.clock:%Y-%m-%d}.md").read_text()
    assert "Sent a WhatsApp alert: 1 new email needs a reply" in log


def test_w5_a_template_is_sent_in_the_form_meta_expects():
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"messages": [{"id": "wamid.t"}]})

    asyncio.run(
        whatsapp_client(handler).send_template(
            phone=OWNER, wa_user_id=None, name="tanios_alert", language="en", text="2 new emails"
        )
    )

    [request] = requests
    assert str(request.url) == "https://graph.example/v26.0/1234567890/messages"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": OWNER,
        "type": "template",
        "template": {
            "name": "tanios_alert",
            "language": {"code": "en"},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": "2 new emails"}]}
            ],
        },
    }


# The setup page over the web ---------------------------------------------------------------------


def client_for(world: World) -> TestClient:
    settings = Settings(_env_file=None, google_auth_url="https://accounts.google.example/o/auth")
    return TestClient(
        create_app(settings, setup=world.setup),
        base_url="https://tanios.example",
        follow_redirects=False,
    )


def open_page(world: World, client: TestClient) -> str:
    [reply] = world.say("setup", sender=TESTER)
    token = reply.split("?t=")[1].split()[0]
    response = client.get("/setup/open", params={"t": token})
    assert response.status_code == 303 and response.headers["location"] == "/setup"
    return client.cookies.get(COOKIE)


def test_u1_opening_the_link_sets_a_locked_down_cookie_and_clears_the_address(tmp_path):
    world = World(tmp_path)
    world.tester()
    with client_for(world) as client:
        [reply] = world.say("setup", sender=TESTER)
        token = reply.split("?t=")[1].split()[0]

        response = client.get("/setup/open", params={"t": token})

    cookie = response.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=strict", "path=/setup", "max-age=1800"):
        assert flag in cookie
    assert response.headers["location"] == "/setup"


def test_u1_the_page_needs_a_live_session_and_sends_strict_security_headers(tmp_path):
    world = World(tmp_path)
    world.tester()
    with client_for(world) as client:
        assert client.get("/setup").status_code == 401
        assert client.get("/setup/open", params={"t": "made-up"}).status_code == 401
        open_page(world, client)
        page = client.get("/setup")

    assert page.status_code == 200
    assert f"Number: +{TESTER}" in page.text
    policy = page.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "script-src" not in policy
    assert "form-action 'self' https://accounts.google.example" in policy
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["cache-control"] == "no-store"


def test_u1_a_form_without_the_pages_own_check_value_is_refused(tmp_path):
    world = World(tmp_path)
    tester = world.tester(with_key=False)
    with client_for(world) as client:
        open_page(world, client)
        forged = client.post("/setup/keys", data={"provider": "openrouter", "key": TESTER_KEY})
        wrong = client.post(
            "/setup/keys",
            data={"provider": "openrouter", "key": TESTER_KEY, "csrf": csrf_for("other")},
        )

    assert forged.status_code == wrong.status_code == 403
    assert world.keys.keys.get(tester.id, {}) == {}


def test_u1_saving_a_key_and_a_budget_through_the_page(tmp_path):
    world = World(tmp_path)
    tester = world.tester(with_key=False)
    with client_for(world) as client:
        session = open_page(world, client)
        csrf = csrf_for(session)
        saved = client.post(
            "/setup/keys", data={"provider": "openrouter", "key": TESTER_KEY, "csrf": csrf}
        )
        budget = client.post("/setup/budget", data={"cap": "3", "csrf": csrf})

    assert "Your openrouter key is saved." in saved.text and TESTER_KEY not in saved.text
    assert world.keys.keys[tester.id] == {"openrouter": TESTER_KEY}
    assert "Saved: $3.00 a month, free models for private content off." in budget.text
    assert world.user(TESTER).monthly_cap_usd == Decimal("3")


def test_u1_connect_gmail_on_the_page_goes_straight_to_google(tmp_path):
    world = World(tmp_path)
    world.tester()
    with client_for(world) as client:
        session = open_page(world, client)
        response = client.post("/setup/gmail/connect", data={"csrf": csrf_for(session)})

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://accounts.google.example/auth?state=")


def test_u3_deleting_on_the_page_ends_the_session(tmp_path):
    world = World(tmp_path)
    world.tester()
    with client_for(world) as client:
        session = open_page(world, client)
        done = client.post("/setup/delete", data={"confirm": "DELETE", "csrf": csrf_for(session)})
        after = client.get("/setup")

    assert "Your data is deleted" in done.text
    assert after.status_code == 401
    assert world.user(TESTER) is None


def test_u1_with_the_setup_page_off_its_addresses_do_nothing(tmp_path):
    with TestClient(create_app(Settings(_env_file=None)), follow_redirects=False) as client:
        assert client.get("/setup/open", params={"t": "x"}).status_code == 401
        assert client.get("/setup").status_code == 401
        assert client.post("/setup/keys", data={"csrf": "x"}).status_code == 401


def test_u1_the_tester_cap_is_a_setting(monkeypatch):
    assert Settings(_env_file=None).tester_cap_usd == Decimal("5")
    monkeypatch.setenv("TANIOS_TESTER_CAP_USD", "2.5")
    assert Settings(_env_file=None).tester_cap_usd == Decimal("2.5")
    monkeypatch.setenv("TANIOS_WHATSAPP_ALERT_TEMPLATE", "tanios_alert")
    assert Settings(_env_file=None).whatsapp_alert_template == "tanios_alert"


def test_u1_help_shows_the_owner_commands_only_to_the_owner(tmp_path):
    world = World(tmp_path)
    world.tester()

    [owner_help] = world.say("help")
    [tester_help] = world.say("help", sender=TESTER)

    assert "invite +962" in owner_help and "invite +962" not in tester_help
    assert "setup - your private page" in tester_help
    assert "delete my data" in tester_help


def test_w3_a_stranger_gets_no_setup_link(tmp_path):
    world = World(tmp_path)

    assert world.say("setup", "delete my data", sender=STRANGER) == [replies.STRANGER_REPLY]
    assert world.tokens.rows == {}


def test_w5_ordinary_notices_never_go_out_as_a_template(tmp_path):
    world = World(tmp_path)
    world.say("hello")
    world.later(hours=25)

    world.connect_gmail(OWNER)  # "Gmail is connected" cannot reach them now
    world.say("mail summary at 13:00")
    world.later(hours=26)
    world.gmail.deliver(an_email("m1", "Weekly deals", received_at=world.clock))
    world.tick()

    assert world.whatsapp.templates == []
