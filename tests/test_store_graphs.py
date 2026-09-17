"""Tests for :mod:`swarm_builder.store.graphs`.

Every test uses ``tmp_path`` as the workspace root -- never the real
repository ``workspace/`` directory -- so these tests can never leak
state into (or read stale state from) a real Swarm Builder install.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from swarm_builder.models import (
    NodeIo,
    Position,
    ProgrammaticSpec,
    SwarmGraph,
    SwarmNode,
)
from swarm_builder.store._ids import InvalidGraphIdError
from swarm_builder.store.graphs import (
    GraphNotFoundError,
    GraphStoreError,
    delete_graph,
    get_graph,
    graph_path,
    graphs_dir,
    list_graphs,
    put_graph,
)

UPDATED_AT = datetime(2024, 1, 1, tzinfo=UTC)


def _minimal_graph(graph_id: str = "g1") -> SwarmGraph:
    """The smallest document that satisfies SwarmGraph's structural
    validator: one node that is simultaneously the entry and exit node.

    ``kind="programmatic"`` with a bare ``ProgrammaticSpec()`` needs no
    agent instructions, tools, or LLM-shaped fields, keeping the
    fixture minimal per the task's guidance.
    """
    node = SwarmNode(
        id="only",
        kind="programmatic",
        title="Only node",
        intent="The single step of a minimal graph.",
        position=Position(x=0, y=0),
        io=NodeIo(input_type="str", output_type="str"),
        programmatic=ProgrammaticSpec(),
    )
    return SwarmGraph(
        id=graph_id,
        name="Minimal graph",
        entry_node_id="only",
        exit_node_id="only",
        state_fields=[],
        nodes=[node],
        edges=[],
        updated_at=UPDATED_AT,
    )


class TestRoundTrip:
    def test_put_then_get_round_trips(self, tmp_path: Path) -> None:
        graph = _minimal_graph("g1")
        put_graph(tmp_path, graph)

        loaded = get_graph(tmp_path, "g1")

        assert loaded == graph
        assert loaded.model_dump(by_alias=True) == graph.model_dump(by_alias=True)

    def test_on_disk_json_uses_camel_case_keys(self, tmp_path: Path) -> None:
        graph = _minimal_graph("g1")
        put_graph(tmp_path, graph)

        raw = graph_path(tmp_path, "g1").read_text(encoding="utf-8")
        data = json.loads(raw)

        assert "entryNodeId" in data
        assert "stateFields" in data
        assert "updatedAt" in data
        assert "entry_node_id" not in data
        assert "state_fields" not in data
        assert "updated_at" not in data

    def test_put_graph_leaves_no_stray_tmp_file(self, tmp_path: Path) -> None:
        graph = _minimal_graph("g1")
        put_graph(tmp_path, graph)

        entries = sorted(p.name for p in graphs_dir(tmp_path).iterdir())

        assert entries == ["g1.json"]


class TestNotFound:
    def test_get_graph_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(GraphNotFoundError):
            get_graph(tmp_path, "nope")

    def test_delete_graph_removes_file_then_second_delete_raises(self, tmp_path: Path) -> None:
        graph = _minimal_graph("g1")
        put_graph(tmp_path, graph)

        delete_graph(tmp_path, "g1")

        assert not graph_path(tmp_path, "g1").exists()
        with pytest.raises(GraphNotFoundError):
            delete_graph(tmp_path, "g1")


class TestListGraphs:
    def test_empty_or_nonexistent_dir_returns_empty(self, tmp_path: Path) -> None:
        assert list_graphs(tmp_path) == ([], [])

    def test_two_valid_graphs_are_returned_sorted(self, tmp_path: Path) -> None:
        put_graph(tmp_path, _minimal_graph("bravo"))
        put_graph(tmp_path, _minimal_graph("alpha"))

        graphs, errors = list_graphs(tmp_path)

        assert [g.id for g in graphs] == ["alpha", "bravo"]
        assert errors == []

    def test_one_bad_file_does_not_block_the_rest(self, tmp_path: Path) -> None:
        put_graph(tmp_path, _minimal_graph("good"))
        directory = graphs_dir(tmp_path)
        (directory / "bad.json").write_text("{not valid json", encoding="utf-8")

        graphs, errors = list_graphs(tmp_path)

        assert [g.id for g in graphs] == ["good"]
        assert len(errors) == 1
        bad_id, detail = errors[0]
        assert bad_id == "bad"
        assert detail  # non-empty error detail

    def test_one_file_failing_schema_validation_is_reported_not_raised(
        self, tmp_path: Path
    ) -> None:
        put_graph(tmp_path, _minimal_graph("good"))
        directory = graphs_dir(tmp_path)
        # Valid JSON, but missing required fields (no nodes/entryNodeId/etc).
        (directory / "invalid.json").write_text(
            json.dumps({"version": 1, "id": "invalid"}), encoding="utf-8"
        )

        graphs, errors = list_graphs(tmp_path)

        assert [g.id for g in graphs] == ["good"]
        assert [bad_id for bad_id, _ in errors] == ["invalid"]


class TestPathSafety:
    """Malicious ids must be rejected before any filesystem access."""

    @pytest.mark.parametrize("bad_id", ["../../etc/passwd", "../secret", "a/b"])
    def test_graph_path_rejects_malicious_id(self, tmp_path: Path, bad_id: str) -> None:
        with pytest.raises(InvalidGraphIdError):
            graph_path(tmp_path, bad_id)
        assert not tmp_path.exists() or list(tmp_path.rglob("*")) == []

    @pytest.mark.parametrize("bad_id", ["../../etc/passwd", "../secret", "a/b"])
    def test_get_graph_rejects_malicious_id(self, tmp_path: Path, bad_id: str) -> None:
        with pytest.raises(InvalidGraphIdError):
            get_graph(tmp_path, bad_id)
        assert not tmp_path.exists() or list(tmp_path.rglob("*")) == []

    @pytest.mark.parametrize("bad_id", ["../../etc/passwd", "../secret", "a/b"])
    def test_put_graph_rejects_malicious_id(self, tmp_path: Path, bad_id: str) -> None:
        graph = _minimal_graph(bad_id)
        with pytest.raises(InvalidGraphIdError):
            put_graph(tmp_path, graph)
        assert not tmp_path.exists() or list(tmp_path.rglob("*")) == []

    @pytest.mark.parametrize("bad_id", ["../../etc/passwd", "../secret", "a/b"])
    def test_delete_graph_rejects_malicious_id(self, tmp_path: Path, bad_id: str) -> None:
        with pytest.raises(InvalidGraphIdError):
            delete_graph(tmp_path, bad_id)
        assert not tmp_path.exists() or list(tmp_path.rglob("*")) == []


class TestWriteFailure:
    def test_os_replace_failure_raises_graph_store_error_and_cleans_up_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = _minimal_graph("g1")

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)

        with pytest.raises(GraphStoreError) as exc_info:
            put_graph(tmp_path, graph)

        expected_path = graph_path(tmp_path, "g1")
        assert str(expected_path) in str(exc_info.value)

        # The finally-unlink contract: no stray temp file left behind.
        leftover = list(graphs_dir(tmp_path).glob("*.tmp"))
        assert leftover == []
