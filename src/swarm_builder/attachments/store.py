"""The attachment store: uploads on disk, with a TTL and no paths from input.

Layout, under the workspace (which is git-ignored wholesale)::

    <workspace>/attachments/<id>/meta.json      the AttachmentRecord
    <workspace>/attachments/<id>/content.txt    extracted text (not for images)
    <workspace>/attachments/<id>/binary         the uploaded bytes (images only)

Three properties are deliberate:

* **No request value ever names a path.** The directory is a uuid4 minted here,
  and any id coming back in a later request is validated against
  :data:`~swarm_builder.attachments.models.ATTACHMENT_ID_PATTERN` before it is
  joined. A traversal attempt is therefore "no such attachment", never an
  ``OSError``.
* **Publishing is one rename.** The directory is built under
  ``attachments/.tmp-<id>/`` and moved into place with a single ``os.rename``
  (``meta.json`` written and fsynced last), so a crash can leave an orphan
  temporary directory but can never leave a half-written attachment that looks
  loadable.
* **Reading is bounded and deduped.** ``load_attachments`` enforces the count
  and total-byte caps from record metadata alone, and collapses duplicates by
  payload digest, so attaching the same workbook twice neither doubles the
  prompt nor doubles the bill.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from swarm_builder.attachments.limits import (
    ATTACHMENT_TTL_SECONDS,
    EXTRACT_TIMEOUT_SECONDS,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    MAX_TOTAL_ATTACHMENT_BYTES,
    PREVIEW_CHARS,
)
from swarm_builder.attachments.models import (
    ATTACHMENT_ID_PATTERN,
    AttachmentExpiredError,
    AttachmentKind,
    AttachmentNotFoundError,
    AttachmentRecord,
    AttachmentStoreError,
    AttachmentTooLargeError,
    Extraction,
    LoadedAttachment,
    sanitize_filename,
)

#: Directory name under the workspace.
ATTACHMENTS_DIRNAME = "attachments"

#: Prefix of a directory that is still being built.
TMP_PREFIX = ".tmp-"

#: Ids currently being read, so pruning never deletes a file mid-request.
_IN_FLIGHT: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()

#: The metadata file name inside an attachment directory.
META_FILENAME = "meta.json"
TEXT_FILENAME = "content.txt"
BINARY_FILENAME = "binary"


def attachments_dir(workspace: Path) -> Path:
    """The directory holding every stored attachment."""
    return workspace / ATTACHMENTS_DIRNAME


def attachment_dir(workspace: Path, attachment_id: str) -> Path:
    """The directory for ``attachment_id``, after validating the id."""
    return attachments_dir(workspace) / validate_attachment_id(attachment_id)


def validate_attachment_id(attachment_id: str) -> str:
    """Return ``attachment_id`` when it is a well-formed opaque id.

    Raises:
        AttachmentNotFoundError: For anything else -- including a traversal
            attempt, which is reported as a missing attachment rather than a
            filesystem error.
    """
    if not ATTACHMENT_ID_PATTERN.fullmatch(attachment_id or ""):
        raise AttachmentNotFoundError(attachment_id)
    return attachment_id


def _write_text_sync(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` and fsync it (a small file, no streaming)."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _write_bytes_sync(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` and fsync it."""
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def save_attachment(
    workspace: Path,
    filename: str,
    data: bytes,
    extraction: Extraction,
    media_type: str,
    kind: AttachmentKind,
) -> AttachmentRecord:
    """Store one extract already read, and return its record.

    Args:
        workspace: The workspace root.
        filename: The client-supplied name; sanitized before it is stored.
        data: The uploaded bytes (kept on disk only for images).
        extraction: What :func:`~swarm_builder.attachments.extract.extract` produced.
        media_type: The canonical media type for the kind.
        kind: Which reader produced the extraction.

    Returns:
        The stored record, already carrying its expiry and payload digest.

    Raises:
        AttachmentTooLargeError: The payload exceeds the per-file cap. The upload
            route already enforces this while reading the body; repeating it here
            keeps the store's own invariant true for any future caller.
        AttachmentStoreError: The workspace could not be written.
    """
    if len(data) > MAX_ATTACHMENT_BYTES:
        limit_mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
        raise AttachmentTooLargeError(
            sanitize_filename(filename), f"the file is larger than {limit_mb} MB."
        )
    try:
        prune_expired(workspace)
    except Exception:  # noqa: BLE001 -- best-effort sweep; never fail a save for it.
        pass
    attachment_id = uuid4().hex
    display = sanitize_filename(filename)
    # Digest the *uploaded* bytes for every kind. Digesting the extracted text for
    # text files made dedupe mean "these two files render identically", which is
    # not what the feature promises: it promises that attaching the same file
    # twice does not double the context, and that is a statement about the file.
    digest = hashlib.sha256(data).hexdigest()
    now = datetime.now(UTC)
    record = AttachmentRecord(
        id=attachment_id,
        filename=display,
        kind=kind,
        media_type=media_type,
        bytes=len(data),
        chars=0 if kind == "image" else len(extraction.text),
        truncated=extraction.truncated,
        content_sha256=digest,
        created_at=now,
        expires_at=now + timedelta(seconds=ATTACHMENT_TTL_SECONDS),
        notes=list(extraction.notes),
        preview="" if kind == "image" else extraction.text[:PREVIEW_CHARS],
    )
    directory = attachments_dir(workspace)
    staging = directory / f"{TMP_PREFIX}{attachment_id}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        staging.mkdir(mode=0o700)
        if kind == "image":
            _write_bytes_sync(staging / BINARY_FILENAME, data)
        else:
            _write_text_sync(staging / TEXT_FILENAME, extraction.text)
        _write_text_sync(
            staging / META_FILENAME,
            json.dumps(record.model_dump(mode="json", by_alias=True), indent=2),
        )
        os.rename(staging, directory / attachment_id)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise AttachmentStoreError(
            f"the attachment could not be saved to {directory}: {exc}"
        ) from exc
    return record


