"""Milestone 4: memory is switched on by one setting, and the owner's vault is ready at start."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from tanios.config import Settings
from tanios.main import _open_memories
from tests.fakes import OWNER, InMemoryStore


def settings(**values) -> Settings:
    return Settings(_env_file=None, owner_number=OWNER, **values)


def test_without_a_vaults_folder_memory_is_off(caplog):
    with caplog.at_level("WARNING"):
        memories = asyncio.run(_open_memories(settings(), InMemoryStore()))

    assert memories is None
    assert "TANIOS_VAULTS_DIR" in caplog.text


def test_m1_the_owners_vault_is_laid_out_when_the_server_starts(tmp_path):
    store = InMemoryStore()
    store.add_user(OWNER)

    memories = asyncio.run(_open_memories(settings(vaults_dir=tmp_path), store))

    assert sorted(path.name for path in (tmp_path / "user-1").iterdir()) == [
        "Files",
        "Inbox",
        "Instructions.md",
        "Log",
        "Memory",
    ]
    assert memories(1).find("anything at all") == []


def test_m1_note_dates_use_the_configured_time_zone(tmp_path):
    store = InMemoryStore()
    store.add_user(OWNER)
    configured = settings(vaults_dir=tmp_path, timezone="Pacific/Kiritimati")  # UTC+14

    memories = asyncio.run(_open_memories(configured, store))
    note = memories(1).capture("call the bank")

    assert note.name[:10] == f"{datetime.now(ZoneInfo('Pacific/Kiritimati')):%Y-%m-%d}"


def test_a_vault_folder_that_cannot_be_used_does_not_stop_the_server(tmp_path, caplog):
    store = InMemoryStore()
    store.add_user(OWNER)
    (tmp_path / "user-1").write_text("a file where the vault folder should be")

    with caplog.at_level("ERROR"):
        memories = asyncio.run(_open_memories(settings(vaults_dir=tmp_path), store))

    assert memories is not None
    assert "vault could not be prepared" in caplog.text
