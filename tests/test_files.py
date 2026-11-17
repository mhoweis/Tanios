"""Milestone 6: saving files into the vault (M6)."""

import asyncio
import base64
import json
import shutil
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from tanios import assistant as replies
from tanios import mail
from tanios.config import Settings
from tanios.files import (
    KINDS,
    FileRequest,
    parse_file_request,
    readable_size,
    safe_extension,
    without_extension,
)
from tanios.gmail.client import ALLOWED_CALLS, GmailApi, GmailError, allowed
from tanios.gmail.message import Attachment, parse_message
from tanios.mail import pick_attachment
from tanios.mailtalk import MailRequest, parse_mail
from tanios.memory import Memory
from tanios.vault import FolderVault, VaultError
from tanios.whatsapp.client import MediaTooLarge
from tanios.whatsapp.parsing import parse_webhook
from tests.fakes import STRANGER, FakeGateway, an_email, file_message, text_message, voice_message
from tests.test_mail import World as MailWorld
from tests.test_voice import whatsapp_client

PDF = b"%PDF-1.7 pretend lease"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


class World(MailWorld):
    """The mail world with a vault, and a PDF and a photo waiting on WhatsApp."""

    def __init__(self, tmp_path, **options) -> None:
        super().__init__(tmp_path, **options)
        self.whatsapp.media["FILE1"] = PDF
        self.whatsapp.media["PHOTO1"] = b"\xff\xd8\xff pretend jpeg"
        self._files = 0

    def send_file(self, **fields) -> list[str]:
        before = len(self.whatsapp.bodies)
        self._files += 1
        self.run(self.assistant.handle(file_message(message_id=f"wamid.f{self._files}", **fields)))
        return self.whatsapp.bodies[before:]

    @property
    def vault(self) -> FolderVault:
        return FolderVault(self.vaults / "user-1")

    @property
    def stored(self) -> list[str]:
        folder = self.vaults / "user-1" / "Files"
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []

    @property
    def index(self) -> str:
        return self.vault.read("Files/Index.md")


# Saving a file sent on WhatsApp ------------------------------------------------------------


def test_m6_a_pdf_sent_with_save_this_as_my_lease_lands_in_files_and_the_index(tmp_path):
    world = World(tmp_path)

    [reply] = world.send_file(caption="save this as my lease")

    assert (tmp_path / "user-1" / "Files" / "My lease.pdf").read_bytes() == PDF
    assert reply == 'Saved to Files: "My lease.pdf" (22 bytes). It is listed in Files/Index.'
    assert world.index == (
        "# Files Tanios saved\n\n"
        "One line per file, oldest first. Tanios adds a line each time it saves one.\n\n"
        "- 2026-11-15 [[My lease.pdf]] (PDF, 22 bytes, From WhatsApp)\n"
    )


def test_m6_saving_a_file_uses_no_model_and_costs_nothing(tmp_path):
    world = World(tmp_path, cap="0.01")
    world.say("what is the capital of Jordan?")  # uses up the cap
    calls = len(world.model.calls)

    world.send_file(caption="save this as my lease")

    assert "My lease.pdf" in world.stored
    assert len(world.model.calls) == calls
    assert len(world.store.ledger) == calls


def test_m8_saving_a_file_is_recorded_in_the_log(tmp_path):
    world = World(tmp_path)

    world.send_file(caption="save this as my lease")

    assert '- 12:00 Saved "My lease.pdf" to Files' in world.log


def test_m6_a_file_with_save_and_no_name_keeps_its_own_name_cleaned_up(tmp_path):
    world = World(tmp_path)

    [reply] = world.send_file(caption="save", filename="Lease_scan (final)#2.pdf")

    assert world.stored == ["About Files.md", "Index.md", "Lease_scan (final) 2.pdf"]
    assert '"Lease_scan (final) 2.pdf"' in reply


def test_m6_a_photo_has_no_name_of_its_own_so_it_gets_a_dated_one(tmp_path):
    world = World(tmp_path)

    world.send_file(media_id="PHOTO1", kind="image", mime_type="image/jpeg", caption="keep this")

    assert "Picture 2026-11-15 1200.jpg" in world.stored
    assert "[[Picture 2026-11-15 1200.jpg]] (picture," in world.index


