"""The shapes and exceptions every attachment code path shares.

An attachment is *supporting evidence for a description*, never part of the
graph document: it is read once, rendered into a bounded context block (or sent
as image content), and then it is scratch state with a TTL. That is why nothing
in this module appears in :mod:`swarm_builder.models` -- the graph schema is
untouched by this feature (``PLAN-ATTACHMENTS-CLARIFY.md``).

**Error messages are app-authored.** Every exception here takes a *sanitized*
display filename plus a fixed cause sentence; the underlying parser's own
message is never interpolated. Two reasons: those messages embed the real
filename and sometimes the file's content, and a user-facing 422 must not leak
a workspace path or a cell of a confidential spreadsheet into a browser, a log
line, or a bug report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from swarm_builder.attachments.limits import MAX_ATTACHMENT_NAME_CHARS

#: What an extractor produced for one file.
AttachmentKind = Literal["csv", "xlsx", "pptx", "image"]

#: Extension -> (kind, canonical media type). The extension is authoritative:
#: a browser's declared content type is a hint at best (and is exactly what an
#: attacker controls), so it is only consulted when the extension is unknown.
SUPPORTED_SUFFIXES: dict[str, tuple[AttachmentKind, str]] = {
    ".csv": ("csv", "text/csv"),
    ".tsv": ("csv", "text/tab-separated-values"),
    ".xlsx": (
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ".pptx": (
        "pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".webp": ("image", "image/webp"),
    ".gif": ("image", "image/gif"),
}

#: Media type -> kind, for the hints above only.
MEDIA_TYPES: dict[str, AttachmentKind] = {
    media_type: kind for kind, media_type in SUPPORTED_SUFFIXES.values()
}

#: Human labels used in prompts, notes and error messages.
KIND_LABELS: dict[AttachmentKind, str] = {
    "csv": "CSV table",
    "xlsx": "Excel workbook",
    "pptx": "PowerPoint deck",
    "image": "image",
}

#: Formats users actually try that this app deliberately refuses, each with the
#: fix to name in the error. Naming the fix is the whole point: "unsupported"
#: alone tells a user nothing about what to do next.
UNSUPPORTED_SUFFIX_FIXES: dict[str, str] = {
    ".xls": "re-save it as .xlsx",
    ".ppt": "re-save it as .pptx",
    ".pdf": "this app does not read PDFs yet",
    ".docx": "this app does not read Word documents yet",
    ".doc": "this app does not read Word documents yet",
    ".txt": "save it as .csv, or paste the text into the description",
    ".md": "save it as .csv, or paste the text into the description",
    ".json": "save it as .csv, or paste the text into the description",
}

#: ``filterwarnings``-free guard: attachment ids are the only thing from a
#: request that ever touches a path, and only these 32 lowercase hex characters
#: are accepted.
ATTACHMENT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

#: Characters never kept in a display filename. Path separators and NUL are the
#: dangerous ones; the quotes are stripped because the name is echoed inside
#: prompt blocks and error strings.
_UNSAFE_NAME_CHARS = re.compile(r'[\\/\x00-\x1f\x7f"\'`]')


class AttachmentError(RuntimeError):
    """Base class for every attachment failure; the message is user-safe."""


class UnsupportedAttachmentError(AttachmentError):
    """The file's extension is not one this app reads."""

    def __init__(self, filename: str, supported: str) -> None:
        self.filename = filename
        self.supported = supported
        super().__init__(
            f"{filename!r} is not a file type this app can read. Supported: {supported}."
        )


class AttachmentTooLargeError(AttachmentError):
    """A per-file, count, or total-size cap was exceeded."""

    def __init__(self, filename: str, message: str) -> None:
        self.filename = filename
        super().__init__(message)


class AttachmentReadError(AttachmentError):
    """The file could not be turned into text (empty, corrupt, encrypted, slow)."""

    def __init__(self, filename: str, cause: str) -> None:
        self.filename = filename
        self.cause = cause
        super().__init__(f"{filename!r} could not be read: {cause}.")


