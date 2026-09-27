"""Every cap this feature enforces, in one module.

**Why one module.** The caps are a security and cost boundary, not tuning
knobs scattered next to the code that happens to use them: the upload route,
the extractors, the store and the prompt builder must agree on the same
numbers, and a reader asking "what is the largest thing a user can make this
server do?" should be able to answer it from one file. Every value here is
also asserted by ``tests/test_attachments_extract.py`` and
``tests/test_attachments_store.py``, so a change is a deliberate change.
"""

from __future__ import annotations

#: Files one generation may reference. Enforced at upload (the client blocks a
#: sixth file) and again server-side when a request names its attachments.
MAX_ATTACHMENTS = 5

#: Largest single upload, in bytes. Enforced twice on the wire: from the
#: ``Content-Length`` header before the body is read, and while the body is
#: streamed, so a lying or absent header cannot fill the disk.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

#: Largest total size of the attachments one generation may use. Checked
#: against the stored records' metadata, so nothing large is read to decide it.
MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024

#: Extracted text kept per file, and across one request. The whole prompt
#: context is bounded by the second value no matter how many files are given.
MAX_ATTACHMENT_CHARS = 20_000
MAX_TOTAL_ATTACHMENT_CHARS = 60_000

#: Longest display filename kept (never used to build a path).
MAX_ATTACHMENT_NAME_CHARS = 200

#: How long an uploaded attachment stays usable. Six hours is far longer than
#: any single generation, and short enough that scratch files do not collect.
ATTACHMENT_TTL_SECONDS = 6 * 60 * 60

#: Wall-clock budget for reading one file. Extraction is CPU/IO-bound and runs
#: in a worker thread; this is what turns a pathological file into a clean 422
#: instead of a request that never answers.
EXTRACT_TIMEOUT_SECONDS = 20.0

#: How many files may be parsed at once. Two is enough to keep a burst of
#: uploads moving and low enough that five concurrent 10 MiB workbooks cannot
#: multiply peak memory by five.
EXTRACT_CONCURRENCY = 2

#: Zip-bomb guard: the total *uncompressed* size a single .xlsx/.pptx may
#: declare. Checked before either parser is imported, because both expand the
#: whole container eagerly -- a 10 MiB upload can otherwise inflate to
#: gigabytes before any other cap in this file applies.
MAX_INFLATED_BYTES = 200 * 1024 * 1024

#: One spreadsheet cell / slide text run, before markdown escaping. Long cell
#: values are usually prose pasted into a cell, and they crowd out every real
#: row that follows.
MAX_CELL_CHARS = 200

#: Per-sheet and per-deck structure caps. All of them are reported back to the
#: user as notes rather than silently applied.
MAX_SHEETS = 5
MAX_SLIDES = 40
MAX_ROWS_PER_SHEET = 200
MAX_COLS = 30

#: How many formula cells may be echoed when a workbook computes its values.
MAX_FORMULA_CELLS = 60

#: Clarify-pass caps: questions shown, answers accepted, length of one answer.
MAX_CLARIFY_QUESTIONS = 4
MAX_ANSWERS = 8
MAX_ANSWER_CHARS = 2_000

#: Characters of extracted text returned to the panel so a user can see what
#: context is about to reach their model provider.
PREVIEW_CHARS = 400

#: CSV dialect sniffing reads this much, never the whole file: sniffing a
#: 10 MiB buffer is slow and can raise on quoted blobs that look like prose.
CSV_SNIFF_BYTES = 64 * 1024

#: Slack allowed above :data:`MAX_ATTACHMENT_BYTES` when checking the declared
#: ``Content-Length`` (multipart framing overhead).
UPLOAD_CONTENT_LENGTH_SLACK = 64 * 1024

#: Size of each read while streaming an upload to memory.
UPLOAD_CHUNK_BYTES = 1024 * 1024

__all__ = [
    "ATTACHMENT_TTL_SECONDS",
    "CSV_SNIFF_BYTES",
    "EXTRACT_CONCURRENCY",
    "EXTRACT_TIMEOUT_SECONDS",
    "MAX_ANSWERS",
    "MAX_ANSWER_CHARS",
    "MAX_ATTACHMENT_BYTES",
    "MAX_ATTACHMENT_CHARS",
    "MAX_ATTACHMENT_NAME_CHARS",
    "MAX_ATTACHMENTS",
    "MAX_CELL_CHARS",
    "MAX_CLARIFY_QUESTIONS",
    "MAX_COLS",
    "MAX_FORMULA_CELLS",
    "MAX_INFLATED_BYTES",
    "MAX_ROWS_PER_SHEET",
    "MAX_SHEETS",
    "MAX_SLIDES",
    "MAX_TOTAL_ATTACHMENT_BYTES",
    "MAX_TOTAL_ATTACHMENT_CHARS",
    "PREVIEW_CHARS",
    "UPLOAD_CHUNK_BYTES",
    "UPLOAD_CONTENT_LENGTH_SLACK",
]
