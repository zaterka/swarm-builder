"""Filesystem storage for run records.

One record per run at ``<workspace_dir>/runs/<graph_id>/<run_id>.json``,
written when the run starts (status ``running``) and rewritten when it
ends, so a server restart still shows what ran and what it produced. The
in-memory job registry remains the live source of truth while a run is
in flight; this store is the history the Run panel lists.

Records are camelCase on the wire like every other document, atomic on
write like ``store/graphs.py``, and pruned to the newest
:data:`MAX_RETAINED_RUNS` per graph (the same depth the job registry
keeps).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from swarm_builder.store._ids import resolve_within, validate_graph_id

#: Newest-N records kept per graph; older ones are deleted on write.
MAX_RETAINED_RUNS = 20


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class NodeRunRecord(_CamelModel):
    """What one step reported during a run (the last ``node`` frame wins)."""

    status: str
    inputs: object | None = None
    output: object | None = None
    state_delta: dict[str, object] | None = None
    error: str | None = None
    duration_ms: int | None = None


class RunRecord(_CamelModel):
    """One persisted run."""

    run_id: str
    graph_id: str
    status: str
    created_at: str
    finished_at: str | None = None
    input: object | None = None
    output: object | None = None
    state: dict[str, object] | None = None
    error: str | None = None
    model: str | None = None
    duration_ms: int | None = None
    compiled: bool = False
    nodes: dict[str, NodeRunRecord] = Field(default_factory=dict)


class RunStoreError(RuntimeError):
    """Any I/O failure while reading or writing a run record."""


def runs_dir(workspace_dir: Path, graph_id: str) -> Path:
    """Return ``<workspace_dir>/runs/<graph_id>`` (not created)."""
    validate_graph_id(graph_id)
    return resolve_within(workspace_dir / "runs", graph_id)


def run_record_path(workspace_dir: Path, graph_id: str, run_id: str) -> Path:
    """Return the record path, validating both ids as path segments."""
    validate_graph_id(run_id)
    return resolve_within(runs_dir(workspace_dir, graph_id), f"{run_id}.json")


def put_run(workspace_dir: Path, record: RunRecord) -> None:
    """Atomically write ``record`` and prune the graph's oldest records.

    Raises:
        RunStoreError: If the directory cannot be created or the write
            fails; names the path.
    """
    path = run_record_path(workspace_dir, record.graph_id, record.run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".run-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(record.model_dump_json(by_alias=True, indent=2))
            handle.write("\n")
        os.replace(tmp_name, path)
    except OSError as exc:
        raise RunStoreError(f"could not write run record {path}: {exc}") from exc
    _prune(path.parent)


def get_run(workspace_dir: Path, graph_id: str, run_id: str) -> RunRecord | None:
    """Read one record, or ``None`` when absent."""
    path = run_record_path(workspace_dir, graph_id, run_id)
    if not path.is_file():
        return None
    try:
        return RunRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise RunStoreError(f"could not read run record {path}: {exc}") from exc


def list_runs(workspace_dir: Path, graph_id: str) -> list[RunRecord]:
    """Every readable record for ``graph_id``, newest first.

    An unreadable file is skipped rather than failing the list -- one
    corrupt record must not hide the rest.
    """
    directory = runs_dir(workspace_dir, graph_id)
    if not directory.is_dir():
        return []
    records: list[RunRecord] = []
    for path in directory.glob("*.json"):
        try:
            records.append(RunRecord.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    records.sort(key=lambda record: record.created_at, reverse=True)
    return records


def _prune(directory: Path) -> None:
    paths = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in paths[MAX_RETAINED_RUNS:]:
        try:
            stale.unlink()
        except OSError:
            continue


__all__ = [
    "MAX_RETAINED_RUNS",
    "NodeRunRecord",
    "RunRecord",
    "RunStoreError",
    "get_run",
    "list_runs",
    "put_run",
    "run_record_path",
    "runs_dir",
]