def _read_record(workspace: Path, attachment_id: str) -> AttachmentRecord:
    """Read and validate one record, or report it as missing/expired."""
    validate_attachment_id(attachment_id)
    meta_path = attachment_dir(workspace, attachment_id) / META_FILENAME
    try:
        raw = meta_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AttachmentNotFoundError(attachment_id) from exc
    except OSError as exc:
        raise AttachmentStoreError(f"the attachment metadata could not be read: {exc}") from exc
    try:
        record = AttachmentRecord.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        # A half-written or hand-mangled record is "not there" from a caller's
        # point of view; it must never surface as a validation traceback.
        raise AttachmentNotFoundError(attachment_id) from exc
    try:
        expired = record.expires_at <= datetime.now(UTC)
    except TypeError as exc:
        # A hand-edited record with a naive timestamp is not a crash: it is an
        # attachment this app can no longer reason about.
        raise AttachmentNotFoundError(attachment_id) from exc
    if expired:
        shutil.rmtree(attachment_dir(workspace, attachment_id), ignore_errors=True)
        raise AttachmentExpiredError(attachment_id)
    return record


def load_attachment(workspace: Path, attachment_id: str) -> LoadedAttachment:
    """Load one attachment's record plus the payload needed to use it.

    Args:
        workspace: The workspace root.
        attachment_id: An opaque id from a previous upload.

    Returns:
        The record, the extracted text ('' for images), and the image bytes.

    Raises:
        AttachmentNotFoundError: The id is malformed, unknown, incomplete, or
            was pruned. Expired attachments raise the subclass
            :class:`~swarm_builder.attachments.models.AttachmentExpiredError`.
    """
    validate_attachment_id(attachment_id)
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.add(attachment_id)
    try:
        record = _read_record(workspace, attachment_id)
        directory = attachment_dir(workspace, attachment_id)
        try:
            if record.kind == "image":
                return LoadedAttachment(
                    record=record, text="", binary=(directory / BINARY_FILENAME).read_bytes()
                )
            text = (directory / TEXT_FILENAME).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            # Pruned between the record read and the payload read: the same
            # answer a caller gets for an expired attachment.
            raise AttachmentExpiredError(attachment_id) from exc
        except OSError as exc:
            raise AttachmentStoreError(
                f"attachment {attachment_id} could not be read from the workspace: {exc}"
            ) from exc
        return LoadedAttachment(record=record, text=text, binary=None)
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(attachment_id)


