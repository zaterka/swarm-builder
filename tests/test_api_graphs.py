"""Tests for graph CRUD + review routes (``/api/graphs*``)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_builder.main import create_app
from swarm_builder.models import (
    NodeIo,
    Position,
    ProgrammaticSpec,
    SwarmGraph,
    SwarmNode,
)
from swarm_builder.store.projects import ensure_project_dir

PLACEHOLDER_UPDATED_AT = datetime(2000, 1, 1, tzinfo=UTC)


def _minimal_graph(graph_id: str = "g1") -> SwarmGraph:
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
        updated_at=PLACEHOLDER_UPDATED_AT,
    )


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    return TestClient(create_app())


def test_put_stamps_updated_at_and_get_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    graph = _minimal_graph("g1")
    body = graph.model_dump(mode="json", by_alias=True)

    put_resp = client.put("/api/graphs/g1", json=body)
    assert put_resp.status_code == 200
    put_body = put_resp.json()
    assert put_body["updatedAt"] != body["updatedAt"]

    get_resp = client.get("/api/graphs/g1")
    assert get_resp.status_code == 200
    get_body = get_resp.json()

    assert get_body["updatedAt"] == put_body["updatedAt"]
    for key in put_body:
        if key == "updatedAt":
            continue
        assert get_body[key] == put_body[key]


def test_list_graphs_reports_node_and_edge_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    graph = _minimal_graph("g1")
    client.put("/api/graphs/g1", json=graph.model_dump(mode="json", by_alias=True))

    resp = client.get("/api/graphs")
    assert resp.status_code == 200
    body = resp.json()

    assert body["errors"] == []
    summary = next(g for g in body["graphs"] if g["id"] == "g1")
    assert summary["nodeCount"] == 1
    assert summary["edgeCount"] == 0


def test_put_body_id_mismatch_is_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    graph = _minimal_graph("g1")

    resp = client.put("/api/graphs/other-id", json=graph.model_dump(mode="json", by_alias=True))
    assert resp.status_code == 422


def test_delete_without_project_flag_keeps_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    graph = _minimal_graph("g1")
    client.put("/api/graphs/g1", json=graph.model_dump(mode="json", by_alias=True))

    workspace_dir = tmp_path / "workspace"
    project_path = ensure_project_dir(workspace_dir, "g1")
    assert project_path.is_dir()

    del_resp = client.delete("/api/graphs/g1")
    assert del_resp.status_code == 200
    del_body = del_resp.json()
    assert del_body["deleted"] is True
    assert del_body["projectDeleted"] is False

    get_resp = client.get("/api/graphs/g1")
    assert get_resp.status_code == 404

    assert project_path.is_dir()


def test_delete_with_project_flag_removes_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    graph = _minimal_graph("g1")
    client.put("/api/graphs/g1", json=graph.model_dump(mode="json", by_alias=True))

    workspace_dir = tmp_path / "workspace"
    project_path = ensure_project_dir(workspace_dir, "g1")
    assert project_path.is_dir()

    del_resp = client.delete("/api/graphs/g1", params={"project": "true"})
    assert del_resp.status_code == 200
    del_body = del_resp.json()
    assert del_body["projectDeleted"] is True

    assert not project_path.exists()


#: Status codes that all constitute a *safe* rejection of a traversal id.
#: 404/422 are the store guard (InvalidGraphIdError) surfacing through the
#: route. 405 is also safe and arrives without our code running at all:
#: Starlette normalizes a path like `/api/graphs/..` to `/api/graphs`
#: before routing, and that path has no PUT/DELETE handler. What must
#: never happen is a 500 (an unhandled crash) or a 2xx (the id being
#: honored) -- both are asserted separately below.
SAFE_REJECTION_CODES = (404, 405, 422)


@pytest.mark.parametrize("bad_id", ["..", "foo%2Fbar"])
def test_traversal_ids_are_rejected_and_never_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_id: str
) -> None:
    client = _client(tmp_path, monkeypatch)
    graph = _minimal_graph("g1")

    get_resp = client.get(f"/api/graphs/{bad_id}")
    assert get_resp.status_code in SAFE_REJECTION_CODES

    put_resp = client.put(
        f"/api/graphs/{bad_id}", json=graph.model_dump(mode="json", by_alias=True)
    )
    assert put_resp.status_code in SAFE_REJECTION_CODES

    del_resp = client.delete(f"/api/graphs/{bad_id}")
    assert del_resp.status_code in SAFE_REJECTION_CODES

    # The invariant that actually matters: nothing escaped the workspace.
    workspace_dir = tmp_path / "workspace"
    assert not (workspace_dir / "graphs" / "etc").exists()
    assert not (tmp_path / "etc").exists()


def test_review_route_on_valid_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    graph = _minimal_graph("g1")
    client.put("/api/graphs/g1", json=graph.model_dump(mode="json", by_alias=True))

    resp = client.post("/api/graphs/g1/review")
    assert resp.status_code == 200
    body = resp.json()
    assert "ok" in body
    assert "errors" in body
    assert "warnings" in body
    assert isinstance(body["errors"], list)
    assert isinstance(body["warnings"], list)


def test_review_route_on_missing_graph_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)

    resp = client.post("/api/graphs/nope/review")
    assert resp.status_code == 404


def test_get_corrupt_graph_file_is_422_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A graph file that is not valid JSON at all -- e.g. a write that
    was interrupted mid-flight before this app's own atomic-replace
    machinery could have produced it, or a hand-edit gone wrong -- must
    never reach the client as an uncaught exception. GET should map it
    to a well-formed 422 naming the corrupt path, exactly like
    ``GET /api/graphs`` already does for the same input via its own
    ``errors`` array."""
    client = _client(tmp_path, monkeypatch)
    graphs_dir = tmp_path / "workspace" / "graphs"
    graphs_dir.mkdir(parents=True)
    (graphs_dir / "corrupt.json").write_text("{not valid json", encoding="utf-8")

    resp = client.get("/api/graphs/corrupt")
    assert resp.status_code == 422
    assert resp.status_code != 500
    detail = resp.json()["detail"]
    assert "corrupt" in detail


def test_get_schema_invalid_graph_file_is_422_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid JSON that fails SwarmGraph's own pydantic validation (here:
    an entry_node_id naming a node that does not exist) must likewise
    be a 422, not an uncaught ValidationError."""
    client = _client(tmp_path, monkeypatch)
    graphs_dir = tmp_path / "workspace" / "graphs"
    graphs_dir.mkdir(parents=True)
    (graphs_dir / "badschema.json").write_text(
        '{"id": "badschema", "name": "x", "entryNodeId": "missing", '
        '"exitNodeId": "missing", "stateFields": [], "nodes": [], '
        '"edges": [], "updatedAt": "2000-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    resp = client.get("/api/graphs/badschema")
    assert resp.status_code == 422
    assert resp.status_code != 500


def test_review_corrupt_graph_file_is_422_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review route loads the graph through the exact same helper
    as GET, so it must degrade the same way for a corrupt file."""
    client = _client(tmp_path, monkeypatch)
    graphs_dir = tmp_path / "workspace" / "graphs"
    graphs_dir.mkdir(parents=True)
    (graphs_dir / "corrupt.json").write_text("{not valid json", encoding="utf-8")

    resp = client.post("/api/graphs/corrupt/review")
    assert resp.status_code == 422
    assert resp.status_code != 500
