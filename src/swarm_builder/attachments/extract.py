"""Turn an uploaded file into bounded, model-readable text.

Three rules shape this module:

**Extension decides the parser.** A browser's declared content type is client
input, so the suffix is authoritative and the media type is only a hint when the
suffix is unknown. A text file renamed ``.xlsx`` is therefore *not* read as a
workbook -- it fails as an unreadable workbook, which is the honest answer.

**Nothing is unbounded.** Every parser is bounded on structure (sheets, slides,
rows, columns, cell length) and on output (characters), the zip containers are
checked for inflation *before* ``openpyxl``/``python-pptx`` is imported (both
expand a container eagerly), and the whole call is expected to run under a
timeout in a worker thread (``routes/attachments.py``). Truncation is never
silent: it is reported in ``Extraction.notes`` and in ``truncated``, which the
panel shows on the attachment chip.

**Parser messages never escape.** A parser's own exception text embeds the real
filename and sometimes the file's content, so it is mapped to a fixed sentence
here. This is the one place that knows how to translate a library's failure into
something safe to show a user.

The two heavy parsers are imported *inside* their extractor functions, which is
this repository's existing lazy-degradation seam (see
``routes/generate.py``): ``import swarm_builder.main`` must keep working on an
environment where the extras are missing, and the route turns the resulting
``ImportError`` into a 503 naming the cause.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Sequence

from swarm_builder.attachments.limits import (
    CSV_SNIFF_BYTES,
    MAX_ATTACHMENT_CHARS,
    MAX_CELL_CHARS,
    MAX_COLS,
    MAX_FORMULA_CELLS,
    MAX_INFLATED_BYTES,
    MAX_ROWS_PER_SHEET,
    MAX_SHEETS,
    MAX_SLIDES,
)
from swarm_builder.attachments.models import (
    MEDIA_TYPES,
    SUPPORTED_SUFFIXES,
    UNSUPPORTED_SUFFIX_FIXES,
    AttachmentKind,
    AttachmentReadError,
    Extraction,
    UnsupportedAttachmentError,
    sanitize_filename,
    suffix_of,
)

#: Encodings a .csv/.tsv is tried against, in order. ``latin-1`` never raises,
#: so it is the floor rather than a failure: a spreadsheet exported by a legacy
#: Windows tool should still produce something readable.
_TEXT_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

#: What ``extract`` reports for an image: no text, but the bytes travel to the
#: model separately as image content.
IMAGE_NOTE = "image attachment: sent to the model as image content, not as text"

#: Leading bytes every image format declares. A magic-byte check is the cheap
#: half of image validation: it catches the common user mistake (a PDF or a text
#: file renamed ``.png``) without decoding anything.
_IMAGE_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
}

#: Human names for the message, so a refusal says what the file is not.
_IMAGE_LABELS: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WebP",
}

#: Fixed cause sentences. Keyed by intent, not by exception class name, because
#: several libraries raise the same class for different reasons.
_CAUSE_UNREADABLE_CONTAINER = (
    # No slash in the sentence: it is shown to users and asserted against in
    # tests as "never contains a path", and a naive check cannot tell a format
    # list from a directory.
    "it is not a readable Excel or PowerPoint file (renamed, truncated, or corrupt)"
)
_CAUSE_UNREADABLE_WORKBOOK = "it is not a readable Excel workbook"
_CAUSE_UNREADABLE_DECK = "it is not a readable PowerPoint presentation"
_CAUSE_ENCRYPTED = "the workbook is password-protected"
_CAUSE_EMPTY = "the file is empty"
_CAUSE_NO_TEXT = "the file contains no readable text"
_CAUSE_GENERIC = "the file could not be parsed"


def supported_summary() -> str:
    """The supported extensions, for an error message a user can act on."""
    return ", ".join(sorted(SUPPORTED_SUFFIXES))


def detect_kind(
    filename: str, declared_media_type: str | None = None
) -> tuple[AttachmentKind, str]:
    """Decide which reader handles ``filename``.

    Args:
        filename: The client-supplied name; only its suffix is inspected.
        declared_media_type: The browser's content type, consulted only when the
            suffix is not one this app knows.

    Returns:
        The kind and the canonical media type to record and, for images, to send.

    Raises:
        UnsupportedAttachmentError: When neither the suffix nor the declared
            type matches a supported format. The message names the fix for the
            formats users actually try (.xls, .ppt, .pdf, .docx).
    """
    display = sanitize_filename(filename)
    suffix = suffix_of(filename)
    known = SUPPORTED_SUFFIXES.get(suffix)
    if known is not None:
        return known
    hint = (declared_media_type or "").split(";")[0].strip().lower()
    hinted = MEDIA_TYPES.get(hint)
    if hinted is not None:
        media_type = next(mt for k, mt in SUPPORTED_SUFFIXES.values() if k == hinted)
        return hinted, media_type
    fix = UNSUPPORTED_SUFFIX_FIXES.get(suffix)
    supported = supported_summary()
    if fix is not None:
        supported = f"{supported} ({fix})"
    raise UnsupportedAttachmentError(display, supported)


def extract(
    filename: str,
    data: bytes,
    *,
    declared_media_type: str | None = None,
) -> Extraction:
    """Read ``data`` as the format ``filename`` claims, bounded on every axis.

    Args:
        filename: The client-supplied name, used for the suffix and for the
            sanitized display name in any error.
        data: The uploaded bytes.
        declared_media_type: The browser's content type, used only as a hint.

    Returns:
        The extracted text (empty for images), whether it was truncated, and the
        notes explaining every cap that applied.

    Raises:
        UnsupportedAttachmentError: The format is not supported.
        AttachmentReadError: The file is empty, corrupt, encrypted, or expands
            past :data:`~swarm_builder.attachments.limits.MAX_INFLATED_BYTES`.
    """
    kind, _media_type = detect_kind(filename, declared_media_type)
    display = sanitize_filename(filename)
    if not data:
        raise AttachmentReadError(display, _CAUSE_EMPTY)
    if kind == "csv":
        return _extract_csv(display, data)
    if kind == "xlsx":
        return _extract_xlsx(display, data)
    if kind == "pptx":
        return _extract_pptx(display, data)
    _verify_image(display, data, _media_type)
    return Extraction(text="", truncated=False, notes=[IMAGE_NOTE])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _cell(value: object) -> str:
    """Render one cell/run as a single markdown-table-safe value."""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    text = text.replace("|", "\\|")
    if len(text) > MAX_CELL_CHARS:
        text = text[:MAX_CELL_CHARS] + "…"
    return text


def _markdown_table(rows: Sequence[Sequence[object]]) -> str:
    """Render rows as a markdown table, first row as the header.

    Padding to a common width keeps the table renderable when the source has
    ragged rows (a CSV with a short last line, a spreadsheet with merged cells).
    """
    if not rows:
        return "_(no rows)_"
    width = max(len(row) for row in rows)
    header = [_cell(value) for value in rows[0]] + [""] * (width - len(rows[0]))
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    for row in rows[1:]:
        cells = [_cell(value) for value in row] + [""] * (width - len(row))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _clip(text: str, limit: int = MAX_ATTACHMENT_CHARS) -> tuple[str, bool]:
    """Cut ``text`` to ``limit`` characters, reporting whether it was cut."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _guard_inflation(display: str, data: bytes) -> None:
    """Refuse a zip container whose members declare an implausible size.

    Runs before either parser is imported. ``openpyxl`` and ``python-pptx``
    expand the whole container, so this is the only place a compressed bomb can
    be stopped without reading it.

    Raises:
        AttachmentReadError: The container is unreadable or inflates too far.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            total = 0
            for info in archive.infolist():
                total += info.file_size
                if total > MAX_INFLATED_BYTES:
                    limit_mb = MAX_INFLATED_BYTES // (1024 * 1024)
                    raise AttachmentReadError(
                        display, f"it expands to more than {limit_mb} MB and was not read"
                    )
    except zipfile.BadZipFile as exc:
        raise AttachmentReadError(display, _CAUSE_UNREADABLE_CONTAINER) from exc


# ---------------------------------------------------------------------------
# CSV / TSV
# ---------------------------------------------------------------------------


def _decode(data: bytes) -> tuple[str, str | None]:
    """Decode text as UTF-8, falling back through cp1252 to latin-1.

    Returns:
        The decoded text and, when a fallback was used, the encoding to mention
        in a note (``None`` for UTF-8, the expected case).
    """
    for encoding in _TEXT_ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if encoding in ("utf-8-sig", "utf-8"):
            return text, None
        return text, f"the file was decoded as {encoding}"
    # Unreachable: latin-1 decodes any byte sequence, errors="replace" is belt
    # and braces for a future change to the tuple above.
    return data.decode("latin-1", errors="replace"), "the file was decoded as latin-1"


def _extract_csv(display: str, data: bytes) -> Extraction:
    """Read a delimiter-separated file as a bounded markdown table."""
    text, encoding_note = _decode(data)
    if not text.strip("\x00\ufeff \t\r\n"):
        raise AttachmentReadError(display, _CAUSE_NO_TEXT)
    try:
        dialect: csv.Dialect | type[csv.Dialect] = csv.Sniffer().sniff(
            text[:CSV_SNIFF_BYTES], delimiters=",;\t|"
        )
    except csv.Error:
        # Sniffing is a heuristic over arbitrary prose; falling back to a comma
        # is better than refusing a file a user can read.
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows: list[list[str]] = []
    truncated = False
    for index, row in enumerate(reader):
        if index >= MAX_ROWS_PER_SHEET:
            truncated = True
            break
        if len(row) > MAX_COLS:
            truncated = True
        values = list(row[:MAX_COLS])
        if any(str(value).strip() for value in values if value is not None):
            rows.append(values)
    notes: list[str] = []
    if encoding_note is not None:
        notes.append(encoding_note)
    if truncated:
        notes.append(f"only the first {MAX_ROWS_PER_SHEET} rows and {MAX_COLS} columns were read")
    if not rows:
        raise AttachmentReadError(display, _CAUSE_NO_TEXT)
    body = _markdown_table(rows)
    text_out, clipped = _clip(body)
    if clipped:
        notes.append(f"only the first {MAX_ATTACHMENT_CHARS} characters were kept")
    return Extraction(text=text_out, truncated=truncated or clipped, notes=notes)


def _verify_image(display: str, data: bytes, media_type: str) -> None:
    """Refuse bytes that are not a decodable image of the declared format.

    Two layers, both cheap, both aimed at a *user* mistake rather than a
    theoretical attack: the magic bytes must match the format the extension
    claims, and Pillow must be able to open and verify the file. Without this,
    a truncated or renamed image is accepted at upload and fails later as a
    provider 400 in the middle of a generation, with a message written for a
    developer (this happened with the first hand-built test fixture).

    A decompression bomb -- a small file declaring an enormous pixel count --
    is refused by Pillow's own guard, which is why this runs in the same
    worker thread and timeout as every other parse.

    Raises:
        AttachmentReadError: The bytes are not an image of the declared format.
    """
    signatures = _IMAGE_SIGNATURES.get(media_type)
    if signatures is not None and not data.startswith(signatures):
        label = _IMAGE_LABELS.get(media_type, media_type)
        raise AttachmentReadError(
            display, f"it does not look like a {label} image (the file may be renamed or truncated)"
        )
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover -- PIL ships with python-pptx.
        raise AttachmentReadError(
            display, "image validation is unavailable in this build"
        ) from exc
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except UnidentifiedImageError as exc:
        raise AttachmentReadError(display, "it is not a readable image") from exc
    except Image.DecompressionBombError as exc:
        raise AttachmentReadError(display, "the image declares too many pixels to read") from exc
    except Exception as exc:  # noqa: BLE001 -- every decode failure is the same answer.
        raise AttachmentReadError(display, "the image is truncated or corrupt") from exc


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def _map_workbook_error(display: str, exc: Exception) -> AttachmentReadError:
    """Translate a parser failure into a fixed, user-safe sentence."""
    if isinstance(exc, zipfile.BadZipFile):
        return AttachmentReadError(display, _CAUSE_UNREADABLE_CONTAINER)
    if isinstance(exc, KeyError):
        # openpyxl raises KeyError for an encrypted workbook's parts.
        return AttachmentReadError(display, _CAUSE_ENCRYPTED)
    return AttachmentReadError(display, _CAUSE_UNREADABLE_WORKBOOK)


def _extract_xlsx(display: str, data: bytes) -> Extraction:
    """Read an .xlsx workbook as one markdown table per sheet, plus formulas."""
    _guard_inflation(display, data)
    import openpyxl

    notes: list[str] = []
    truncated = False
    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True
        )
    except Exception as exc:  # noqa: BLE001 -- every parser failure is mapped below.
        raise _map_workbook_error(display, exc) from exc

    sheet_names = list(workbook.sheetnames)
    if len(sheet_names) > MAX_SHEETS:
        notes.append(f"only the first {MAX_SHEETS} of {len(sheet_names)} sheets were read")
        truncated = True
    blocks: list[str] = []
    try:
        for name in sheet_names[:MAX_SHEETS]:
            sheet = workbook[name]
            rows: list[list[str]] = []
            expected_rows = sheet.max_row or 0
            expected_cols = sheet.max_column or 0
            if expected_rows > MAX_ROWS_PER_SHEET or expected_cols > MAX_COLS:
                truncated = True
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= MAX_ROWS_PER_SHEET:
                    truncated = True
                    break
                values = list(row[:MAX_COLS])
                if any(str(value).strip() for value in values if value is not None):
                    rows.append(values)
            if not rows:
                blocks.append(f'### Sheet "{name}" (empty)')
                continue
            blocks.append(
                f'### Sheet "{name}" ({len(rows)} rows x '
                f"{min(expected_cols or len(rows[0]), MAX_COLS)} cols)\n" + _markdown_table(rows)
            )
    except Exception as exc:  # noqa: BLE001 -- a corrupt sheet fails the whole file, mapped.
        raise _map_workbook_error(display, exc) from exc
    finally:
        _close_quietly(workbook)

    if truncated:
        notes.append(
            f"only the first {MAX_ROWS_PER_SHEET} rows and {MAX_COLS} columns of each "
            "sheet were read"
        )
    formulas = _collect_formulas(data, sheet_names[:MAX_SHEETS])
    if formulas:
        notes.append(
            "this workbook computes values with formulas; the formulas are included below "
            "because a formula cell without a cached value reads as empty"
        )
        blocks.append("### Formulas\n" + "\n".join(formulas))
    if not blocks:
        raise AttachmentReadError(display, _CAUSE_NO_TEXT)
    text, clipped = _clip("\n\n".join(blocks))
    if clipped:
        notes.append(f"only the first {MAX_ATTACHMENT_CHARS} characters were kept")
    return Extraction(text=text, truncated=truncated or clipped, notes=notes)


def _collect_formulas(data: bytes, sheet_names: Sequence[str]) -> list[str]:
    """Gather up to :data:`MAX_FORMULA_CELLS` formulas as ``Sheet!A1 =IF(...)``.

    A second, bounded pass over the same file with ``data_only=False``. Business
    thresholds and routing rules routinely live in formulas, and a workbook whose
    cached values were never computed would otherwise reach the model as a grid
    of blanks -- which is precisely the case this feature exists for.

    Args:
        data: The workbook bytes.
        sheet_names: The sheets to scan (already capped by the caller).

    Returns:
        Formula lines, or an empty list when the workbook has none or the pass
        fails -- this is enrichment, so it must never fail the whole file.
    """
    import openpyxl

    collected: list[str] = []
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except Exception:  # noqa: BLE001 -- the values pass already succeeded; skip quietly.
        return []
    try:
        for name in sheet_names:
            sheet = workbook[name]
            for row in sheet.iter_rows(min_row=1, max_row=MAX_ROWS_PER_SHEET, max_col=MAX_COLS):
                for cell in row:
                    if getattr(cell, "data_type", None) != "f":
                        continue
                    formula = _cell(getattr(cell, "value", ""))
                    if not formula:
                        continue
                    collected.append(f"- {name}!{cell.coordinate} {formula}")
                    if len(collected) >= MAX_FORMULA_CELLS:
                        return collected
    except Exception:  # noqa: BLE001 -- best-effort enrichment, never a failure.
        return collected
    finally:
        _close_quietly(workbook)
    return collected


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------


def _extract_pptx(display: str, data: bytes) -> Extraction:
    """Read a .pptx deck as per-slide text, tables and speaker notes."""
    _guard_inflation(display, data)
    import pptx

    notes: list[str] = []
    try:
        presentation = pptx.Presentation(io.BytesIO(data))
        slides = list(presentation.slides)
    except Exception as exc:  # noqa: BLE001 -- mapped to a fixed sentence below.
        raise AttachmentReadError(display, _CAUSE_UNREADABLE_DECK) from exc

    capped_slides = len(slides) > MAX_SLIDES
    if capped_slides:
        notes.append(f"only the first {MAX_SLIDES} of {len(slides)} slides were read")
    blocks: list[str] = []
    for index, slide in enumerate(slides[:MAX_SLIDES], start=1):
        title = _slide_title(slide)
        lines = [f"## Slide {index} — {title}" if title else f"## Slide {index}"]
        for shape in slide.shapes:
            if getattr(shape, "has_table", False):
                table_rows = [
                    [cell.text for cell in row.cells] for row in shape.table.rows
                ][:MAX_ROWS_PER_SHEET]
                lines.append(_markdown_table(table_rows))
                continue
            if getattr(shape, "has_text_frame", False):
                text = " ".join(shape.text_frame.text.split())
                if text and text != title:
                    lines.append(text)
        notes_text = _slide_notes(slide)
        if notes_text:
            lines.append(f"Notes: {notes_text}")
        blocks.append("\n".join(lines))
    if not blocks:
        raise AttachmentReadError(display, _CAUSE_NO_TEXT)
    text, clipped = _clip("\n\n".join(blocks))
    if clipped:
        notes.append(f"only the first {MAX_ATTACHMENT_CHARS} characters were kept")
    return Extraction(text=text, truncated=clipped or capped_slides, notes=notes)


def _slide_title(slide: object) -> str:
    """The slide's title text, or '' when it has no title placeholder."""
    shapes = getattr(slide, "shapes", None)
    title = getattr(shapes, "title", None) if shapes is not None else None
    text = getattr(title, "text", "") if title is not None else ""
    return " ".join(str(text).split())


def _slide_notes(slide: object) -> str:
    """The slide's speaker notes, or '' when it has none.

    ``has_notes_slide`` is checked first: touching ``notes_slide`` on a deck
    without notes materializes an empty one, which python-pptx warns about.
    """
    try:
        if not getattr(slide, "has_notes_slide", False):
            return ""
        text_frame = slide.notes_slide.notes_text_frame
        return " ".join(text_frame.text.split())
    except Exception:  # noqa: BLE001 -- notes are enrichment; never fail the file.
        return ""


def _close_quietly(workbook: object) -> None:
    """Close a read-only workbook, ignoring a failure to do so."""
    close = getattr(workbook, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:  # noqa: BLE001 -- a failed close must not mask the result.
        return


__all__ = [
    "IMAGE_NOTE",
    "detect_kind",
    "extract",
    "supported_summary",
]