def load_attachments(workspace: Path, ids: Sequence[str]) -> list[LoadedAttachment]:
    """Load, dedupe and cap the attachments one request named.

    Order follows ``ids``; a duplicate payload keeps its first position. The
    count and total-size caps are checked against record metadata, so deciding
    them never reads a payload.

    Args:
        workspace: The workspace root.
        ids: Attachment ids, in the order the user attached them.

    Returns:
        The loaded attachments, deduped and in order.

    Raises:
        AttachmentNotFoundError: An id is malformed, unknown, or expired.
        AttachmentTooLargeError: More than
            :data:`~swarm_builder.attachments.limits.MAX_ATTACHMENTS` distinct
            attachments, or more than
            :data:`~swarm_builder.attachments.limits.MAX_TOTAL_ATTACHMENT_BYTES`
            in total.
    """
    unique_ids = list(dict.fromkeys(ids))
    if len(unique_ids) > MAX_ATTACHMENTS:
        raise AttachmentTooLargeError(
            f"{len(unique_ids)} files",
            f"at most {MAX_ATTACHMENTS} files can be attached to one generation; "
            f"{len(unique_ids)} were given.",
        )
    loaded: list[LoadedAttachment] = []
    seen_digests: set[str] = set()
    total_bytes = 0
    for attachment_id in unique_ids:
        item = load_attachment(workspace, attachment_id)
        if item.record.content_sha256 in seen_digests:
            continue
        seen_digests.add(item.record.content_sha256)
        total_bytes += item.record.bytes
        if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
            limit_mb = MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)
            raise AttachmentTooLargeError(
                "the attached files",
                f"the attached files total more than {limit_mb} MB; remove one and try again.",
            )
        loaded.append(item)
    return loaded


def delete_attachment(workspace: Path, attachment_id: str) -> bool:
    """Delete one attachment's directory.

    Returns:
        True when a stored attachment was removed, False when the id was
        unknown (deleting something already gone is not an error).

    Raises:
        AttachmentNotFoundError: The id is malformed.
    """
    directory = attachment_dir(workspace, attachment_id)
    if not directory.is_dir():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return True


def prune_expired(workspace: Path, *, now: datetime | None = None) -> int:
    """Remove expired attachments and abandoned staging directories.

    Best-effort by contract: every caller treats the return value as
    telemetry, and a failure to prune never fails a request. Ids currently being
    read are skipped, and a staging directory is only removed once it is older
    than one extraction budget, so a prune can never race an in-progress upload.

    Args:
        workspace: The workspace root.
        now: The instant to judge expiry against (tests inject one).

    Returns:
        How many directories were removed.
    """
    directory = attachments_dir(workspace)
    if not directory.is_dir():
        return 0
    moment = now or datetime.now(UTC)
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    with _IN_FLIGHT_LOCK:
        in_flight = set(_IN_FLIGHT)
    for entry in entries:
        try:  # noqa: BLE001 -- pruning is best-effort by contract: one unreadable
            # directory (a hand-edited record, a naive timestamp, a vanished file)
            # must never fail the upload that triggered the sweep.
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith(TMP_PREFIX):
                if _age_seconds(entry, moment) > EXTRACT_TIMEOUT_SECONDS:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
                continue
            if not ATTACHMENT_ID_PATTERN.fullmatch(name) or name in in_flight:
                continue
            record = _peek_record(entry)
            if record is None:
                if _age_seconds(entry, moment) > EXTRACT_TIMEOUT_SECONDS:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
                continue
            if record.expires_at <= moment:
                # Re-read immediately before removal: a request that started
                # after the scan may have replaced the record meanwhile, and
                # pruning a live attachment is worse than keeping a dead one.
                if _peek_record(entry) == record:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
        except Exception:  # noqa: BLE001 -- see above.
            continue
    return removed


def _peek_record(directory: Path) -> AttachmentRecord | None:
    """Read a record without raising (``None`` when it is absent or unreadable)."""
    try:
        raw = (directory / META_FILENAME).read_text(encoding="utf-8")
        return AttachmentRecord.model_validate(json.loads(raw))
    except (OSError, json.JSONDecodeError, ValidationError, ValueError):
        return None


def _age_seconds(path: Path, moment: datetime) -> float:
    """How long ago ``path`` was last modified, in seconds."""
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return 0.0
    return (moment - modified).total_seconds()


__all__ = [
    "ATTACHMENTS_DIRNAME",
    "attachments_dir",
    "attachment_dir",
    "delete_attachment",
    "load_attachment",
    "load_attachments",
    "prune_expired",
    "sanitize_filename",
    "save_attachment",
    "validate_attachment_id",
]
