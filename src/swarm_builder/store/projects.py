"""Filesystem lifecycle for generated project directories.

Each graph, once compiled, gets exactly one directory at
``<workspace_dir>/projects/<graph_id>/``. This module owns only the
directory's *lifecycle* -- create, check existence, clear, delete -- not
its contents: writing scaffolded files into it belongs to
``compile/scaffold.py``, which calls :func:`ensure_project_dir` before
writing.

The one lifecycle rule with product-level weight is
:func:`clear_project_dir`. v1 does not merge prior edits on recompile,
so a recompile must start from a genuinely empty directory rather than
layering new files over stale ones from a previous compile -- leftovers
from an earlier graph shape would otherwise be validated (and reported)
as if they belonged to the current one.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from swarm_builder.store._ids import resolve_within, validate_graph_id


class ProjectStoreError(RuntimeError):
    """Raised for any I/O failure during a project directory lifecycle
    operation. Same contract as
    :class:`swarm_builder.store.graphs.GraphStoreError`: always names
    the resolved path that failed.
    """


def projects_dir(workspace_dir: Path) -> Path:
    """Return ``<workspace_dir>/projects``. Does not create it."""
    return workspace_dir / "projects"


def project_dir(workspace_dir: Path, graph_id: str) -> Path:
    """Return ``<workspace_dir>/projects/<graph_id>/``.

    Validates ``graph_id`` (:func:`validate_graph_id`) and resolves-
    within the projects root (:func:`resolve_within`) before returning
    -- same defense-in-depth posture as
    :func:`swarm_builder.store.graphs.graph_path`.

    Args:
        workspace_dir: The workspace root.
        graph_id: The graph id whose project directory to resolve.

    Returns:
        The resolved project directory path (not guaranteed to exist).

    Raises:
        InvalidGraphIdError: If ``graph_id`` is malformed or its
            resolved path would escape the projects directory.
    """
    validate_graph_id(graph_id)
    return resolve_within(projects_dir(workspace_dir), graph_id)


def project_exists(workspace_dir: Path, graph_id: str) -> bool:
    """Return True iff the project directory exists AND is a directory
    (not, say, a stray file that happens to have that name).

    Args:
        workspace_dir: The workspace root.
        graph_id: The graph id whose project directory to check.

    Returns:
        Whether a real project directory exists for ``graph_id``.

    Raises:
        InvalidGraphIdError: If ``graph_id`` is malformed.
    """
    return project_dir(workspace_dir, graph_id).is_dir()


def ensure_project_dir(workspace_dir: Path, graph_id: str) -> Path:
    """Create the project directory (and any missing parents) if
    absent, and return its path.

    This is the entry point ``scaffold()`` calls before writing files
    into a project directory; this function provides only the lifecycle
    primitive and never inspects or removes existing contents.

    Args:
        workspace_dir: The workspace root.
        graph_id: The graph id whose project directory to create.

    Returns:
        The resolved, now-existing project directory path.

    Raises:
        InvalidGraphIdError: If ``graph_id`` is malformed.
        ProjectStoreError: If the directory cannot be created. Names
            the resolved path.
    """
    path = project_dir(workspace_dir, graph_id)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectStoreError(f"could not create project directory {path}: {exc}") from exc
    return path


def clear_project_dir(workspace_dir: Path, graph_id: str) -> None:
    """Remove the ENTIRE contents of the project directory, then
    recreate it empty.

    A no-op (not an error) if the project directory does not exist yet.
    A recompile must start from a genuinely empty directory: v1 does not
    merge prior edits, and a file left over from an earlier graph shape
    would otherwise be validated as though it belonged to the current
    one.

    Args:
        workspace_dir: The workspace root.
        graph_id: The graph id whose project directory to clear.

    Raises:
        InvalidGraphIdError: If ``graph_id`` is malformed.
        ProjectStoreError: If the removal or recreation fails. Names
            the resolved path.
    """
    path = project_dir(workspace_dir, graph_id)
    if not path.exists():
        return

    # Re-verify immediately before the rmtree call as the very last
    # defense-in-depth check: never rmtree a path that hasn't just
    # been re-validated (this also re-catches a symlink swapped in
    # between the check above and this call).
    safe_path = resolve_within(projects_dir(workspace_dir), graph_id)
    try:
        shutil.rmtree(safe_path)
        safe_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectStoreError(f"could not clear project directory {safe_path}: {exc}") from exc


def delete_project_dir(workspace_dir: Path, graph_id: str) -> None:
    """Remove the project directory entirely (the directory itself, not
    just its contents).

    A no-op if it does not exist (this is what backs
    ``DELETE /api/graphs/:id?project=1`` -- deleting a graph whose
    project was never compiled must not be an error).

    Args:
        workspace_dir: The workspace root.
        graph_id: The graph id whose project directory to delete.

    Raises:
        InvalidGraphIdError: If ``graph_id`` is malformed.
        ProjectStoreError: If the removal fails. Names the resolved
            path.
    """
    path = project_dir(workspace_dir, graph_id)
    if not path.exists():
        return

    # Same last-moment re-verification as clear_project_dir.
    safe_path = resolve_within(projects_dir(workspace_dir), graph_id)
    try:
        shutil.rmtree(safe_path)
    except OSError as exc:
        raise ProjectStoreError(f"could not delete project directory {safe_path}: {exc}") from exc
