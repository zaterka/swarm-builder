"""Filesystem lifecycle for generated project directories.

Each graph, once compiled, gets exactly one directory under
``<workspace_dir>/projects/`` -- keyed, since this change, by a slug of the
graph's *name* rather than its id: ``support-triage/`` instead of a UUID. The
directory's *lifecycle* (create, claim, find, rename, delete) lives here; its
*contents* belong to ``compile/scaffold.py``, which writes into the directory
this module has claimed.

**Ownership is a marker file.** A name is not a key -- two graphs can both be
called "Support Triage" -- so every claimed directory carries a
``.swarm-project.json`` marker (``{"graphId": ..., "graphName": ...}``) written
before the first scaffold file. Claiming then means: probe ``<slug>``, and if a
directory already exists there, read its marker. Mine → reuse (a recompile
clears the contents); someone else's (or no marker at all) → take
``<slug>-2``, then ``<slug>-3``, and so on. The marker is also what makes
delete safe: a directory is only removed when its marker names the graph being
deleted, so two same-named graphs can never clear each other's work.

The id-keyed functions below remain as the legacy path: projects compiled
before this change live at ``projects/<graph_id>/`` with no marker, and still
resolve for export/run/delete via the id-keyed fallback in
:func:`project_dir_for` / :func:`langgraph_project_dir_for`.

The one lifecycle rule with product-level weight is
:func:`clear_project_dir`. v1 does not merge prior edits on recompile, so a
recompile must start from a genuinely empty directory rather than layering new
files over stale ones from a previous compile.
"""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
from pathlib import Path

from swarm_builder.store._ids import InvalidGraphIdError, resolve_within, validate_graph_id


class ProjectStoreError(RuntimeError):
    """Raised for any I/O failure during a project directory lifecycle
    operation. Same contract as
    :class:`swarm_builder.store.graphs.GraphStoreError`: always names
    the resolved path that failed.
    """


# ---------------------------------------------------------------------------
# Slug derivation and the ownership marker
# ---------------------------------------------------------------------------

#: Marker file at the root of every claimed project directory.
MARKER_NAME = ".swarm-project.json"

#: Maximum length of a derived directory slug. Kept short so the full path
#: stays sane on Windows/OneDrive where the workspace may already be deep.
_SLUG_MAX_LEN = 48

#: Non-slug characters collapse to a single hyphen (directories read better
#: with dashes than underscores, and this slug also becomes the PEP 508 project
#: name, where hyphens are conventional).
_NON_SLUG_CHARS_RE = re.compile(r"[^a-z0-9]+")