def test_m6_a_second_file_with_the_same_name_never_replaces_the_first(tmp_path):
    world = World(tmp_path)
    world.whatsapp.media["FILE2"] = b"%PDF-1.7 a different lease"

    world.send_file(caption="save this as my lease")
    [reply] = world.send_file(media_id="FILE2", caption="save this as my lease")

    files = tmp_path / "user-1" / "Files"
    assert (files / "My lease.pdf").read_bytes() == PDF
    assert (files / "My lease 2.pdf").read_bytes() == b"%PDF-1.7 a different lease"
    assert '"My lease 2.pdf"' in reply
    assert world.index.count("\n- ") == 2


def test_m6_a_file_sent_without_save_is_held_not_saved_and_tanios_says_how_to_keep_it(tmp_path):
    world = World(tmp_path)

    [reply] = world.send_file()

    assert reply.startswith("Got your PDF. I cannot read files yet")
    assert "save this as my lease" in reply
    assert "Index.md" not in world.stored
    assert world.whatsapp.downloads == []  # not even fetched until asked


def test_m6_the_held_file_is_saved_when_the_user_then_says_save_this_as(tmp_path):
    world = World(tmp_path)
    world.send_file()

    [reply] = world.say("Save this as my lease")

    assert reply == 'Saved to Files: "My lease.pdf" (22 bytes). It is listed in Files/Index.'
    assert (tmp_path / "user-1" / "Files" / "My lease.pdf").read_bytes() == PDF
    assert world.say("save this as my lease again") == [replies.NO_FILE_WAITING]  # only once


def test_m6_a_caption_that_is_not_a_save_request_does_not_save_or_reach_a_model(tmp_path):
    world = World(tmp_path)

    [reply] = world.send_file(caption="what does this contract say about the deposit?")

    assert reply.startswith("Got your PDF. I cannot read files yet")
    assert "Index.md" not in world.stored
    assert world.model.calls == []


def test_m6_a_held_file_is_dropped_after_an_hour(tmp_path):
    world = World(tmp_path)
    world.send_file()
    world.later(minutes=61)

    assert world.say("save this as my lease") == [replies.NO_FILE_WAITING]
    assert "Index.md" not in world.stored


def test_m6_save_this_with_no_file_explains_what_to_do(tmp_path):
    world = World(tmp_path)

    assert world.say("save this as my lease") == [replies.NO_FILE_WAITING]


def test_w2_m6_a_spoken_save_this_as_saves_the_held_file_and_confirms_by_voice(tmp_path):
    world = World(tmp_path, hears="Save this as my lease.")
    world.send_file()

    world.run(world.assistant.handle(voice_message()))

    assert "My lease.pdf" in world.stored
    assert world.whatsapp.voices[-1][1] == b"OGG:mp3:Saved your PDF to Files as My lease.pdf."


@pytest.mark.parametrize(
    ("filename", "mime_type", "said"),
    [
        ("setup.exe", "application/x-msdownload", '".exe" files'),
        ("invoice.pdf.js", "application/pdf", '".js" files'),
        ("page.html", "text/html", '".html" files'),
        ("notes.md", "text/markdown", '".md" files'),
        ("archive.zip", "application/zip", '".zip" files'),
        ("picture.svg", "image/svg+xml", '".svg" files'),
        (None, "application/octet-stream", "files of that type"),
    ],
)
def test_m6_programs_scripts_web_pages_and_markdown_are_never_saved(
    tmp_path, filename, mime_type, said
):
    world = World(tmp_path)

    [reply] = world.send_file(filename=filename, mime_type=mime_type, caption="save this as x")

    assert reply == f"I do not save {said}. I keep documents, pictures, audio and video."
    assert world.stored in ([], ["About Files.md"])
    assert world.whatsapp.downloads == []


def test_m6_a_file_that_claims_to_be_a_pdf_but_is_named_exe_is_refused(tmp_path):
    world = World(tmp_path)

    [reply] = world.send_file(filename="lease.exe", mime_type="application/pdf", caption="save")

    assert reply.startswith('I do not save ".exe" files.')


