"""Tanios's own Obsidian vault: a folder of Markdown notes on the server.

Obsidian Sync keeps this folder and the user's devices in step (see docs/MEMORY.md).
Tanios only ever touches files inside the folder it is given (M1).
"""

import contextlib
import os
import secrets
from pathlib import Path, PurePosixPath
from typing import Protocol

MAX_NOTE_BYTES = 200_000
MAX_NOTES_SCANNED = 2000


class VaultError(Exception):
    """A path that is not allowed, or a file that could not be used."""


class Vault(Protocol):
    """Tests use the real FolderVault on a temporary folder; this is the shape it has."""

    def exists(self, path: str) -> bool: ...

    def read(self, path: str) -> str: ...

    def write(self, path: str, text: str) -> None: ...

    def append(self, path: str, text: str) -> None: ...

    def delete(self, path: str) -> None: ...

    def notes(self, folder: str = "") -> list[str]:
        """Paths of the Markdown notes under a folder, relative to the vault, sorted."""
        ...


class FolderVault:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _resolve(self, path: str) -> Path:
        """Turn a vault-relative path into a real one, refusing anything outside the vault."""
        relative = PurePosixPath(path)
        if relative.is_absolute() or not relative.parts:
            raise VaultError("path must be relative to the vault")
        for part in relative.parts:
            # No climbing out, and no hidden files: Obsidian keeps its own settings in
            # dot-folders, which are not Tanios's to touch.
            if part in ("..", ".") or part.startswith(".") or "\\" in part or "\x00" in part:
                raise VaultError("path is not allowed")
        root = self._root.resolve()
        target = (root / Path(*relative.parts)).resolve()
        # resolve() follows symlinks, so a link pointing out of the vault is caught here.
        if not target.is_relative_to(root):
            raise VaultError("path leaves the vault")
        return target

    def exists(self, path: str) -> bool:
        return self._resolve(path).is_file()

    def read(self, path: str) -> str:
        target = self._resolve(path)
        try:
            with target.open("rb") as file:
                data = file.read(MAX_NOTE_BYTES)
        except OSError:
            raise VaultError("note could not be read") from None
        return data.decode("utf-8", errors="replace")

    def write(self, path: str, text: str) -> None:
        target = self._resolve(path)
        # Write to a hidden temporary file, then swap it in, so a sync that runs at the
        # same moment never sees half a note. Obsidian ignores hidden files.
        temporary = target.parent / f".tanios-{secrets.token_hex(6)}.tmp"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, target)
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise VaultError("note could not be written") from None

    def append(self, path: str, text: str) -> None:
        target = self._resolve(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as file:
                file.write(text)
        except OSError:
            raise VaultError("note could not be written") from None

    def delete(self, path: str) -> None:
        target = self._resolve(path)
        try:
            target.unlink()
        except FileNotFoundError:
            raise VaultError("note does not exist") from None
        except OSError:
            raise VaultError("note could not be deleted") from None

    def ensure_folder(self, folder: str) -> None:
        try:
            self._resolve(folder).mkdir(parents=True, exist_ok=True)
        except OSError:
            raise VaultError("folder could not be created") from None

    def notes(self, folder: str = "") -> list[str]:
        root = self._root.resolve()
        start = self._resolve(folder) if folder else root
        if not start.is_dir():
            return []
        found: list[str] = []
        for directory, folders, files in os.walk(start, followlinks=False):
            folders[:] = sorted(name for name in folders if not name.startswith("."))
            for name in sorted(files):
                if name.startswith(".") or not name.lower().endswith(".md"):
                    continue
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                found.append(path.relative_to(root).as_posix())
                if len(found) >= MAX_NOTES_SCANNED:
                    return found
        return found