class AttachmentNotFoundError(AttachmentError):
    """The id is unknown, its record is incomplete, or it was never valid."""

    def __init__(self, attachment_id: str, *, expired: bool = False) -> None:
        self.attachment_id = attachment_id
        self.expired = expired
        if expired:
            message = (
                f"attachment {attachment_id!r} has expired; attach the file again and generate."
            )
        else:
            message = (
                f"there is no attachment with id {attachment_id!r}; it may have been removed "
                "or never uploaded."
            )
        super().__init__(message)


class AttachmentExpiredError(AttachmentNotFoundError):
    """The attachment existed but its TTL passed.

    A subclass, so a caller that only cares whether an id is usable needs one
    ``except`` -- and a caller that wants to tell the user to re-attach can ask
    for this one specifically.
    """

    def __init__(self, attachment_id: str) -> None:
        super().__init__(attachment_id, expired=True)


class AttachmentStoreError(AttachmentError):
    """The workspace could not be written (disk full, permissions, read-only).

    The only attachment error whose cause text names a resolved path, so it
    carries two messages: the exception's own (with the path, for a server log)
    and :attr:`public_message`, the fixed, path-free sentence a route may return.
    A client must never be handed a workspace path, whatever went wrong.
    """

    def __init__(self, detail: str) -> None:
        self.public_message = (
            "the attachments workspace could not be written; check that it is "
            "writable and has free space"
        )
        super().__init__(detail)


def sanitize_filename(raw: str) -> str:
    """Return a display-only filename that is safe to store, log and echo.

    Never used to build a path: the stored file lives under a uuid directory.
    Path separators, control characters and quotes are replaced, whitespace is
    collapsed, and the result is capped (a 4 KiB filename is a denial-of-service
    vector for prompt size, not a filename).

    Args:
        raw: The name the client sent, possibly with directories or junk.

    Returns:
        A non-empty name, at most :data:`MAX_ATTACHMENT_NAME_CHARS` characters.
    """
    cleaned = _UNSAFE_NAME_CHARS.sub("_", raw or "")
    cleaned = " ".join(cleaned.split()).strip()
    if len(cleaned) > MAX_ATTACHMENT_NAME_CHARS:
        cleaned = cleaned[:MAX_ATTACHMENT_NAME_CHARS]
    if not cleaned or not any(character.isalnum() for character in cleaned):
        # A name made only of separators or control characters is not a name;
        # showing the user "___" helps nobody identify their own file later.
        return "attachment"
    return cleaned


def suffix_of(filename: str) -> str:
    """The lowercased extension of ``filename``, including the dot ('' when none)."""
    return Path(filename).suffix.lower()


class AttachmentRecord(BaseModel):
    """One stored attachment's metadata.

    The camelCase aliasing matches every other wire model in this app, so the
    same ``meta.json`` is also the shape the route returns.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    filename: str
    kind: AttachmentKind
    media_type: str
    bytes: int = Field(description="Size of the uploaded file, not of the extracted text.")
    chars: int = Field(description="Characters of extracted text; 0 for images.")
    truncated: bool
    content_sha256: str = Field(
        description="Digest of the stored payload; attachments with the same digest are deduped."
    )
    created_at: datetime
    expires_at: datetime
    notes: list[str] = Field(default_factory=list)
    preview: str = Field(
        default="", description="First characters of the extracted text; '' for images."
    )


@dataclass(frozen=True)
class Extraction:
    """What one file produced: its text, whether it was cut short, and why."""

    text: str
    truncated: bool
    notes: list[str]


@dataclass(frozen=True)
class LoadedAttachment:
    """A stored attachment plus the payload needed to use it."""

    record: AttachmentRecord
    text: str
    binary: bytes | None


__all__ = [
    "ATTACHMENT_ID_PATTERN",
    "KIND_LABELS",
    "MEDIA_TYPES",
    "SUPPORTED_SUFFIXES",
    "UNSUPPORTED_SUFFIX_FIXES",
    "AttachmentError",
    "AttachmentExpiredError",
    "AttachmentKind",
    "AttachmentNotFoundError",
    "AttachmentReadError",
    "AttachmentRecord",
    "AttachmentStoreError",
    "AttachmentTooLargeError",
    "Extraction",
    "LoadedAttachment",
    "UnsupportedAttachmentError",
    "sanitize_filename",
    "suffix_of",
]
