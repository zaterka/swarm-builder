"""Filesystem storage for graph documents.

One graph document lives at ``<workspace_dir>/graphs/<graph_id>.json``,
serialized with the camelCase alias convention :class:`SwarmGraph`
mandates (see ``models.py``'s module docstring, "Naming convention" --
``by_alias=True`` on every write, never a bare ``model_dump()``).

Writes are atomic (write-to-temp-then-``os.replace``) so a crash or a
concurrent read mid-write can never observe a half-written JSON file.
Reads that hit a malformed or invalid file are reported per-file by
:func:`list_graphs` (one bad file must never break the rest of the
list) but propagate unwrapped from :func:`get_graph` (the route layer
owns turning a validation error into an HTTP response).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from swarm_builder.models import SwarmGraph
from swarm_builder.store._ids import resolve_within, validate_graph_id


class GraphNotFoundError(LookupError):
    """Raised by :func:`get_graph`/:func:`delete_graph` when no such
    graph file exists.

    Carries the graph id and the path that was checked, so callers can
    build a clear 404 detail message without re-deriving the path.
    """

    def __init__(self, graph_id: str, path: Path) -> None:
        """Build the error for a graph that is not on disk.

        Args:
            graph_id: The id that was requested.
            path: The resolved path that was checked and did not exist.
        """
        self.graph_id = graph_id
        self.path = path
        super().__init__(f"no graph {graph_id!r} at {path}")


class GraphStoreError(RuntimeError):
    """Raised for any I/O failure during a graph store operation.

    Covers a full disk, a permission denial, a cross-filesystem
    ``os.replace``, and an uncreatable graphs directory. Always names the
    resolved path that failed, because "the disk is full" is only
    actionable once you know which path the write was aimed at.
    """


def graphs_dir(workspace_dir: Path) -> Path:
    """Return ``<workspace_dir>/graphs``.

    Does NOT create it; callers that write create it on demand (see
    :func:`put_graph`), and callers that only read treat a missing
    directory as "no graphs yet" rather than an error.

    Args:
        workspace_dir: The workspace root.

    Returns:
        The graphs subdirectory path (not guaranteed to exist).
    """
    return workspace_dir / "graphs"


def graph_path(workspace_dir: Path, graph_id: str) -> Path:
    """Return ``<workspace_dir>/graphs/<graph_id>.json``.

    Validates ``graph_id`` first (:func:`validate_graph_id`) and
    resolves-within the graphs directory (:func:`resolve_within`)
    before returning -- this function is itself part of the defense-
    in-depth path-safety story, called by every other function below.

    Args:
        workspace_dir: The workspace root.
        graph_id: The graph id to resolve a path for.

    Returns:
        The resolved path to the graph's JSON file.

    Raises:
        InvalidGraphIdError: If ``graph_id`` is malformed or its
            resolved path would escape the graphs directory.
    """
    validate_graph_id(graph_id)
    return resolve_within(graphs_dir(workspace_dir), f"{graph_id}.json")


def list_graphs(workspace_dir: Path) -> tuple[list[SwarmGraph], list[tuple[str, str]]]:
    """List every graph stored under ``graphs_dir(workspace_dir)``.

    Parses and validates each ``*.json`` file directly under the
    directory through :class:`SwarmGraph`. One bad file (malformed
    JSON, or JSON that fails :class:`SwarmGraph` validation) must never
    prevent the rest from listing, so failures are collected rather
    than raised.

    Args:
        workspace_dir: The workspace root.

    Returns:
        A ``(valid_graphs, errors)`` pair. ``valid_graphs`` holds every
        successfully parsed :class:`SwarmGraph`, sorted by filename for
        determinism. ``errors`` holds a ``(graph_id, detail)`` pair for
        every file that failed to parse or validate, where ``graph_id``
        is the filename stem. Returns ``([], [])`` if ``graphs_dir``
        does not exist yet (never created just to list).
    """
    directory = graphs_dir(workspace_dir)
    if not directory.is_dir():
        return [], []

    valid_graphs: list[SwarmGraph] = []
    errors: list[tuple[str, str]] = []
    for path in sorted(directory.glob("*.json")):
        graph_id = path.stem
        try:
            raw_text = path.read_text(encoding="utf-8")
            data = json.loads(raw_text)
            graph = SwarmGraph.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            errors.append((graph_id, str(exc)))
            continue
        valid_graphs.append(graph)

    return valid_graphs, errors


def get_graph(workspace_dir: Path, graph_id: str) -> SwarmGraph:
    """Read and validate one graph document.

    Args:
        workspace_dir: The workspace root.
        graph_id: The id of the graph to read.

    Returns:
        The validated :class:`SwarmGraph`.

    Raises:
        InvalidGraphIdError: If ``graph_id`` is malformed.
        GraphNotFoundError: If the graph file does not exist.
        pydantic.ValidationError: If the file exists but fails
            :class:`SwarmGraph` validation -- left unwrapped so the
            route layer maps it to its own error response; this
            function does not need to catch it.
    """
    path = graph_path(workspace_dir, graph_id)
    if not path.is_file():
        raise GraphNotFoundError(graph_id, path)

    raw_text = path.read_text(encoding="utf-8")
    data = json.loads(raw_text)
    return SwarmGraph.model_validate(data)


def put_graph(workspace_dir: Path, graph: SwarmGraph) -> None:
    """Atomically write ``graph`` to ``graph_path(workspace_dir, graph.id)``.

    Atomicity contract: the JSON is written to a temp file created in
    the SAME directory as the destination (so the final
    ``os.replace`` never crosses a filesystem boundary, even when
    ``SWARM_WORKSPACE`` points at a different mount than the default),
    fsynced, then atomically renamed onto the destination. A crash or a
    concurrent reader can therefore never observe a partially written
    file.

    Args:
        workspace_dir: The workspace root.
        graph: The graph to persist. Its ``id`` field determines the
            destination path.

    Raises:
        InvalidGraphIdError: If ``graph.id`` is malformed.
        GraphStoreError: If the graphs directory cannot be created, or
            if any step of the write fails (disk full, permission
            denied, cross-filesystem replace, etc.). Names the resolved
            path that failed.
    """
    final_path = graph_path(workspace_dir, graph.id)
    directory = graphs_dir(workspace_dir)

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GraphStoreError(
            f"could not create graphs directory {directory}: {exc}"
        ) from exc

    # by_alias=True is not optional here: the on-disk contract is the
    # camelCase alias, per models.py's own stated naming convention.
    payload = graph.model_dump(mode="json", by_alias=True)
    text = json.dumps(payload, indent=2)

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=directory,
            delete=False,
            suffix=".tmp",
            encoding="utf-8",
        ) as f:
            tmp_path = Path(f.name)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, final_path)
    except OSError as exc:
        raise GraphStoreError(f"could not write graph to {final_path}: {exc}") from exc
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                # Best-effort cleanup; the primary error (if any) has
                # already been raised above and takes precedence.
                pass


def delete_graph(workspace_dir: Path, graph_id: str) -> None:
    """Delete the graph JSON file.

    Idempotent behavior is NOT implemented here (the route layer
    decides whether a missing graph on DELETE is a 404 or a no-op;
    this function just reports the honest outcome).

    Args:
        workspace_dir: The workspace root.
        graph_id: The id of the graph to delete.

    Raises:
        InvalidGraphIdError: If ``graph_id`` is malformed.
        GraphNotFoundError: If the graph file does not exist.
    """
    path = graph_path(workspace_dir, graph_id)
    if not path.is_file():
        raise GraphNotFoundError(graph_id, path)
    path.unlink()
