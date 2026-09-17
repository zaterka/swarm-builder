"""Slugification: derive a stable Python identifier from a node's ``title``.

Used by later groups (the emitter, the scaffolder) to turn a human-typed
title such as ``"Summarize results"`` into a Python-safe identifier such
as ``summarize_results`` for use as a ``builder.step(fn, node_id=...)``
argument, a step module filename (``steps/<slug>.py``), and the step
function's own name. Kept importable from a sibling module (rather than
living only inside a script) so both codegen and the test suite share one
implementation.

Algorithm (see PLAN.md "Edge cases" and codegen contract rule 11):

1. Unicode-normalize (NFKD) and drop everything that does not fold to
   ASCII -- this is what makes a fully non-ASCII title (e.g. all-CJK)
   collapse to nothing rather than mojibake. Note this folding is lossy
   for a *partially* non-ASCII title: NFKD decomposes some letters into
   an ASCII base plus a combining mark that step 1 then drops (e.g.
   ``"Straße"`` -> ``strae``, ``"Ωmega"`` -> ``mega``) rather than
   falling back to ``step_<index>`` -- the fallback only triggers when
   *nothing* ASCII survives. The result is always a valid identifier,
   just not always a visually obvious one.
2. Lowercase, then collapse every run of non ``[a-z0-9]`` characters to a
   single underscore, trimming leading/trailing underscores.
3. An empty result (non-ASCII or punctuation-only titles fold to nothing)
   falls back to ``step_<index>``, where ``index`` is the node's position
   in the input sequence -- guaranteed unique without needing dedup.
4. A leading digit is prefixed with an underscore, since
   ``[A-Za-z_][A-Za-z0-9_]*`` (a valid Python identifier) cannot start
   with a digit.
5. A result that collides with a Python keyword (``import``, ``class``,
   ...) gets a trailing underscore, the conventional Python escape.
6. ``slugify_titles`` processes a whole ordered list of titles and
   deduplicates collisions with a numeric suffix (``_2``, ``_3``, ...),
   checked against every slug produced so far -- including other
   already-deduplicated slugs -- so a title that happens to look like
   another title's dedup suffix can never collide silently.

The mapping is a pure function of the ordered title list: the same
titles in the same order always produce the same slugs, which is what
"stable across recompiles" (PLAN.md) requires.
"""

from __future__ import annotations

import keyword
import re
import unicodedata
from collections.abc import Sequence

#: A valid Python identifier, ASCII-only (titles are ASCII-folded before
#: this is checked, so this regex never needs to consider unicode
#: identifier rules).
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_NON_SLUG_CHARS_RE = re.compile(r"[^a-z0-9]+")


def _fallback(index: int) -> str:
    """Return the positional fallback slug for a title that folds to nothing."""
    return f"step_{index}"


def _base_slug(title: str, index: int) -> str:
    """Slugify a single title, with no dedup against other titles."""
    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    collapsed = _NON_SLUG_CHARS_RE.sub("_", lowered).strip("_")

    if not collapsed:
        # Non-ASCII or punctuation-only title -- nothing survived folding.
        return _fallback(index)

    if collapsed[0].isdigit():
        collapsed = f"_{collapsed}"

    if keyword.iskeyword(collapsed) or keyword.issoftkeyword(collapsed):
        collapsed = f"{collapsed}_"

    if not IDENTIFIER_RE.match(collapsed):
        # Defensive: the transformations above should always produce a
        # valid identifier, but never emit something codegen can't use.
        return _fallback(index)

    return collapsed


def slugify(title: str, index: int = 0) -> str:
    """Slugify one title in isolation (no cross-title deduplication).

    ``index`` seeds the ``step_<index>`` fallback for a title that folds
    to nothing. Prefer :func:`slugify_titles` when slugifying an entire
    node list, since only that function deduplicates collisions.
    """
    return _base_slug(title, index)


def slugify_titles(titles: Sequence[str]) -> list[str]:
    """Slugify an ordered list of titles, deduplicating collisions.

    Returns one slug per input title, in order. A title whose base slug
    was already used gets a numeric suffix (``_2``, ``_3``, ...); the
    search also skips any suffix that some other title has already
    claimed (organically or via its own dedup), so slugs never collide.
    """
    used: set[str] = set()
    result: list[str] = []
    for index, title in enumerate(titles):
        base = _base_slug(title, index)
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        result.append(candidate)
    return result
