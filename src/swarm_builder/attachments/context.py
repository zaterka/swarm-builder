"""Render loaded attachments into one bounded block of drafter context.

The drafter receives three things in a fixed order: the user's description
(the instruction), their answers to the clarifying questions (authoritative),
and this context block (evidence). Keeping them separate matters -- a model
handed a spreadsheet with no framing will happily turn every row into a node,
and a model handed a table *as evidence* will use its column names, thresholds
and vocabulary without inventing steps.

The block is bounded by
:data:`~swarm_builder.attachments.limits.MAX_TOTAL_ATTACHMENT_CHARS` regardless
of how many files were attached: files are appended in the order the user
attached them, the one that crosses the budget is cut with an explicit marker,
and any that never fit are counted in a closing note. Truncation is always
disclosed, because a model (and a user) must be able to tell that context is
missing rather than absent from the source.

Pure and deterministic: no I/O, no clock, no environment.
"""

from __future__ import annotations

from collections.abc import Sequence

from swarm_builder.attachments.limits import MAX_TOTAL_ATTACHMENT_CHARS
from swarm_builder.attachments.models import KIND_LABELS, LoadedAttachment

#: The framing header. Written for the model, not for a user: it says what the
#: blocks are and, more importantly, what they are not.
CONTEXT_HEADER = (
    "The user attached these supplementary files. Treat them as business CONTEXT "
    "(field names, terminology, routing rules, thresholds, sample data) — not as a "
    "list of steps to model. Do not create a node per row or per slide."
)

#: Appended in place of the remainder of a block that crossed the budget.
TRUNCATION_MARKER = "[truncated: context budget reached]"


def render_attachments_context(loaded: Sequence[LoadedAttachment]) -> str:
    """Build the context block for a drafter prompt.

    Args:
        loaded: The attachments the request named, in the user's order. Images
            are skipped here -- their bytes travel as image content instead.

    Returns:
        The block, or '' when there is no text-bearing attachment to describe.
    """
    textual = [item for item in loaded if item.record.kind != "image"]
    if not textual:
        return ""

    total = len(loaded)
    parts: list[str] = [CONTEXT_HEADER]
    used = 0
    omitted = 0
    for index, item in enumerate(loaded, start=1):
        if item.record.kind == "image":
            continue
        record = item.record
        label = KIND_LABELS[record.kind]
        header = (
            f'--- file {index}/{total}: "{record.filename}" '
            f"({label}, {record.chars} chars) ---"
        )
        remaining = MAX_TOTAL_ATTACHMENT_CHARS - used
        if remaining <= 0:
            omitted += 1
            continue
        body = item.text
        if len(body) > remaining:
            body = f"{body[:remaining]}\n{TRUNCATION_MARKER}"
        used += len(body)
        block = [header]
        if record.notes:
            block.append("notes: " + "; ".join(record.notes))
        block.append(body)
        block.append(f'--- end file {index}/{total} ---')
        parts.append("\n".join(block))

    if omitted:
        parts.append(
            f"[{omitted} file(s) omitted: the {MAX_TOTAL_ATTACHMENT_CHARS}-character "
            "context budget was reached]"
        )
    if len(parts) == 1:
        return ""
    return "\n\n".join(parts)


__all__ = [
    "CONTEXT_HEADER",
    "TRUNCATION_MARKER",
    "render_attachments_context",
]
