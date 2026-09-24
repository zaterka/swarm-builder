"""``/api/graphs/attachments``: upload, and discard, supplementary files.

An attachment is *context for one description*. It is uploaded on its own
request (so the panel can show per-file progress and report a per-file error),
stored in the workspace under a TTL, and referenced by opaque id when a
generation or analysis request is made. Nothing here is persisted into a graph
document.

Four properties this route is responsible for, each with its own failure mode:

* **Bounded intake.** The declared ``Content-Length`` is checked before the body
  is read, and the body is streamed with a hard cap, so neither a huge upload nor
  a lying header can fill the disk. The per-file cap is 10 MiB.
* **Bounded work.** Extraction runs in a worker thread under a timeout, with a
  concurrency semaphore around it: ``openpyxl``/``python-pptx`` are synchronous
  and can be made slow by a hostile file. Lazy imports keep a missing parser an
  HTTP failure (503) rather than an import error at server startup.
* **No model call, and no model requirement, for text.** Only an image needs the
  resolved model (to check that it can read images). A CSV must be attachable on
  a machine with no model configured at all.
* **Safe messages.** Every failure is translated to an app-authored sentence
  with a stable ``code``; a parser's own message (which embeds the real filename
  and sometimes file content) is never returned, logged, or stored.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, File, Request, UploadFile
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from swarm_builder import runtime
from swarm_builder.attachments.limits import (
    EXTRACT_CONCURRENCY,
    EXTRACT_TIMEOUT_SECONDS,
    MAX_ATTACHMENT_BYTES,
    UPLOAD_CHUNK_BYTES,
    UPLOAD_CONTENT_LENGTH_SLACK,
)
from swarm_builder.attachments.models import (
    AttachmentKind,
    AttachmentNotFoundError,
    AttachmentReadError,
    AttachmentRecord,
    AttachmentStoreError,
    AttachmentTooLargeError,
    UnsupportedAttachmentError,
    sanitize_filename,
)
from swarm_builder.config import get_dsh_home, get_workspace_dir
from swarm_builder.inherit.settings import resolve_effective_model
from swarm_builder.routes.health import credential_blocker
from swarm_builder.routes.problems import problem
from swarm_builder.vision import supports_image_input

router = APIRouter(tags=["attachments"])

#: One per process: bounds how many files are being parsed at once. Created at
#: import time deliberately -- an ``asyncio.Semaphore`` binds no event loop until
#: it is awaited (Python 3.10+), so this is safe across the fresh event loop each
#: test uses.
_EXTRACT_SEMAPHORE = asyncio.Semaphore(EXTRACT_CONCURRENCY)

#: Error codes, the stable half of the structured detail.
CODE_UNSUPPORTED_TYPE = "unsupported_attachment_type"
CODE_TOO_LARGE = "attachment_too_large"
CODE_UNREADABLE = "attachment_unreadable"
CODE_EXPIRED = "attachment_expired"
CODE_IMAGE_UNSUPPORTED = "image_input_unsupported"
CODE_UNAVAILABLE = "attachments_unavailable"


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class AttachmentOut(_CamelModel):
    """One attachment, as the panel needs it."""

    id: str
    filename: str
    kind: AttachmentKind
    media_type: str
    bytes: int
    chars: int
    truncated: bool
    notes: list[str]
    #: First characters of the extracted text, so the user can see what context
    #: is about to reach their provider. Empty for images.
    preview: str
    expires_at: datetime


class AttachmentUploadResponse(_CamelModel):
    attachment: AttachmentOut
    #: Non-blocking notes (a dry run saying the image will not be read, for
    #: instance). Never a failure: a failure is a non-2xx status.
    warnings: list[str]


class AttachmentDeleteResponse(_CamelModel):
    attachment_id: str
    deleted: bool


def _attachment_out(record: AttachmentRecord) -> AttachmentOut:
    """Project a stored record onto the wire shape."""
    return AttachmentOut(
        id=record.id,
        filename=record.filename,
        kind=record.kind,
        media_type=record.media_type,
        bytes=record.bytes,
        chars=record.chars,
        truncated=record.truncated,
        notes=list(record.notes),
        preview=record.preview,
        expires_at=record.expires_at,
    )


def _declared_length_too_large(request: Request) -> bool:
    """Whether the declared body size already exceeds the per-file cap."""
    raw = request.headers.get("content-length")
    if raw is None:
        return False
    try:
        declared = int(raw)
    except ValueError:
        return False
    return declared > MAX_ATTACHMENT_BYTES + UPLOAD_CONTENT_LENGTH_SLACK


async def _read_upload(file: UploadFile, display: str) -> bytes:
    """Stream an upload into memory, refusing anything past the per-file cap.

    Raises:
        AttachmentTooLargeError: The body exceeded ``MAX_ATTACHMENT_BYTES``.
    """
    buffer = bytearray()
    while True:
        chunk = await file.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_ATTACHMENT_BYTES:
            await file.close()
            limit_mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
            raise AttachmentTooLargeError(
                display, f"the file is larger than {limit_mb} MB; upload a smaller one."
            )
    await file.close()
    return bytes(buffer)


async def _extract_bounded(
    reader: Callable[..., object], *args: object, **kwargs: object
) -> object:
    """Run a synchronous reader in a thread under the concurrency and time caps.

    The slot is released by a completion callback, **not** by this coroutine.
    That distinction is the whole point: a parser that ignores a timeout keeps
    burning CPU, so releasing its slot on timeout would let successive timed-out
    uploads pile up unbounded threads. The slot therefore stays held until the
    thread really finishes, which is what makes ``EXTRACT_CONCURRENCY`` a real
    bound. The caller still gets its 422 at the timeout.

    Raises:
        TimeoutError: The reader exceeded
            :data:`~swarm_builder.attachments.limits.EXTRACT_TIMEOUT_SECONDS`.
            (``asyncio.TimeoutError`` is an alias of the builtin from Python 3.11,
            which is this project's floor -- ruff's UP041 enforces the builtin.)
    """
    await _EXTRACT_SEMAPHORE.acquire()
    future = asyncio.ensure_future(asyncio.to_thread(reader, *args, **kwargs))
    future.add_done_callback(lambda _done: _EXTRACT_SEMAPHORE.release())
    try:
        return await asyncio.wait_for(asyncio.shield(future), EXTRACT_TIMEOUT_SECONDS)
    except TimeoutError:
        raise


def _image_gate(provider: str, model: str) -> None:
    """Refuse an image when the resolved model cannot read one.

    Raises:
        HTTPException: 422 with ``image_input_unsupported`` naming the model and
            both fixes.
    """
    support = supports_image_input(provider, model)
    if not support.supported:
        raise problem(422, CODE_IMAGE_UNSUPPORTED, support.reason, [support.reason])


def _resolve_route_for_image() -> tuple[str, str] | None:
    """The provider/model an image attachment will be sent to, if any.

    Returns:
        ``(provider, model)``, or ``None`` under dry run -- where nothing is sent
        to a model at all, so the image gate does not apply.

    Raises:
        HTTPException: 503 when no model is configured or its credential is
            missing, exactly as a generation would report it.
    """
    if runtime.dry_run_active():
        return None
    effective = resolve_effective_model(get_dsh_home(), None)
    if effective.source == "bundle-default":
        raise problem(
            503,
            "no_model_configured",
            "No model is configured yet, so an image could not be read. Choose a provider "
            "and add your API key in Model settings, or turn on Dry run mode to build and "
            "run offline.",
        )
    missing = credential_blocker(effective)
    if missing is not None:
        raise problem(503, "missing_credential", missing)
    return effective.provider, effective.model


def _map_attachment_error(exc: Exception, display: str) -> Exception:
    """Translate a store/extraction failure into an HTTP failure."""
    if isinstance(exc, UnsupportedAttachmentError):
        return problem(415, CODE_UNSUPPORTED_TYPE, str(exc), [str(exc)])
    if isinstance(exc, AttachmentTooLargeError):
        return problem(413, CODE_TOO_LARGE, str(exc), [str(exc)])
    if isinstance(exc, AttachmentReadError):
        return problem(422, CODE_UNREADABLE, str(exc), [str(exc)])
    if isinstance(exc, AttachmentNotFoundError):
        return problem(422, CODE_EXPIRED, str(exc), [str(exc)])
    if isinstance(exc, AttachmentStoreError):
        # The public half of the message: the exception's own text carries the
        # resolved path, which belongs in a server log, not in a response.
        return problem(
            500, "attachment_store_failed", exc.public_message, [exc.public_message]
        )
    return problem(500, "attachment_failed", f"{display!r} could not be processed.", [])


@router.post(
    "/graphs/attachments",
    response_model=AttachmentUploadResponse,
    responses={
        413: {"description": "the file exceeds the per-file size cap"},
        415: {"description": "the file type is not supported"},
        422: {"description": "the file could not be read, or the model cannot read images"},
        503: {"description": "the attachment readers are unavailable, or no model is configured"},
    },
)
async def upload_attachment_route(
    request: Request, file: Annotated[UploadFile, File()]
) -> AttachmentUploadResponse:
    """Store one uploaded file and return its id and a preview of what was read.

    Raises:
        HTTPException: 413 for an oversized body, 415 for an unsupported type,
            422 for an unreadable file or an image under a non-vision model, 503
            when the parsers are missing or no model is configured (images only).
    """
    try:
        # Lazy-degradation seam, like routes/generate.py: the extractors import
        # openpyxl/python-pptx inside their functions, so this import is cheap
        # but the modules themselves are optional at runtime.
        from swarm_builder.attachments import extract as extract_module
        from swarm_builder.attachments import store as store_module
    except ImportError as exc:
        raise problem(
            503,
            CODE_UNAVAILABLE,
            "file attachments are not available in this build: the readers "
            "(openpyxl, python-pptx) could not be imported.",
        ) from exc

    display = sanitize_filename(file.filename or "attachment")
    if _declared_length_too_large(request):
        limit_mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
        raise problem(
            413,
            CODE_TOO_LARGE,
            f"the file is larger than {limit_mb} MB; upload a smaller one.",
            [f"{display!r} exceeds the per-file limit of {limit_mb} MB."],
        )
    try:
        data = await _read_upload(file, display)
    except AttachmentTooLargeError as exc:
        raise _map_attachment_error(exc, display) from exc

    warnings: list[str] = []
    raw_name = file.filename or "attachment"
    declared_type = file.content_type
    try:
        kind, media_type = extract_module.detect_kind(raw_name, declared_type)
        # Read the file first, then consult the model. Both orders are defensible;
        # this one reports the more actionable failure first: "this file is not a
        # readable image" is about the file the user chose, while "no model is
        # configured" is about the app.
        extraction = await _extract_bounded(
            extract_module.extract, raw_name, data, declared_media_type=declared_type
        )
        if kind == "image":
            route = _resolve_route_for_image()
            if route is None:
                warnings.append(
                    "Dry run is on: the image is stored, but no model will read it."
                )
            else:
                _image_gate(route[0], route[1])
        record = store_module.save_attachment(
            get_workspace_dir(), raw_name, data, extraction, media_type, kind
        )
    except TimeoutError as exc:
        raise problem(
            422,
            CODE_UNREADABLE,
            f"{display!r} could not be read: reading it took longer than "
            f"{int(EXTRACT_TIMEOUT_SECONDS)} seconds.",
            ["the file is too complex to read within the time budget"],
        ) from exc
    except (
        UnsupportedAttachmentError,
        AttachmentTooLargeError,
        AttachmentReadError,
        AttachmentNotFoundError,
        AttachmentStoreError,
    ) as exc:
        raise _map_attachment_error(exc, display) from exc

    return AttachmentUploadResponse(attachment=_attachment_out(record), warnings=warnings)


@router.delete(
    "/graphs/attachments/{attachmentId}",
    response_model=AttachmentDeleteResponse,
    responses={404: {"description": "no such attachment"}},
)
async def delete_attachment_route(attachmentId: str) -> AttachmentDeleteResponse:
    """Discard one attachment (idempotent for the caller, 404 when unknown).

    Raises:
        HTTPException: 404 when the id is malformed, unknown, or already gone.
    """
    from swarm_builder.attachments import store as store_module

    try:
        deleted = store_module.delete_attachment(get_workspace_dir(), attachmentId)
    except AttachmentNotFoundError as exc:
        raise problem(404, CODE_EXPIRED, str(exc), [str(exc)]) from exc
    except AttachmentStoreError as exc:
        raise problem(
            500, "attachment_store_failed", exc.public_message, [exc.public_message]
        ) from exc
    if not deleted:
        raise problem(
            404,
            CODE_EXPIRED,
            f"there is no attachment with id {attachmentId!r}.",
            [],
        )
    return AttachmentDeleteResponse(attachment_id=attachmentId, deleted=True)


__all__ = [
    "CODE_EXPIRED",
    "CODE_IMAGE_UNSUPPORTED",
    "CODE_TOO_LARGE",
    "CODE_UNAVAILABLE",
    "CODE_UNREADABLE",
    "CODE_UNSUPPORTED_TYPE",
    "AttachmentDeleteResponse",
    "AttachmentOut",
    "AttachmentUploadResponse",
    "router",
]
