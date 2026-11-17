"""What Tanios remembers, kept as plain notes in its own Obsidian vault (M1 to M5, M7, M8).

Saving, capturing, finding and forgetting are done with plain code and no model call,
so they cost nothing and behave the same every time. A model is only involved when a
question is answered from the notes that were found.
"""

import contextlib
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

from tanios.files import KINDS, readable_size
from tanios.vault import FolderVault, VaultError

INSTRUCTIONS = "Instructions.md"
FILES_INDEX = "Files/Index.md"
INDEX_HEADER = (
    "# Files Tanios saved\n\n"
    "One line per file, oldest first. Tanios adds a line each time it saves one.\n\n"
)
# Picking a free name and writing under it must not be interleaved for two saves.
_WRITING = threading.Lock()
FOLDERS = ("Memory", "Inbox", "Files", "Log")
MARKER = "Write your instructions below this line."

INSTRUCTIONS_TEMPLATE = f"""# Instructions for Tanios

This note tells Tanios how you want it to work. Write in plain English, for example:

- Always answer in bullet points.
- Keep answers under three sentences unless I ask for more.
- My working week is Sunday to Thursday.

Tanios reads this note before every answer, so a change here applies from your next
message. It can shape how Tanios answers. It cannot make Tanios send, delete or share
anything without your approval.

---

{MARKER}

"""

ABOUT = {
    "Memory": "Tanios saves what you ask it to remember here, one note per memory.\n\n"
    'Say "remember ..." on WhatsApp to add one, or "forget ..." to remove one.\n',
    "Inbox": "Quick notes you send Tanios land here, named with the date.\n\n"
    'Say "note: ..." on WhatsApp to add one.\n',
    "Files": "Files you ask Tanios to save are kept here, and listed in the Index note.\n\n"
    'Send a file on WhatsApp with the caption "save this as my lease", or say '
    '"save the attachment from 2 as my lease" for an email in your list.\n',
}

MAX_INSTRUCTIONS_CHARS = 4000
MAX_SNIPPET_CHARS = 1200
MAX_CONTEXT_CHARS = 4000
MAX_TITLE_CHARS = 60

STOPWORDS = frozenset(
    "a about all also am an and any are as at be been but by can did do does for from had "
    "has have her him his how i if in into is it its just me my no not of on or our out "
    "please she so some than that the their them then there these they this to up us was "
    "we were what when where which who whom why will with would you your".split()
)

_WORD = re.compile(r"[a-z0-9]+")
_FORBIDDEN = re.compile(r'[\\/:*?"<>|#^\[\]]')
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


@dataclass(frozen=True)
class Note:
    name: str  # what the user sees in Obsidian, without folder or .md
    path: str  # relative to the vault
    text: str  # the note's content, shortened if long


@dataclass(frozen=True)
class SavedFile:
    name: str  # with its extension, for example "My lease.pdf"
    path: str  # relative to the vault
    size: int


@dataclass(frozen=True)
class Forgotten:
    removed: tuple[Note, ...] = ()
    # More than one memory matched, so nothing was removed. The user has to narrow it.
    candidates: tuple[str, ...] = ()