def test_m6_a_name_the_user_types_cannot_put_the_file_outside_the_files_folder(tmp_path):
    world = World(tmp_path)

    world.send_file(caption="save this as ../../../etc/cron.d/job")
    world.send_file(caption="save this as /tmp/owned")
    world.send_file(caption="save this as .hidden")
    world.send_file(caption="save this as Memory/Planted note.md")

    assert world.stored == [
        "About Files.md",
        "Etc cron.d job.pdf",
        "Hidden.pdf",
        "Index.md",
        "Memory Planted note.md.pdf",
        "Tmp owned.pdf",
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["user-1"]
    assert not (tmp_path / "user-1" / "Memory" / "Planted note.md").exists()


def test_m6_a_files_own_name_cannot_put_it_outside_the_files_folder_either(tmp_path):
    world = World(tmp_path)

    world.send_file(caption="save", filename="../../Instructions.pdf")

    assert world.stored == ["About Files.md", "Index.md", "Instructions.pdf"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["user-1"]


def test_m6_a_file_over_the_size_limit_is_not_saved(tmp_path):
    world = World(tmp_path)
    world.assistant._max_file_bytes = 10

    [reply] = world.send_file(caption="save this as my lease")

    assert reply == "That file is too large. The most I save is 10 bytes."
    assert "Index.md" not in world.stored


def test_m6_if_whatsapp_cannot_hand_over_the_file_nothing_is_claimed(tmp_path):
    world = World(tmp_path)

    [reply] = world.send_file(media_id="GONE", caption="save this as my lease")

    assert reply == replies.FILE_FETCH_FAILED
    assert "Index.md" not in world.stored


def test_m6_a_vault_problem_is_reported_and_the_file_stays_ready_to_try_again(tmp_path):
    world = World(tmp_path)
    world.send_file()
    world.memory_for(1).ensure_layout()
    folder = tmp_path / "user-1" / "Files"
    shutil.rmtree(folder)
    folder.write_text("a file where the folder should be")

    assert world.say("save this as my lease") == [replies.FILE_NOT_SAVED]

    folder.unlink()
    assert world.say("save this as my lease")[0].startswith('Saved to Files: "My lease.pdf"')


def test_m6_if_the_index_cannot_be_written_the_file_is_taken_back_out(tmp_path):
    world = World(tmp_path)
    world.memory_for(1).ensure_layout()
    (tmp_path / "user-1" / "Files" / "Index.md").mkdir()  # the index cannot be a note now

    [reply] = world.send_file(caption="save this as my lease")

    assert reply == replies.FILE_NOT_SAVED
    assert not (tmp_path / "user-1" / "Files" / "My lease.pdf").exists()
    assert "Saved" not in world.log


def test_w4_while_paused_a_file_is_neither_held_nor_saved(tmp_path):
    world = World(tmp_path)
    world.say("pause")

    assert world.send_file(caption="save this as my lease") == [replies.PAUSED_REPLY]
    world.say("resume")
    assert world.say("save this as my lease") == [replies.NO_FILE_WAITING]


def test_w3_a_strangers_file_is_never_fetched_or_saved(tmp_path):
    world = World(tmp_path)
    before = len(world.whatsapp.bodies)

    world.run(
        world.assistant.handle(file_message(sender=STRANGER, caption="save this as my lease"))
    )

    assert world.whatsapp.bodies[before:] == [replies.STRANGER_REPLY]
    assert world.whatsapp.downloads == [] and "Index.md" not in world.stored


def test_m1_one_users_held_file_cannot_be_saved_by_another_user(tmp_path):
    world = World(tmp_path)
    world.store.add_user("962790000002")
    world.send_file()
    before = len(world.whatsapp.bodies)

    world.run(
        world.assistant.handle(
            text_message("save this as mine", sender="962790000002", message_id="wamid.x")
        )
    )

    assert world.whatsapp.bodies[before:] == [replies.NO_FILE_WAITING]
    assert not (tmp_path / "user-2" / "Files" / "Mine.pdf").exists()


def test_m4_m6_the_index_lets_a_later_question_find_the_file(tmp_path):
    world = World(tmp_path, gateway=FakeGateway(answer="It is in Files.\nNOTES: Index"))
    world.send_file(caption="save this as my lease")

    [reply] = world.say("where is my lease?")

    assert "[[My lease.pdf]]" in world.model.calls[-1]["user"]
    assert reply == "It is in Files.\n\nFrom your notes: Index"
    assert PDF.decode() not in world.model.calls[-1]["user"]  # the file itself is never read


def test_m6_file_names_and_contents_never_reach_the_server_log(tmp_path, caplog):
    world = World(tmp_path)

    with caplog.at_level("DEBUG"):
        world.send_file(caption="save this as my lease", filename="secret-plan.pdf")

    for private in ("lease", "secret-plan", "pretend"):
        assert private not in caplog.text


# Saving a named Gmail attachment ------------------------------------------------------------


def lease_email(**fields):
    attachments = (Attachment("lease-scan.pdf", "application/pdf", len(PDF), "ATT-1"),)
    return an_email("m1", "Invoice for October", attachments=attachments, **fields)


def test_m6_the_attachment_of_a_listed_email_is_saved_with_the_name_the_user_gives(tmp_path):
    world = World(tmp_path)
    world.gmail.files["ATT-1"] = PDF
    world.gmail.deliver(lease_email())
    world.say("what's new?")

    [reply] = world.say("save the attachment from 1 as my lease")

    assert (tmp_path / "user-1" / "Files" / "My lease.pdf").read_bytes() == PDF
    assert reply == 'Saved to Files: "My lease.pdf" (22 bytes). It is listed in Files/Index.'
    assert "[[My lease.pdf]] (PDF, 22 bytes, From an email by Rana Haddad)" in world.index
    assert '- 12:00 Saved "My lease.pdf" to Files' in world.log


def test_m6_saving_an_attachment_changes_nothing_in_gmail_and_uses_no_model(tmp_path):
    world = World(tmp_path)
    world.gmail.files["ATT-1"] = PDF
    world.gmail.deliver(lease_email())
    world.say("what's new?")
    calls, gmail_calls = len(world.model.calls), len(world.gmail.calls)

    world.say("save the attachment from 1 as my lease")

    assert len(world.model.calls) == calls
    assert world.gmail.calls[gmail_calls:] == ["message", "attachment"]


def test_m6_with_several_attachments_the_user_names_the_one_to_save(tmp_path):
    world = World(tmp_path)
    world.gmail.files.update({"ATT-1": PDF, "ATT-2": b"\x89PNG pretend"})
    attachments = (
        Attachment("lease-scan.pdf", "application/pdf", 22, "ATT-1"),
        Attachment("floor plan.png", "image/png", 12, "ATT-2"),
    )
    world.gmail.deliver(an_email("m1", "Invoice for October", attachments=attachments))
    world.say("what's new?")

    [ask] = world.say("save the attachment from 1")
    assert ask == (
        "Email 1 has 2 attachments:\n"
        "- lease-scan.pdf (22 bytes)\n"
        "- floor plan.png (12 bytes)\n"
        "Say which one, for example: save lease-scan.pdf from 1"
    )
    assert "Index.md" not in world.stored

    [reply] = world.say("save floor plan.png from 1")
    assert reply.startswith('Saved to Files: "Floor plan.png"')
    assert (tmp_path / "user-1" / "Files" / "Floor plan.png").read_bytes() == b"\x89PNG pretend"


def test_m6_an_attachment_on_a_suspicious_email_is_never_saved(tmp_path):
    world = World(tmp_path)
    world.gmail.files["ATT-1"] = PDF
    world.gmail.deliver(
        an_email(
            "m1",
            "Verify your password",
            attachments=(Attachment("form.pdf", "application/pdf", 22, "ATT-1"),),
        )
    )
    world.say("what's new?")

    assert world.say("save the attachment from 1 as form") == [mail.SUSPICIOUS_NO_SAVE]
    assert "attachment" not in world.gmail.calls
    assert "Index.md" not in world.stored


@pytest.mark.parametrize("filename", ["update.exe", "macro.docm", "page.html", "notes.md", "a.zip"])
def test_m6_an_attachment_of_a_type_tanios_does_not_keep_is_refused(tmp_path, filename):
    world = World(tmp_path)
    world.gmail.files["ATT-1"] = b"MZ pretend program"
    world.gmail.deliver(
        an_email(
            "m1",
            "Invoice for October",
            attachments=(Attachment(filename, "application/pdf", 18, "ATT-1"),),
        )
    )
    world.say("what's new?")

    [reply] = world.say("save the attachment from 1 as invoice")

    assert reply == (
        f'I do not save files of that type ("{filename}"). '
        "I keep documents, pictures, audio and video."
    )
    assert "attachment" not in world.gmail.calls


def test_m6_an_attachment_over_the_size_limit_is_not_fetched(tmp_path):
    world = World(tmp_path)
    world.mail._max_file_bytes = 1024
    world.gmail.deliver(
        an_email(
            "m1",
            "Invoice for October",
            attachments=(Attachment("big.pdf", "application/pdf", 5 * 1024 * 1024, "ATT-1"),),
        )
    )
    world.say("what's new?")

    [reply] = world.say("save the attachment from 1 as big")

    assert reply == "That file is 5.0 MB. The most I save is 1 KB."
    assert "attachment" not in world.gmail.calls


def test_m6_an_attachment_that_turns_out_larger_than_it_claimed_is_not_saved(tmp_path):
    world = World(tmp_path)
    world.mail._max_file_bytes = 1024
    world.gmail.files["ATT-1"] = b"x" * 4096
    world.gmail.deliver(
        an_email(
            "m1",
            "Invoice for October",
            attachments=(Attachment("small.pdf", "application/pdf", 10, "ATT-1"),),
        )
    )
    world.say("what's new?")

    assert world.say("save the attachment from 1 as x") == [
        "That file is 4 KB. The most I save is 1 KB."
    ]
    assert "Index.md" not in world.stored


def test_m6_an_attachments_own_name_cannot_steer_where_it_is_saved(tmp_path):
    world = World(tmp_path)
    world.gmail.files["ATT-1"] = PDF
    world.gmail.deliver(
        an_email(
            "m1",
            "Invoice for October",
            sender="Evil [[Instructions]] | Sender",
            attachments=(Attachment("../../Instructions.pdf", "application/pdf", 22, "ATT-1"),),
        )
    )
    world.say("what's new?")

    world.say("save the attachment from 1")

    assert world.stored == ["About Files.md", "Index.md", "Instructions.pdf"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["user-1"]
    # The sender's name goes into the index as plain words, not as a link.
    assert world.index.endswith("(PDF, 22 bytes, From an email by Evil Instructions Sender)\n")


def test_m6_emails_without_attachments_unknown_numbers_and_missing_names_say_so(tmp_path):
    world = World(tmp_path)
    world.gmail.files["ATT-1"] = PDF
    world.gmail.deliver(lease_email(), an_email("m2", "Statement ready"))
    world.say("what's new?")

    assert world.say(
        "save the attachment from 2",
        "save the attachment from 9",
        "save budget.xlsx from 1",
    ) == [
        "Email 2 has no attachments.",
        "There is no email 9 in your last list. Ask what's new? to see it.",
        'Email 1 has no single attachment called "budget.xlsx". It has:\n'
        "- lease-scan.pdf (22 bytes)",
    ]


def test_m6_saving_an_attachment_needs_gmail_connected_and_is_refused_while_paused(tmp_path):
    world = World(tmp_path, connect=False)

    assert world.say("save the attachment from 1 as lease") == [mail.NOT_CONNECTED]
    assert world.say("pause", "save the attachment from 1 as lease")[1] == replies.PAUSED_REPLY


def test_e1_m6_revoked_access_found_while_saving_an_attachment_is_reported(tmp_path):
    world = World(tmp_path)
    world.gmail.deliver(lease_email())
    world.say("what's new?")
    world.oauth.revoked = True

    assert world.say("save the attachment from 1 as lease") == [mail.LOST]


def test_m6_without_a_vault_an_attachment_cannot_be_saved():
    world = MailWorld()  # email on, memory off
    world.gmail.deliver(lease_email())
    world.say("what's new?")

    assert world.say("save the attachment from 1 as lease") == [mail.FILES_NEED_MEMORY]


# The rules, one by one ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "wish"),
    [
        ("save this as my lease", FileRequest("my lease")),
        ("Save this as my lease.", FileRequest("my lease")),
        ('Save this as "My Lease"', FileRequest("My Lease")),
        ("save", FileRequest("")),
        ("Save it", FileRequest("")),
        ("keep this", FileRequest("")),
        ("save this pdf as Lease 2026.pdf", FileRequest("Lease 2026")),
        ("Hey Tanios, please save the photo under passport", FileRequest("passport")),
        ("file this as tax return", FileRequest("tax return")),
        ("save as lease", FileRequest("lease")),
        ("save me a seat", None),
        ("save the date", None),
        ("what is this?", None),
        ("this is my lease", None),
        ("can you save this for later reading", None),
        ("", None),
        (None, None),
    ],
)
def test_m6_a_save_request_is_recognised_from_the_whole_caption_without_a_model(said, wish):
    assert parse_file_request(said) == wish


@pytest.mark.parametrize(
    ("said", "request_"),
    [
        (
            "save the attachment from 2 as my lease",
            MailRequest("attachment", number=2, text="my lease"),
        ),
        ("Save attachment from email 3.", MailRequest("attachment", number=3)),
        ("save the pdf in 1", MailRequest("attachment", number=1)),
        (
            "save lease-scan.pdf from 2 as my lease",
            MailRequest("attachment", number=2, text="my lease", which="lease-scan.pdf"),
        ),
        (
            'save "Offer letter.docx" from 1',
            MailRequest("attachment", number=1, which="Offer letter.docx"),
        ),
        ("save money from 2 jobs", None),
        ("save 2 seats for me from 6", None),
        ("save energy in 5 ways", None),
        ("save this as my lease", None),
    ],
)
def test_m6_saving_an_attachment_is_recognised_by_the_shape_of_the_message(said, request_):
    assert parse_mail(said) == request_


@pytest.mark.parametrize(
    ("filename", "mime_type", "extension"),
    [
        ("Lease.PDF", "application/pdf", "pdf"),
        ("photo.JPG", None, "jpg"),
        (None, "image/jpeg", "jpg"),
        (None, "IMAGE/PNG; charset=binary", "png"),
        ("report", "application/pdf", "pdf"),  # no extension in the name: go by type
        ("report.final.docx", None, "docx"),
        ("run.sh", "text/plain", None),
        ("lease.pdf.exe", "application/pdf", None),
        ("script.py", "text/plain", None),
        ("page.htm", "text/html", None),
        (None, None, None),
        (None, "application/x-sh", None),
    ],
)
def test_m6_the_saved_extension_comes_from_a_short_list_of_ordinary_file_types(
    filename, mime_type, extension
):
    assert safe_extension(filename, mime_type) == extension


def test_m6_nothing_that_can_run_or_pass_as_a_note_is_on_the_list():
    for risky in (
        "exe",
        "js",
        "sh",
        "bat",
        "cmd",
        "html",
        "htm",
        "svg",
        "md",
        "zip",
        "docm",
        "py",
        "jar",
        "apk",
        "dmg",
        "iso",
        "lnk",
        "scr",
        "php",
    ):
        assert risky not in KINDS


def test_m6_only_a_known_extension_is_taken_off_a_typed_name():
    assert without_extension("Lease 2026.pdf") == "Lease 2026"
    assert without_extension("Dr. Haddad v2.0") == "Dr. Haddad v2.0"
    assert without_extension("lease") == "lease"


def test_readable_sizes():
    assert [readable_size(n) for n in (0, 900, 2048, 1_300_000, 25 * 1024 * 1024)] == [
        "0 bytes",
        "900 bytes",
        "2 KB",
        "1.2 MB",
        "25.0 MB",
    ]


def test_m6_the_vault_refuses_a_file_type_even_if_asked_directly(tmp_path):
    memory = Memory(FolderVault(tmp_path / "user-1"), now=lambda: NOW)

    for extension in ("exe", "md", "../pdf", ""):
        with pytest.raises(VaultError):
            memory.keep_file(b"x", name="thing", extension=extension, origin="from a test")

    assert not (tmp_path / "user-1" / "Files" / "Index.md").exists()


def test_m6_bytes_are_stored_exactly_and_no_temporary_file_is_left(tmp_path):
    memory = Memory(FolderVault(tmp_path / "user-1"), now=lambda: NOW)
    data = bytes(range(256)) * 40

    saved = memory.keep_file(data, name="scan", extension="png", origin="from WhatsApp")

    assert (saved.name, saved.path, saved.size) == ("Scan.png", "Files/Scan.png", 10240)
    assert (tmp_path / "user-1" / "Files" / "Scan.png").read_bytes() == data
    assert sorted(p.name for p in (tmp_path / "user-1" / "Files").iterdir()) == [
        "About Files.md",
        "Index.md",
        "Scan.png",
    ]


def test_m6_the_index_keeps_what_the_user_wrote_in_it(tmp_path):
    vault = FolderVault(tmp_path / "user-1")
    memory = Memory(vault, now=lambda: NOW)
    memory.keep_file(b"1", name="one", extension="pdf", origin="from WhatsApp")
    vault.append("Files/Index.md", "My own note about these files.\n")

    memory.keep_file(b"2", name="two", extension="pdf", origin="from WhatsApp")

    lines = vault.read("Files/Index.md").splitlines()
    assert lines[-3:] == [
        "- 2026-11-15 [[One.pdf]] (PDF, 1 bytes, From WhatsApp)",
        "My own note about these files.",
        "- 2026-11-15 [[Two.pdf]] (PDF, 1 bytes, From WhatsApp)",
    ]


def test_m6_many_saves_at_once_all_get_their_own_name(tmp_path):
    memory = Memory(FolderVault(tmp_path / "user-1"), now=lambda: NOW)

    async def scenario():
        await asyncio.gather(
            *(
                asyncio.to_thread(
                    memory.keep_file,
                    str(n).encode(),
                    name="lease",
                    extension="pdf",
                    origin="from WhatsApp",
                )
                for n in range(12)
            )
        )

    asyncio.run(scenario())

    files = [p for p in (tmp_path / "user-1" / "Files").iterdir() if p.suffix == ".pdf"]
    assert len(files) == 12
    assert sorted(p.read_bytes() for p in files) == sorted(str(n).encode() for n in range(12))
    assert FolderVault(tmp_path / "user-1").read("Files/Index.md").count("\n- ") == 12


def test_m6_a_held_file_expires_exactly_on_the_hour_boundary(tmp_path):
    world = World(tmp_path)
    world.send_file()
    world.clock += timedelta(minutes=60)

    assert world.say("save this as my lease")[0].startswith("Saved to Files")


# WhatsApp and Gmail plumbing --------------------------------------------------------------


def test_m6_a_document_webhook_carries_its_caption_name_and_type():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "1234567890"},
                            "messages": [
                                {
                                    "from": "962790000001",
                                    "id": "wamid.doc",
                                    "type": "document",
                                    "document": {
                                        "id": "9001",
                                        "filename": "scan0001.pdf",
                                        "mime_type": "application/pdf",
                                        "caption": "save this as my lease",
                                        "sha256": "abc",
                                    },
                                },
                                {
                                    "from": "962790000001",
                                    "id": "wamid.img",
                                    "type": "image",
                                    "image": {
                                        "id": "9002",
                                        "mime_type": "image/jpeg",
                                        "caption": 7,
                                    },
                                },
                            ],
                        },
                    }
                ]
            }
        ],
    }

    document, image = parse_webhook(payload)

    assert (document.kind, document.media_id) == ("document", "9001")
    assert (document.caption, document.filename, document.mime_type) == (
        "save this as my lease",
        "scan0001.pdf",
        "application/pdf",
    )
    assert (image.kind, image.media_id, image.mime_type) == ("image", "9002", "image/jpeg")
    assert image.caption is None and image.filename is None  # a caption that is not text


