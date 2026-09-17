"""Path-safety primitives shared by :mod:`store.graphs` and
:mod:`store.projects`.

A graph or project id ultimately becomes a filesystem path segment (and,
via the HTTP route layer, a URL path parameter). Both destinations are
hostile input surfaces: a crafted id like ``"../../etc/passwd"`` must
never be allowed to join its way outside the workspace directory. This
module provides two independent, deliberately overlapping defenses:

1. :func:`validate_graph_id` -- a narrow charset/length check, applied
   at the *top* of every store function that accepts an id, before any
   path is even constructed.
2. :func:`resolve_within` -- a resolve-and-verify check, applied
   immediately before any read, write, or ``rmtree`` call, even though
   step 1 should already have rejected anything that could reach this
   point. This second check is not redundant: it also catches a symlink
   planted inside the workspace that points outside it, which a
   charset check on the *id* alone cannot see (the id can be perfectly
   well-formed while the path it resolves to, after following a
   symlink, is not).
"""

from __future__ import annotations

import re
from pathlib import Path

#: A graph or project id must be safe to use as a single path segment
#: (never containing `/`, `..`, or anything that could escape the
#: workspace directory when joined with a Path), and must round-trip
#: cleanly through a URL path parameter. Deliberately narrow: letters,
#: digits, underscore, hyphen only, 1-128 characters.
GRAPH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class InvalidGraphIdError(ValueError):
    """Raised for any id that fails the charset/length rule, or for any
    resolved path that would escape its intended root.

    Callers (store functions AND the HTTP route layer) must map this to
    a 422, never let a bad id or an escaping path reach a filesystem
    operation.
    """

    def __init__(self, graph_id: str) -> None:
        """Build the error, naming the rejected id and the rule it broke.

        Args:
            graph_id: The offending id, or the joined path when this is
                raised by :func:`resolve_within` for an escaping path.
        """
        self.graph_id = graph_id
        super().__init__(
            f"invalid graph id {graph_id!r}: must match {GRAPH_ID_RE.pattern}"
        )


def validate_graph_id(graph_id: str) -> None:
    """Raise :class:`InvalidGraphIdError` if ``graph_id`` is not a safe
    single path segment.

    Call this at the TOP of every ``store/graphs.py`` and
    ``store/projects.py`` function that accepts an id -- defense in
    depth, not only at the route layer.

    Args:
        graph_id: The candidate graph or project id.

    Raises:
        InvalidGraphIdError: If ``graph_id`` does not match
            :data:`GRAPH_ID_RE`.
    """
    if not GRAPH_ID_RE.match(graph_id):
        raise InvalidGraphIdError(graph_id)


def resolve_within(root: Path, *parts: str) -> Path:
    """Join ``root`` with ``parts``, resolve the result, and verify it
    is still inside ``root``.

    This is the defense-in-depth guard used immediately before ANY
    read, write, or ``rmtree`` in ``store/graphs.py`` and
    ``store/projects.py``, even though :func:`validate_graph_id` should
    already have caught anything that could escape -- belt and
    suspenders, and it also catches a symlink planted inside the
    workspace pointing outside it (see module docstring).

    Args:
        root: The directory the joined path must stay inside.
        *parts: Path segments to join onto ``root``.

    Returns:
        The resolved absolute path, guaranteed to be inside
        ``root.resolve()``.

    Raises:
        InvalidGraphIdError: If the resolved path is not relative to
            ``root.resolve()``.
    """
    resolved_root = root.resolve()
    joined = resolved_root.joinpath(*parts).resolve()
    if not joined.is_relative_to(resolved_root):
        # graph_id is not applicable here (this guards a whole path, not
        # necessarily a single id), so pass the joined string -- still
        # gives callers/logs the offending value.
        raise InvalidGraphIdError(str(Path(*parts)))
    return joined