def words(text: str) -> list[str]:
    """Lower-case content words, with a plain trailing "s" removed so plurals match."""
    found = []
    for word in _WORD.findall(text.lower()):
        if len(word) < 3 or word in STOPWORDS:
            continue
        found.append(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return found


def note_title(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    """A file name Obsidian accepts on every device, taken from the start of the text."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    title = " ".join(_FORBIDDEN.sub(" ", first_line).split()).strip(" .")
    if len(title) > limit:
        cut = title.rfind(" ", 0, limit)
        title = title[: cut if cut > limit // 2 else limit].rstrip(" .,;")
    if not title:
        return "Note"
    return title[0].upper() + title[1:]


def body_of(text: str) -> str:
    """A note's content without the properties block at its top."""
    return _FRONTMATTER.sub("", text, count=1).strip()


class Memory:
    def __init__(self, vault: FolderVault, now: Callable[[], datetime]) -> None:
        self._vault = vault
        self._now = now

    # M1: the layout -------------------------------------------------------------------

    def destroy(self) -> bool:
        """U3: delete this user's whole vault on the server."""
        return self._vault.destroy()

    def ensure_layout(self) -> None:
        """Create the folders and starter notes that are missing. Never overwrites."""
        for folder in FOLDERS:
            self._vault.ensure_folder(folder)
        if not self._vault.exists(INSTRUCTIONS):
            self._vault.write(INSTRUCTIONS, INSTRUCTIONS_TEMPLATE)
        for folder, text in ABOUT.items():
            path = f"{folder}/About {folder}.md"
            if not self._vault.exists(path):
                self._vault.write(path, text)

    # M5: the user's standing instructions ----------------------------------------------

    def instructions(self) -> str:
        """What the user wrote in the Instructions note. Empty while it is untouched."""
        if not self._vault.exists(INSTRUCTIONS):
            return ""
        text = self._vault.read(INSTRUCTIONS)
        # With the marker still in place, only what follows it counts. If the user
        # removed the marker, the whole note is theirs.
        _, marker, after = text.partition(MARKER)
        written = after if marker else text
        return written.strip()[:MAX_INSTRUCTIONS_CHARS]

    # M2 and M3: saving -----------------------------------------------------------------

    def remember(self, fact: str) -> Note:
        note = self._save("Memory", note_title(fact), fact)
        self.log(f'Remembered "{note.name}"')
        return note

    def capture(self, text: str) -> Note:
        dated = f"{self._now():%Y-%m-%d} {note_title(text, MAX_TITLE_CHARS - 11)}"
        note = self._save("Inbox", dated, text)
        self.log(f'Noted "{note.name}" in Inbox')
        return note

    def _save(self, folder: str, title: str, text: str) -> Note:
        self.ensure_layout()
        content = text.strip()
        properties = f"---\ncreated: {self._now():%Y-%m-%dT%H:%M}\nsource: whatsapp\n---\n"
        with _WRITING:
            name = self._free_name(folder, title, "md")
            path = f"{folder}/{name}.md"
            self._vault.write(path, f"{properties}{content}\n")
        return Note(name=name, path=path, text=content)

    def _free_name(self, folder: str, title: str, extension: str) -> str:
        """The title, or the title with a number, so nothing already there is replaced."""
        name = title
        counter = 2
        while self._vault.exists(f"{folder}/{name}.{extension}"):
            name = f"{title} {counter}"
            counter += 1
        return name

    # M6: files -------------------------------------------------------------------------

    def keep_file(
        self, data: bytes, *, name: str, extension: str, origin: str, fallback: str = ""
    ) -> SavedFile:
        """Store a file's bytes under Files/ and add its line to the index.

        `name` is what the user asked for and `fallback` the file's own name; both pass
        through note_title, so neither can carry a path. The bytes are never read.
        """
        if extension not in KINDS:
            raise VaultError("file type is not one Tanios keeps")
        self.ensure_layout()
        wanted = name.strip() or fallback.strip()
        if wanted:
            title = note_title(wanted)
        else:
            title = f"{KINDS[extension].capitalize()} {self._now():%Y-%m-%d %H%M}"
        with _WRITING:
            stem = self._free_name("Files", title, extension)
            saved = SavedFile(f"{stem}.{extension}", f"Files/{stem}.{extension}", len(data))
            self._vault.write_bytes(saved.path, data)
            try:
                if not self._vault.exists(FILES_INDEX):
                    self._vault.write(FILES_INDEX, INDEX_HEADER)
                self._vault.append(
                    FILES_INDEX,
                    f"- {self._now():%Y-%m-%d} [[{saved.name}]] ({KINDS[extension]}, "
                    f"{readable_size(saved.size)}, {note_title(origin, 80)})\n",
                )
            except VaultError:
                # A file with no line in the index would be easy to lose. Take it back
                # out, so "nothing was saved" is true.
                with contextlib.suppress(VaultError):
                    self._vault.delete(saved.path)
                raise
        self.log(f'Saved "{saved.name}" to Files')
        return saved

    # M4: finding -----------------------------------------------------------------------

    def find(self, question: str, limit: int = 4) -> list[Note]:
        """The notes most likely to help answer a question. Empty when nothing matches."""
        wanted = set(words(question))
        if not wanted:
            return []
        scored: list[tuple[int, str, Note]] = []
        for path in self._searchable():
            note = self._load(path)
            if note is None:
                continue
            in_title = wanted & set(words(note.name))
            in_body = wanted & set(words(note.text))
            score = 2 * len(in_title) + len(in_body - in_title)
            if score:
                scored.append((-score, path, note))
        scored.sort()

        chosen: list[Note] = []
        used = 0
        for _, _, note in scored[:limit]:
            if used + len(note.text) > MAX_CONTEXT_CHARS and chosen:
                break
            chosen.append(note)
            used += len(note.text)
        return chosen

    def _searchable(self) -> list[str]:
        """Every note except the log, the instructions and the About notes."""
        return [
            path
            for path in self._vault.notes()
            if path != INSTRUCTIONS
            and not path.startswith("Log/")
            and not path.rsplit("/", 1)[-1].startswith("About ")
        ]

    def _load(self, path: str) -> Note | None:
        try:
            text = body_of(self._vault.read(path))
        except VaultError:
            return None
        return Note(name=path.rsplit("/", 1)[-1][:-3], path=path, text=text[:MAX_SNIPPET_CHARS])

    # M7: forgetting --------------------------------------------------------------------

    def forget(self, what: str) -> Forgotten:
        """Remove the one memory that matches. Several matches remove nothing."""
        wanted = set(words(what))
        if not wanted:
            return Forgotten()
        matches: list[Note] = []
        for path in self._vault.notes("Memory"):
            if path.rsplit("/", 1)[-1].startswith("About "):
                continue
            note = self._load(path)
            if note is None:
                continue
            if note.name.lower() == what.strip().lower():
                matches = [note]
                break
            if wanted <= set(words(note.name)) | set(words(note.text)):
                matches.append(note)
        if len(matches) > 1:
            return Forgotten(candidates=tuple(note.name for note in matches[:5]))
        for note in matches:
            self._vault.delete(note.path)
            self.log(f'Forgot "{note.name}"')
        return Forgotten(removed=tuple(matches))

    # M8: the daily log -----------------------------------------------------------------

    def log(self, line: str) -> None:
        """One line in today's log for something Tanios did on the user's behalf."""
        now = self._now()
        path = f"Log/{now:%Y-%m-%d}.md"
        if not self._vault.exists(path):
            self._vault.write(path, f"# Tanios log for {now.day} {now:%B %Y}\n\n")
        self._vault.append(path, f"- {now:%H:%M} {line}\n")


class Memories:
    """Gives every user their own vault: a folder named after their user number."""

    def __init__(self, vaults_dir: Path, timezone: tzinfo) -> None:
        self._vaults_dir = vaults_dir
        self._timezone = timezone

    def __call__(self, user_id: int) -> Memory:
        # The folder name comes from a database number, never from anything a user typed.
        vault = FolderVault(self._vaults_dir / f"user-{int(user_id)}")
        return Memory(vault, now=lambda: datetime.now(self._timezone))