def test_m6_attachments_are_read_from_an_email_by_name_size_and_id_not_content():
    def b64(text):
        return base64.urlsafe_b64encode(text.encode()).decode()

    parsed = parse_message(
        {
            "id": "m1",
            "threadId": "t1",
            "internalDate": "1794744000000",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [{"name": "From", "value": "rana@example.com"}],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("See attached.")}},
                    {
                        "mimeType": "application/pdf",
                        "filename": "lease\r\nscan.pdf",
                        "body": {"attachmentId": "ANGjdJ_long-id", "size": 48213},
                    },
                    {"mimeType": "image/png", "filename": "", "body": {"attachmentId": "inline"}},
                    {"mimeType": "image/png", "filename": "logo.png", "body": {"size": 10}},
                ],
            },
        }
    )

    assert parsed.text == "See attached."
    assert parsed.attachments == (
        Attachment("lease scan.pdf", "application/pdf", 48213, "ANGjdJ_long-id"),
    )


def test_m6_fetching_an_attachment_is_one_read_call_that_returns_its_bytes():
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        data = base64.urlsafe_b64encode(PDF).decode().rstrip("=")
        return httpx2.Response(200, json={"size": len(PDF), "data": data})

    async def token(fresh: bool = False) -> str:
        return "token-0"

    client = GmailApi(
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        api_base="https://gmail.example",
        access_token=token,
    )
    long_id = "ANGjdJ-" + "x" * 400

    assert asyncio.run(client.attachment("18c2a", long_id)) == PDF
    [request] = requests
    assert request.method == "GET"
    assert request.url.path == f"/gmail/v1/users/me/messages/18c2a/attachments/{long_id}"

    for bad in ("../../settings/filters", "a/b", "", "x" * 2001):
        with pytest.raises(GmailError):
            asyncio.run(client.attachment("18c2a", bad))
    assert len(requests) == 1


