"""The rules for saving a file (M6), with no model involved.

A file is data. Tanios stores its bytes under a name the user chose and never opens,
reads or runs it. Only ordinary document, picture, audio and video types are kept.
"""

import re
from dataclasses import dataclass

# Extension -> what to call it in a reply. Anything not listed here is not saved:
# programs, scripts, web pages, archives, and Markdown (which Obsidian would treat as
# one of the user's notes).
KINDS: dict[str, str] = {
    "pdf": "PDF",
    "txt": "text file",
    "csv": "spreadsheet",
    "rtf": "document",
    "doc": "document",
    "docx": "document",
    "odt": "document",
    "xls": "spreadsheet",
    "xlsx": "spreadsheet",
    "ods": "spreadsheet",
    "ppt": "presentation",
    "pptx": "presentation",
    "odp": "presentation",
    "png": "picture",
    "jpg": "picture",
    "jpeg": "picture",
    "gif": "picture",
    "webp": "picture",
    "heic": "picture",
    "bmp": "picture",
    "tif": "picture",
    "tiff": "picture",
    "mp3": "audio file",
    "m4a": "audio file",
    "ogg": "audio file",
    "opus": "audio file",
    "wav": "audio file",
    "aac": "audio file",
    "amr": "audio file",
    "mp4": "video",
    "mov": "video",
    "3gp": "video",
}

# For files that arrive without a name, such as photos sent on WhatsApp.
BY_MIME_TYPE: dict[str, str] = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/csv": "csv",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/ogg": "ogg",
    "audio/aac": "aac",
    "audio/amr": "amr",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/3gpp": "3gp",
}

_EXTENSION = re.compile(r"\.([A-Za-z0-9]{1,8})$")
_THING = r"(?:file|pdf|photo|picture|image|pic|document|doc|video|scan|attachment)"
_AS = r"(?:\s+(?:as|under|called|named)\b[\s:]*(?P<name>.*))?"
_LEAD = re.compile(
    r"^(?:(?:hey|hi|hello|ok|okay)\b[\s,]*)?(?:tanios\b[\s,.!:]*)?"
    r"(?:(?:please|can you|could you|will you)\b[\s,]*)*",
    re.IGNORECASE,
)
_SAVE_THIS = re.compile(
    rf"^(?:save|keep|store|file)(?:\s+(?:this|that|it|the\s+{_THING}|this\s+{_THING}))?{_AS}$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class IncomingFile:
    """A file a user sent on WhatsApp, before it is fetched."""

    media_id: str
    extension: str
    filename: str | None = None

    @property
    def kind(self) -> str:
        return KINDS[self.extension]


@dataclass(frozen=True)
class FileRequest:
    name: str  # what the user wants it called; empty means "use its own name"


def extension_of(filename: str | None) -> str | None:
    match = _EXTENSION.search(filename.strip()) if filename else None
    return match.group(1).lower() if match else None


def safe_extension(filename: str | None, mime_type: str | None) -> str | None:
    """The extension to save a file under, or None when its type is not one Tanios keeps.

    A name with an extension is judged by that extension alone: "invoice.exe" is refused
    whatever type it claims to be.
    """
    named = extension_of(filename)
    if named is not None:
        return named if named in KINDS else None
    mime = (mime_type or "").split(";")[0].strip().lower()
    return BY_MIME_TYPE.get(mime)


def without_extension(name: str) -> str:
    """ "lease.pdf" -> "lease". Only a known extension is removed."""
    extension = extension_of(name)
    return name[: -len(extension) - 1] if extension in KINDS else name


def parse_file_request(said: str | None) -> FileRequest | None:
    """Is this caption or message asking to save a file? Decided without a model."""
    if not said or not said.strip():
        return None
    request = _LEAD.sub("", said.strip(), count=1).strip().rstrip(".!").strip()
    match = _SAVE_THIS.match(request)
    if not match:
        return None
    name = (match.group("name") or "").strip().strip("\"'“”‘’").strip()
    return FileRequest(without_extension(name))


def readable_size(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KB"
    return f"{size / (1024 * 1024):.1f} MB"
