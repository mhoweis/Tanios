"""Milestone 4: what Tanios keeps in its vault, with no model involved (M1 to M5, M7, M8)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from tanios.memory import (
    INSTRUCTIONS,
    INSTRUCTIONS_TEMPLATE,
    MARKER,
    MAX_CONTEXT_CHARS,
    Memories,
    Memory,
    note_title,
    words,
)
from tanios.vault import FolderVault

MORNING = datetime(2026, 11, 15, 9, 5, tzinfo=UTC)


@pytest.fixture
def vault(tmp_path):
    return FolderVault(tmp_path / "user-1")


@pytest.fixture
def memory(vault):
    return Memory(vault, now=lambda: MORNING)


# M1: its own vault with a fixed layout -------------------------------------------------


def test_m1_the_vault_has_instructions_and_the_four_folders(memory, tmp_path):
    memory.ensure_layout()

    root = tmp_path / "user-1"
    assert (root / "Instructions.md").is_file()
    for folder in ("Memory", "Inbox", "Files", "Log"):
        assert (root / folder).is_dir()
    assert sorted(path.name for path in root.iterdir()) == [
        "Files",
        "Inbox",
        "Instructions.md",
        "Log",
        "Memory",
    ]


def test_m1_laying_out_the_vault_again_never_overwrites_what_the_user_wrote(memory, vault):
    memory.ensure_layout()
    vault.write(INSTRUCTIONS, "Always answer in bullet points.")
    vault.write("Memory/About Memory.md", "my own words")

    memory.ensure_layout()

    assert vault.read(INSTRUCTIONS) == "Always answer in bullet points."
    assert vault.read("Memory/About Memory.md") == "my own words"


def test_m1_each_user_gets_a_separate_vault_folder(tmp_path):
    memories = Memories(tmp_path, ZoneInfo("UTC"))

    memories(1).remember("my accountant is Rana")
    memories(2).ensure_layout()

    assert (tmp_path / "user-1" / "Memory" / "My accountant is Rana.md").is_file()
    assert memories(2).find("who is my accountant") == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["user-1", "user-2"]


def test_m1_dates_follow_the_configured_time_zone(tmp_path):
    amman = Memories(tmp_path, ZoneInfo("Asia/Amman"))(1)

    note = amman.capture("call the bank")

    today = datetime.now(ZoneInfo("Asia/Amman"))
    assert note.name.startswith(f"{today:%Y-%m-%d} ")


# M2: remember --------------------------------------------------------------------------


def test_m2_a_fact_is_saved_as_one_note_in_memory_named_after_it(memory, vault):
    note = memory.remember("my accountant is Rana")

    assert note.name == "My accountant is Rana"
    assert note.path == "Memory/My accountant is Rana.md"
    saved = vault.read(note.path)
    assert saved.endswith("my accountant is Rana\n")
    assert "created: 2026-11-15T09:05" in saved
    assert "source: whatsapp" in saved


def test_m2_two_memories_with_the_same_words_do_not_overwrite_each_other(memory, vault):
    first = memory.remember("call Rana")
    second = memory.remember("call Rana")

    assert first.path == "Memory/Call Rana.md"
    assert second.path == "Memory/Call Rana 2.md"
    assert len(vault.notes("Memory")) == 3  # two memories and the About note


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("my accountant is Rana", "My accountant is Rana"),
        ("wifi: home/office #1 [router]?", "Wifi home office 1 router"),
        ("   ", "Note"),
        ("first line\nsecond line", "First line"),
        ("../../etc/passwd", "Etc passwd"),
        (".hidden", "Hidden"),
    ],
)
def test_m2_a_note_name_is_safe_on_every_device(text, title):
    assert note_title(text) == title


def test_m2_a_long_fact_gets_a_short_name_cut_at_a_word(memory):
    note = memory.remember("the landlord said the rent goes up by five percent from January " * 3)

    assert len(note.name) <= 60
    assert note.name == "The landlord said the rent goes up by five percent from"


# M3: capture ---------------------------------------------------------------------------


def test_m3_a_quick_note_lands_in_inbox_with_the_date(memory, vault):
    note = memory.capture("call the bank about the transfer")

    assert note.path == "Inbox/2026-11-15 Call the bank about the transfer.md"
    assert vault.read(note.path).endswith("call the bank about the transfer\n")


# M4: find ------------------------------------------------------------------------------


def test_m4_a_question_finds_the_note_that_answers_it(memory):
    memory.remember("my accountant is Rana")
    memory.remember("the gym closes at ten")

    found = memory.find("who is my accountant?")

    assert [note.name for note in found] == ["My accountant is Rana"]
    assert found[0].text == "my accountant is Rana"  # the properties block is left out


def test_m4_notes_the_user_wrote_in_obsidian_are_found_too(memory, vault):
    memory.ensure_layout()
    vault.write("Projects/Villa.md", "The villa contractor is Samir, phone 0790000000.")

    assert [note.path for note in memory.find("who is the villa contractor")] == [
        "Projects/Villa.md"
    ]


def test_m4_a_word_in_the_title_counts_for_more_than_a_word_in_the_text(memory, vault):
    vault.write("Memory/Trip.md", "We talked about the passport once.")
    vault.write("Memory/Passport.md", "Expires in March 2031.")

    assert [note.name for note in memory.find("when does my passport expire")][0] == "Passport"


def test_m4_plurals_match_and_small_words_are_ignored():
    assert words("Who are my accountants?") == ["accountant"]
    assert words("the of and to") == []


def test_m4_the_log_the_instructions_and_the_about_notes_are_never_offered(memory, vault):
    memory.remember("my accountant is Rana")  # also writes a log line naming the note
    vault.write(INSTRUCTIONS, "My accountant prefers email.")

    paths = [note.path for note in memory.find("accountant memory inbox remember")]

    assert paths == ["Memory/My accountant is Rana.md"]


def test_m4_nothing_is_offered_when_nothing_matches(memory):
    memory.remember("my accountant is Rana")

    assert memory.find("what is the capital of Jordan?") == []
    assert memory.find("?") == []


def test_m4_what_is_offered_to_the_model_is_capped(memory, vault):
    for number in range(8):
        vault.write(f"Memory/Lease {number}.md", "lease " * 400)

    found = memory.find("lease")

    assert 1 <= len(found) <= 4
    assert sum(len(note.text) for note in found) <= MAX_CONTEXT_CHARS


# M5: Instructions ----------------------------------------------------------------------


def test_m5_the_untouched_starter_note_counts_as_no_instructions(memory):
    memory.ensure_layout()

    assert memory.instructions() == ""


def test_m5_what_the_user_writes_under_the_line_is_their_instructions(memory, vault):
    memory.ensure_layout()
    vault.write(INSTRUCTIONS, INSTRUCTIONS_TEMPLATE + "Always answer in bullet points.\n")

    assert memory.instructions() == "Always answer in bullet points."


def test_m5_if_the_user_replaces_the_whole_note_all_of_it_counts(memory, vault):
    vault.write(INSTRUCTIONS, "Keep answers under two sentences.")

    assert MARKER not in vault.read(INSTRUCTIONS)
    assert memory.instructions() == "Keep answers under two sentences."


def test_m5_no_instructions_note_means_no_instructions(memory):
    assert memory.instructions() == ""


# M7: forget ----------------------------------------------------------------------------


def test_m7_forget_deletes_the_one_matching_memory_and_reports_it(memory, vault):
    memory.remember("my accountant is Rana")
    memory.remember("the gym closes at ten")

    result = memory.forget("my accountant")

    assert [note.name for note in result.removed] == ["My accountant is Rana"]
    assert not vault.exists("Memory/My accountant is Rana.md")
    assert vault.exists("Memory/The gym closes at ten.md")


def test_m7_when_several_memories_match_nothing_is_deleted(memory, vault):
    memory.remember("Rana is my accountant")
    memory.remember("Rana's office is in Abdoun")

    result = memory.forget("Rana")

    assert result.removed == ()
    assert result.candidates == ("Rana is my accountant", "Rana's office is in Abdoun")
    assert len(vault.notes("Memory")) == 3


def test_m7_the_full_name_picks_one_memory_out_of_several(memory, vault):
    memory.remember("Rana is my accountant")
    memory.remember("Rana's office is in Abdoun")

    result = memory.forget("rana is my accountant")

    assert [note.name for note in result.removed] == ["Rana is my accountant"]
    assert vault.exists("Memory/Rana's office is in Abdoun.md")


def test_m7_forget_only_removes_memories_never_other_notes(memory, vault):
    memory.capture("call the bank about the transfer")
    vault.write("Projects/Bank.md", "bank transfer details")

    result = memory.forget("bank transfer")

    assert result.removed == () and result.candidates == ()
    assert len(vault.notes("Inbox")) == 2
    assert vault.exists("Projects/Bank.md")


def test_m7_forget_with_nothing_to_match_removes_nothing(memory, vault):
    memory.remember("my accountant is Rana")

    assert memory.forget("the").removed == ()
    assert memory.forget("dentist").removed == ()
    assert vault.exists("Memory/My accountant is Rana.md")


# M8: the daily log ---------------------------------------------------------------------


def test_m8_every_save_and_every_forget_gets_one_line_in_todays_log(memory, vault):
    memory.remember("my accountant is Rana")
    memory.capture("call the bank")
    memory.forget("accountant")

    assert vault.read("Log/2026-11-15.md") == (
        "# Tanios log for 15 November 2026\n"
        "\n"
        '- 09:05 Remembered "My accountant is Rana"\n'
        '- 09:05 Noted "2026-11-15 Call the bank" in Inbox\n'
        '- 09:05 Forgot "My accountant is Rana"\n'
    )


def test_m8_a_forget_that_removed_nothing_is_not_logged_as_an_action(memory, vault):
    memory.forget("dentist")

    assert vault.notes("Log") == []


def test_m8_each_day_has_its_own_log_note(vault):
    clock = [MORNING]
    memory = Memory(vault, now=lambda: clock[0])

    memory.remember("one")
    clock[0] = MORNING.replace(day=16)
    memory.remember("two")

    assert vault.notes("Log") == ["Log/2026-11-15.md", "Log/2026-11-16.md"]