def test_m6_an_unreadable_attachment_answer_is_a_plain_error():
    for body in ({}, {"data": ""}, {"data": 5}, ["list"]):

        def handler(request: httpx2.Request, body=body) -> httpx2.Response:
            return httpx2.Response(200, content=json.dumps(body))

        async def token(fresh: bool = False) -> str:
            return "token-0"

        client = GmailApi(
            httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            api_base="https://gmail.example",
            access_token=token,
        )
        with pytest.raises(GmailError):
            asyncio.run(client.attachment("18c2a", "ATT"))


def test_m6_reading_an_attachment_is_allowed_and_nothing_else_about_attachments_is():
    assert allowed("GET", "/messages/18c2a/attachments/ANGjdJ_x-1") is True
    assert allowed("DELETE", "/messages/18c2a/attachments/ANGjdJ_x-1") is False
    assert allowed("POST", "/messages/18c2a/attachments/ANGjdJ_x-1") is False
    assert allowed("GET", "/messages/18c2a/attachments/a/b") is False
    # Eleven calls for mail, and this one. A thirteenth needs a deliberate change here.
    assert len(ALLOWED_CALLS) == 12


def test_m6_picking_an_attachment_by_name():
    files = (
        Attachment("lease-scan.pdf", "application/pdf", 10, "A"),
        Attachment("Lease terms.docx", "application/msword", 10, "B"),
    )

    assert pick_attachment(files, "LEASE-SCAN.PDF", 1) == files[0]
    assert pick_attachment(files, "terms", 1) == files[1]
    assert isinstance(pick_attachment(files, "lease", 1), str)  # matches both: ask
    assert isinstance(pick_attachment(files, "", 1), str)
    assert pick_attachment(files[:1], "", 1) == files[0]
    assert pick_attachment((), "", 4) == "Email 4 has no attachments."


