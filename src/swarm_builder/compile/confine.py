"""Path confinement guard (PLAN.md fact 26; "Why not the harness": "a
four-line ``Path.resolve()`` + ``is_relative_to`` guard covers path
confinement, verified allowing ``step.py`` and blocking
``../../etc/passwd``").

This is, per PLAN.md, "the one security-relevant invariant in v1" --
the fill agent's tools (``read_file``, ``write_region``, ``parse_check``)
have no shell and no general-purpose file write, so this guard is the
entire attack surface for a path-traversal escape out of the generated
project directory. :func:`confine_path` is deliberately small and owned
by nothing else: every caller resolves a candidate path through it
*before* opening, reading, or writing anything.

Kept independent of :mod:`swarm_builder.store._ids`, which solves the
analogous problem for graph/project *ids* used as URL path parameters --
that module validates a narrow id charset first and only then resolves
within a workspace root. This module has no id charset to validate: its
callers hand it an already-decided candidate path (relative or
absolute) and need only the resolve-and-verify half of that defense.
"""

from __future__ import annotations

from pathlib import Path


class PathEscapesRootError(Exception):
    """Raised when a candidate path resolves outside its confinement root.

    Raised before any filesystem read or write is attempted against the
    candidate -- only :meth:`Path.resolve` (metadata-only symlink
    resolution, not a content read/write) runs ahead of the check.
    """

    def __init__(self, candidate: Path | str, root: Path) -> None:
        self.candidate = candidate
        self.root = root
        super().__init__(
            f"path {candidate!r} resolves outside confinement root {root}"
        )


def confine_path(root: Path, candidate: Path | str) -> Path:
    """Resolve ``candidate`` against ``root`` and verify it stays inside it.

    A relative ``candidate`` is joined onto ``root`` first; an absolute
    ``candidate`` is resolved as given, which lets a caller reject an
    absolute path outside the root without also rejecting one that
    happens to already sit inside it. Either way, the resolved path
    (post symlink-following) must remain within ``root.resolve()`` --
    this is what catches a symlink planted inside ``root`` whose target
    escapes it, which a purely lexical ``..``-count check cannot see.

    Args:
        root: The confinement boundary (e.g. a compiled project's
            directory).
        candidate: The path to confine, relative or absolute.

    Returns:
        The resolved absolute path, guaranteed to be inside
        ``root.resolve()``.

    Raises:
        PathEscapesRootError: If the resolved path is not relative to
            ``root.resolve()``. Raised before any I/O against
            ``candidate`` itself.
    """
    resolved_root = root.resolve()
    candidate_path = Path(candidate)
    joined = candidate_path if candidate_path.is_absolute() else resolved_root / candidate_path
    resolved = joined.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise PathEscapesRootError(candidate, resolved_root)
    return resolved


__all__ = ["PathEscapesRootError", "confine_path"]
