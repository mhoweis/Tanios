"""Milestone 4: Tanios's vault is one folder, and it never reaches outside it (M1)."""

import os

import pytest

from tanios.vault import MAX_NOTE_BYTES, FolderVault, VaultError


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "vault").mkdir()
    return FolderVault(tmp_path / "vault")


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "Memory/../../outside.md",
        "Memory/../Memory/note.md",
        "/etc/passwd",
        ".obsidian/app.json",
        "Memory/.hidden.md",
        "Memory\\..\\outside.md",
        "Memory/bad\x00name.md",
        "",
    ],
)
def test_m1_a_path_that_would_leave_the_vault_or_touch_hidden_files_is_refused(vault, path):
    for action in (
        lambda: vault.read(path),
        lambda: vault.write(path, "x"),
        lambda: vault.append(path, "x"),
        lambda: vault.delete(path),
        lambda: vault.exists(path),
    ):
        with pytest.raises(VaultError):
            action()


def test_m1_nothing_is_written_outside_the_vault_when_a_path_is_refused(vault, tmp_path):
    with pytest.raises(VaultError):
        vault.write("../outside.md", "should never exist")

    assert not (tmp_path / "outside.md").exists()
    assert os.listdir(tmp_path) == ["vault"]


def test_m1_a_link_that_points_out_of_the_vault_cannot_be_read_or_written(vault, tmp_path):
    secret = tmp_path / "personal-vault"
    secret.mkdir()
    (secret / "diary.md").write_text("private diary")
    (tmp_path / "vault" / "Memory").mkdir()
    (tmp_path / "vault" / "Memory" / "link.md").symlink_to(secret / "diary.md")
    (tmp_path / "vault" / "Personal").symlink_to(secret, target_is_directory=True)

    for path in ("Memory/link.md", "Personal/diary.md"):
        with pytest.raises(VaultError):
            vault.read(path)
        with pytest.raises(VaultError):
            vault.write(path, "overwritten")
        with pytest.raises(VaultError):
            vault.delete(path)

    assert (secret / "diary.md").read_text() == "private diary"
    assert vault.notes() == []


def test_a_note_is_written_whole_and_no_temporary_file_is_left(vault, tmp_path):
    vault.write("Memory/Accountant.md", "Rana")
    vault.write("Memory/Accountant.md", "Rana Haddad")

    assert vault.read("Memory/Accountant.md") == "Rana Haddad"
    assert os.listdir(tmp_path / "vault" / "Memory") == ["Accountant.md"]


def test_append_adds_to_the_end_and_creates_the_note_if_needed(vault):
    vault.append("Log/2026-11-15.md", "- one\n")
    vault.append("Log/2026-11-15.md", "- two\n")

    assert vault.read("Log/2026-11-15.md") == "- one\n- two\n"


def test_deleting_removes_the_note_and_a_missing_note_is_an_error(vault):
    vault.write("Memory/Old.md", "x")

    vault.delete("Memory/Old.md")

    assert not vault.exists("Memory/Old.md")
    with pytest.raises(VaultError):
        vault.delete("Memory/Old.md")


def test_reading_a_missing_note_is_a_vault_error(vault):
    with pytest.raises(VaultError):
        vault.read("Memory/Nothing.md")


def test_a_huge_note_is_read_only_up_to_the_limit(vault):
    vault.write("Inbox/Big.md", "a" * (MAX_NOTE_BYTES + 5000))

    assert len(vault.read("Inbox/Big.md")) == MAX_NOTE_BYTES


def test_notes_lists_markdown_files_in_order_and_skips_hidden_and_other_files(vault, tmp_path):
    vault.write("Memory/B.md", "b")
    vault.write("Memory/A.md", "a")
    vault.write("Inbox/2026-11-15 Bank.md", "bank")
    vault.write("Files/lease.pdf", "not a note")
    hidden = tmp_path / "vault" / ".obsidian"
    hidden.mkdir()
    (hidden / "workspace.md").write_text("obsidian's own")
    (tmp_path / "vault" / ".trash.md").write_text("hidden")

    assert vault.notes() == ["Inbox/2026-11-15 Bank.md", "Memory/A.md", "Memory/B.md"]
    assert vault.notes("Memory") == ["Memory/A.md", "Memory/B.md"]
    assert vault.notes("Nowhere") == []


def test_a_vault_that_cannot_be_written_reports_a_vault_error(tmp_path):
    (tmp_path / "vault").write_text("a file where the vault folder should be")
    vault = FolderVault(tmp_path / "vault")

    with pytest.raises(VaultError):
        vault.write("Memory/Note.md", "x")
    with pytest.raises(VaultError):
        vault.ensure_folder("Memory")