def test_m6_a_download_respects_the_size_limit_it_is_given():
    hosts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        hosts.append(request.url.host)
        if request.url.host == "graph.example":
            return httpx2.Response(
                200, json={"url": "https://lookaside.fbsbx.com/x", "file_size": 20 * 1024 * 1024}
            )
        return httpx2.Response(200, content=b"x" * 2048)

    client = whatsapp_client(handler)

    # A 20 MB file: refused at the default limit, and at a smaller one, before fetching.
    for limit in ({}, {"max_bytes": 1024}):
        with pytest.raises(MediaTooLarge):
            asyncio.run(client.download_media("123", **limit))
    assert hosts == ["graph.example"] * 2

    # Allowed when the caller's limit is higher than the default.
    assert len(asyncio.run(client.download_media("123", max_bytes=25 * 1024 * 1024))) == 2048


def test_m6_a_file_that_is_larger_than_whatsapp_said_is_refused_after_fetching():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "graph.example":
            return httpx2.Response(
                200, json={"url": "https://lookaside.fbsbx.com/x", "file_size": 10}
            )
        return httpx2.Response(200, content=b"x" * 4096)

    with pytest.raises(MediaTooLarge):
        asyncio.run(whatsapp_client(handler).download_media("123", max_bytes=1024))


def test_m6_the_file_size_limit_is_a_setting_with_sensible_bounds(monkeypatch):
    assert Settings(_env_file=None).max_file_mb == 25
    monkeypatch.setenv("TANIOS_MAX_FILE_MB", "5")
    assert Settings(_env_file=None).max_file_mb == 5
    for bad in ("0", "500"):
        monkeypatch.setenv("TANIOS_MAX_FILE_MB", bad)
        with pytest.raises(ValueError):
            Settings(_env_file=None)