def project_dir_name(name: str, graph_id: str) -> str:
    """Derive the directory (and package) name for a graph.

    NFKD-normalizes, folds to ASCII, lowercases, and collapses runs of
    non-``[a-z0-9]`` to a single hyphen. A name that folds to nothing (all-CJK,
    punctuation-only) falls back to ``project-<id8>``. The result always matches
    ``GRAPH_ID_RE`` (``[A-Za-z0-9_-]``), so every existing path-safety primitive
    keeps working unchanged.

    Args:
        name: The graph's display name (free text).
        graph_id: The graph id, used only for the fallback prefix.

    Returns:
        A safe, bounded directory/package name.
    """
    normalized = unicodedata.normalize("NFKD", name or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    collapsed = _NON_SLUG_CHARS_RE.sub("-", ascii_only.lower()).strip("-")

    if not collapsed:
        collapsed = f"project-{graph_id[:8]}"
    if len(collapsed) > _SLUG_MAX_LEN:
        collapsed = collapsed[:_SLUG_MAX_LEN].rstrip("-")
    if not collapsed:
        collapsed = f"project-{graph_id[:8]}"
    return collapsed


def _marker_path(project_dir: Path) -> Path:
    return project_dir / MARKER_NAME


def _read_marker(project_dir: Path) -> dict[str, object] | None:
    """Read a directory's ownership marker, or ``None`` when absent/unreadable."""
    try:
        document = json.loads(_marker_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _marker_matches(project_dir: Path, graph_id: str) -> bool:
    marker = _read_marker(project_dir)
    return marker is not None and marker.get("graphId") == graph_id


def write_project_marker(project_dir: Path, graph_id: str, name: str) -> None:
    """Write (or rewrite) the canonical ownership marker into ``project_dir``.

    Called by each compile pipeline after it clears the directory and before
    the boundary baseline is captured, so a recompile that wiped the directory
    still leaves a marker behind. The fill agent has no tool that can create or
    modify this file, and ``boundary.py`` excludes it, so it is never part of
    the boundary contract.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"graphId": graph_id, "graphName": name}, indent=2) + "\n"
    _marker_path(project_dir).write_text(payload, encoding="utf-8")


def _find_marked_dir(root: Path, graph_id: str) -> Path | None:
    """Return the directory under ``root`` whose marker names ``graph_id``."""
    if not root.is_dir():
        return None
    try:
        entries = sorted(root.iterdir())
    except OSError:  # pragma: no cover - defensive
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            safe = resolve_within(root, entry.name)
        except InvalidGraphIdError:
            continue
        if _marker_matches(safe, graph_id):
            return safe
    return None


def _next_free_dir(root: Path, base: str, graph_id: str) -> Path:
    """Return ``root/<base>``, or the first ``<base>-2``/``<base>-3``... that is
    either free or already marked as ``graph_id``'s own."""
    candidate = base
    suffix = 2
    while True:
        path = resolve_within(root, candidate)
        if not path.exists() or _marker_matches(path, graph_id):
            return path
        candidate = f"{base}-{suffix}"
        suffix += 1


# ---------------------------------------------------------------------------
# Legacy id-keyed lifecycle (pre-name project directories)
# ---------------------------------------------------------------------------


def projects_dir(workspace_dir: Path) -> Path:
    """Return ``<workspace_dir>/projects``. Does not create it."""
    return workspace_dir / "projects"


def project_dir(workspace_dir: Path, graph_id: str) -> Path:
    """Return ``<workspace_dir>/projects/<graph_id>/`` (the legacy id-keyed path)."""
    validate_graph_id(graph_id)
    return resolve_within(projects_dir(workspace_dir), graph_id)


def project_exists(workspace_dir: Path, graph_id: str) -> bool:
    """Whether a directory exists at the legacy id-keyed project path."""
    return project_dir(workspace_dir, graph_id).is_dir()


def ensure_project_dir(workspace_dir: Path, graph_id: str) -> Path:
    """Create the legacy id-keyed project directory and return its path.

    Preserved for tests and for anything that still writes a pre-name project;
    new compiles go through :func:`claim_project_dir` instead.
    """
    path = project_dir(workspace_dir, graph_id)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectStoreError(f"could not create project directory {path}: {exc}") from exc
    return path


def clear_project_dir(workspace_dir: Path, graph_id: str) -> None:
    """Remove the ENTIRE contents of the legacy id-keyed project directory."""
    path = project_dir(workspace_dir, graph_id)
    if not path.exists():
        return
    safe_path = resolve_within(projects_dir(workspace_dir), graph_id)
    try:
        shutil.rmtree(safe_path)
        safe_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectStoreError(f"could not clear project directory {safe_path}: {exc}") from exc


def delete_project_dir(workspace_dir: Path, graph_id: str) -> None:
    """Remove the legacy id-keyed project directory entirely."""
    path = project_dir(workspace_dir, graph_id)
    if not path.exists():
        return
    safe_path = resolve_within(projects_dir(workspace_dir), graph_id)
    try:
        shutil.rmtree(safe_path)
    except OSError as exc:
        raise ProjectStoreError(f"could not delete project directory {safe_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Name-keyed lifecycle
# ---------------------------------------------------------------------------


def claim_project_dir(workspace_dir: Path, graph_id: str, name: str) -> Path:
    """Claim (creating if necessary) the name-keyed project directory.

    Probes ``<slug>``, then ``<slug>-2``, ... until it finds a directory that is
    either absent (claimed atomically with ``mkdir``) or already marked as this
    graph's own (a recompile). Writes a provisional marker; the compile pipeline
    rewrites the canonical one after clearing.

    Raises:
        InvalidGraphIdError: If ``graph_id`` is malformed.
        ProjectStoreError: If the directory cannot be created.
    """
    validate_graph_id(graph_id)
    root = projects_dir(workspace_dir)
    base = project_dir_name(name, graph_id)

    while True:
        path = resolve_within(root, base)
        if path.is_dir() and _marker_matches(path, graph_id):
            return path  # already ours; pipeline will clear and re-mark
        try:
            path.mkdir(parents=True)
        except FileExistsError:
            base = f"{project_dir_name(name, graph_id)}-{_suffix_for(base)}"
            continue
        except OSError as exc:
            raise ProjectStoreError(f"could not create project directory {path}: {exc}") from exc
        write_project_marker(path, graph_id, name)
        return path


def _suffix_for(candidate: str) -> int:
    """Return the next numeric suffix after ``candidate``'s trailing ``-N``."""
    if "-" in candidate and candidate.rsplit("-", 1)[-1].isdigit():
        return int(candidate.rsplit("-", 1)[-1]) + 1
    return 2


def project_dir_for(workspace_dir: Path, graph_id: str) -> Path | None:
    """Return the name-keyed project directory for ``graph_id``, or ``None``.

    Marker scan first (survives a rename or a failed directory move), then the
    legacy ``projects/<graph_id>/`` fallback for pre-name compiles.
    """
    validate_graph_id(graph_id)
    marked = _find_marked_dir(projects_dir(workspace_dir), graph_id)
    if marked is not None:
        return marked
    legacy = project_dir(workspace_dir, graph_id)
    return legacy if legacy.is_dir() else None


def delete_project_for_graph(workspace_dir: Path, graph_id: str, name: str) -> None:
    """Remove the graph's project directory, marker-verified before ``rmtree``.

    Resolves via :func:`project_dir_for` (marker scan, then legacy id fallback).
    A directory whose marker names another graph is never deleted; the legacy
    id-keyed path is only deleted when it carries no marker (it cannot belong to
    a name-keyed graph). ``name`` is accepted for symmetry with the other
    graph-aware functions and is currently unused -- the marker, not the name,
    is the authority.
    """
    del name  # marker is the authority; name is kept for API symmetry
    path = project_dir_for(workspace_dir, graph_id)
    if path is None:
        return
    if _read_marker(path) is not None and not _marker_matches(path, graph_id):
        return
    safe_path = resolve_within(projects_dir(workspace_dir), path.name)
    try:
        shutil.rmtree(safe_path)
    except OSError as exc:
        raise ProjectStoreError(f"could not delete project directory {safe_path}: {exc}") from exc


def rename_project_dirs(workspace_dir: Path, graph_id: str, new_name: str) -> None:
    """Best-effort rename of both project trees when a graph's name changes.

    The marker scan in :func:`project_dir_for` already keeps a renamed graph's
    directory findable, so this is a cosmetic improvement -- move the directory
    to match the new name when that is possible, and leave it in place when it
    is not (a target collision, an in-flight compile, an I/O error). The next
    compile re-claims under the new name regardless.
    """
    _rename_tree(projects_dir(workspace_dir), graph_id, new_name)
    _rename_tree(langgraph_projects_dir(workspace_dir), graph_id, new_name)


def _rename_tree(root: Path, graph_id: str, new_name: str) -> None:
    if not root.is_dir():
        return
    current = _find_marked_dir(root, graph_id)
    if current is None:
        return
    base = project_dir_name(new_name, graph_id)
    target = _next_free_dir(root, base, graph_id)
    if target == current:
        return
    try:
        current.rename(target)
    except OSError:  # pragma: no cover - best-effort by contract
        return
    write_project_marker(target, graph_id, new_name)


# ---------------------------------------------------------------------------
# LangGraph export directories: a sibling tree, never nested inside the
# pydantic-graph project (a recompile clears that whole directory, and the
# boundary check would report a nested export as unexpected files).
# ---------------------------------------------------------------------------


def langgraph_projects_dir(workspace_dir: Path) -> Path:
    """Return ``<workspace_dir>/projects-langgraph``. Does not create it."""
    return workspace_dir / "projects-langgraph"


def langgraph_project_dir(workspace_dir: Path, graph_id: str) -> Path:
    """Return ``<workspace_dir>/projects-langgraph/<graph_id>/`` (legacy, validated)."""
    validate_graph_id(graph_id)
    return resolve_within(langgraph_projects_dir(workspace_dir), graph_id)


def langgraph_project_exists(workspace_dir: Path, graph_id: str) -> bool:
    """Whether a LangGraph export directory exists for ``graph_id`` (legacy)."""
    return langgraph_project_dir(workspace_dir, graph_id).is_dir()


def delete_langgraph_project_dir(workspace_dir: Path, graph_id: str) -> None:
    """Remove the legacy id-keyed LangGraph export directory."""
    path = langgraph_project_dir(workspace_dir, graph_id)
    if not path.exists():
        return
    safe_path = resolve_within(langgraph_projects_dir(workspace_dir), graph_id)
    try:
        shutil.rmtree(safe_path)
    except OSError as exc:
        raise ProjectStoreError(
            f"could not delete LangGraph project directory {safe_path}: {exc}"
        ) from exc


def claim_langgraph_project_dir(workspace_dir: Path, graph_id: str, name: str) -> Path:
    """Claim the name-keyed LangGraph export directory (mirror of :func:`claim_project_dir`)."""
    validate_graph_id(graph_id)
    root = langgraph_projects_dir(workspace_dir)
    base = project_dir_name(name, graph_id)

    while True:
        path = resolve_within(root, base)
        if path.is_dir() and _marker_matches(path, graph_id):
            return path
        try:
            path.mkdir(parents=True)
        except FileExistsError:
            base = f"{project_dir_name(name, graph_id)}-{_suffix_for(base)}"
            continue
        except OSError as exc:
            raise ProjectStoreError(
                f"could not create LangGraph project directory {path}: {exc}"
            ) from exc
        write_project_marker(path, graph_id, name)
        return path


def langgraph_project_dir_for(workspace_dir: Path, graph_id: str) -> Path | None:
    """Return the name-keyed LangGraph export directory, or ``None``."""
    validate_graph_id(graph_id)
    marked = _find_marked_dir(langgraph_projects_dir(workspace_dir), graph_id)
    if marked is not None:
        return marked
    legacy = langgraph_project_dir(workspace_dir, graph_id)
    return legacy if legacy.is_dir() else None


def delete_langgraph_project_for_graph(workspace_dir: Path, graph_id: str, name: str) -> None:
    """Remove the LangGraph export directory, marker-verified (see
    :func:`delete_project_for_graph`)."""
    del name
    path = langgraph_project_dir_for(workspace_dir, graph_id)
    if path is None:
        return
    if _read_marker(path) is not None and not _marker_matches(path, graph_id):
        return
    safe_path = resolve_within(langgraph_projects_dir(workspace_dir), path.name)
    try:
        shutil.rmtree(safe_path)
    except OSError as exc:
        raise ProjectStoreError(
            f"could not delete LangGraph project directory {safe_path}: {exc}"
        ) from exc
