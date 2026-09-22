"""``/api/graphs/:id/runs`` and ``compile/run.py``.

Fast tests cover input coercion, staleness, the health check's run
readiness, the 422/409 refusals, the persisted record store, and the
tracer-line parser. The one ``slow`` test is the feature's proof: a
``SWARM_FAKE_FILL=1`` compile-if-stale followed by a real subprocess run
of the generated project against a keyless ``TestModel``
(``SWARM_RUN_TEST_MODEL=1``), driven end to end over HTTP, asserting the
per-node trace on the stream and the persisted record afterwards.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fixtures.graphs import fanout_join_graph, linear_chat_graph, mixed_programmatic_graph
from swarm_builder.compile import run as run_module
from swarm_builder.compile.jobs import GraphCompileInProgressError, Job, JobRegistry
from swarm_builder.compile.run import (
    RunInputError,
    _TracerState,
    coerce_input,
    project_is_stale,
)
from swarm_builder.main import create_app
from swarm_builder.models import SwarmGraph
from swarm_builder.routes import compile as compile_routes
from swarm_builder.store.runs import MAX_RETAINED_RUNS, RunRecord, list_runs, put_run

REPO_ROOT = Path(__file__).resolve().parent.parent
UV_CACHE_DIR = REPO_ROOT / ".uv-cache"

_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
_MAX_POLLS = 2400

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    monkeypatch.setenv("UV_CACHE_DIR", str(UV_CACHE_DIR))
    monkeypatch.delenv("SWARM_MODEL", raising=False)
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)
    monkeypatch.delenv("SWARM_API_KEY_ENV", raising=False)
    return tmp_path / "workspace"


@pytest.fixture(autouse=True)
def fresh_registry() -> Iterator[None]:
    compile_routes._reset_registry()
    yield
    compile_routes._reset_registry()


def _client() -> TestClient:
    return TestClient(create_app())


def _save_graph(client: TestClient, graph: SwarmGraph) -> str:
    body = json.loads(graph.model_dump_json(by_alias=True))
    response = client.put(f"/api/graphs/{graph.id}", json=body)
    assert response.status_code == 200, response.text
    return graph.id


def _parse_frames(text: str) -> list[dict[str, object]]:
    """Split an SSE body into frames (sse-starlette separates with CRLF)."""
    frames: list[dict[str, object]] = []
    for raw in re.split(r"\r?\n\r?\n", text.strip()):
        frame: dict[str, object] = {}
        for line in raw.splitlines():
            if line.startswith("event:"):
                frame["event"] = line[len("event:") :].strip()
            elif line.startswith("data:"):
                frame["data"] = json.loads(line[len("data:") :].strip())
            elif line.startswith("id:"):
                frame["id"] = int(line[len("id:") :].strip())
        if "event" in frame:
            frames.append(frame)
    return frames


# ---------------------------------------------------------------------------
# coerce_input
# ---------------------------------------------------------------------------


def test_coerce_str_passes_strings_and_refuses_others() -> None:
    assert coerce_input("hello", "str") == "hello"
    with pytest.raises(RunInputError):
        coerce_input({"a": 1}, "str")


def test_coerce_json_accepts_dict_or_json_text() -> None:
    assert coerce_input({"a": 1}, "json") == {"a": 1}
    assert coerce_input('{"a": 1}', "json") == {"a": 1}
    with pytest.raises(RunInputError, match="not valid JSON"):
        coerce_input("{nope", "json")
    with pytest.raises(RunInputError, match="JSON object"):
        coerce_input("[1, 2]", "json")


def test_coerce_list_str_requires_strings() -> None:
    assert coerce_input(["a", "b"], "list[str]") == ["a", "b"]
    assert coerce_input('["a"]', "list[str]") == ["a"]
    with pytest.raises(RunInputError):
        coerce_input([1, 2], "list[str]")


# ---------------------------------------------------------------------------
# project_is_stale
# ---------------------------------------------------------------------------


def _touch_project(project_dir: Path, *, tracer: bool = True) -> None:
    (project_dir / "src" / "swarm_workflow").mkdir(parents=True, exist_ok=True)
    (project_dir / "src" / "swarm_workflow" / "graph.py").write_text("# graph\n")
    if tracer:
        (project_dir / "run").mkdir(exist_ok=True)
        (project_dir / "run" / "stream_run.py").write_text("# tracer\n")


def test_stale_when_missing_or_without_tracer(tmp_path: Path) -> None:
    graph = linear_chat_graph()
    assert project_is_stale(tmp_path / "nope", graph)
    _touch_project(tmp_path, tracer=False)
    assert project_is_stale(tmp_path, graph)


def test_stale_follows_updated_at_against_graph_py_mtime(tmp_path: Path) -> None:
    _touch_project(tmp_path)
    graph_py = tmp_path / "src" / "swarm_workflow" / "graph.py"
    old_graph = linear_chat_graph()  # updated_at is 2024-01-01
    assert not project_is_stale(tmp_path, old_graph)

    fresh = old_graph.model_copy(update={"updated_at": datetime.now(UTC)})
    past = time.time() - 3600
    os.utime(graph_py, (past, past))
    assert project_is_stale(tmp_path, fresh)


# ---------------------------------------------------------------------------
# Tracer line parsing
# ---------------------------------------------------------------------------


def test_tracer_state_maps_lines_to_events() -> None:
    job = Job("r1", "g1", kind="run")
    state = _TracerState(job)
    state.stdout_line(json.dumps({"event": "run_started", "model": "test", "input": "x"}))
    state.stdout_line(json.dumps({"event": "node_started", "nodeId": "a", "inputs": "x"}))
    finished = {"event": "node_finished", "nodeId": "a", "output": "y", "stateDelta": {}}
    state.stdout_line(json.dumps({**finished, "durationMs": 3}))
    state.stdout_line("not json at all")
    state.stderr_line("warning: something")
    state.stdout_line(json.dumps({"event": "run_finished", "output": "y", "state": {}}))

    events = [(e.event_type, e.payload) for e in job.events_after(None)]
    assert events[0] == ("run", {"status": "started", "model": "test", "input": "x"})
    assert events[1] == ("node", {"nodeId": "a", "status": "started", "inputs": "x"})
    assert events[2][0] == "node"
    assert events[2][1]["status"] == "succeeded" and events[2][1]["output"] == "y"
    assert events[3] == ("log", {"message": "not json at all"})
    assert events[4] == ("log", {"message": "warning: something", "stream": "stderr"})
    assert state.result is not None and state.result["output"] == "y"
    assert state.model == "test"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_run_store_round_trips_and_prunes(tmp_path: Path) -> None:
    for index in range(MAX_RETAINED_RUNS + 3):
        put_run(
            tmp_path,
            RunRecord(
                run_id=f"r{index:03d}",
                graph_id="g1",
                status="succeeded",
                created_at=f"2026-01-01T00:00:{index:02d}+00:00",
            ),
        )
        # Distinct mtimes so pruning order is deterministic on coarse filesystems.
        time.sleep(0.002)
    records = list_runs(tmp_path, "g1")
    assert len(records) == MAX_RETAINED_RUNS
    assert records[0].run_id == f"r{MAX_RETAINED_RUNS + 2:03d}"
    assert list_runs(tmp_path, "other") == []


# ---------------------------------------------------------------------------
# HTTP refusals and health
# ---------------------------------------------------------------------------


def test_health_reports_run_readiness_fields(workspace: Path) -> None:
    _ = workspace
    body = _client().get("/api/health").json()
    assert body["runReady"] is False
    assert isinstance(body["runBlockers"], list)
    # Every compile blocker is also a run blocker.
    for blocker in body["blockers"]:
        assert blocker in body["runBlockers"]


def test_start_run_refuses_bad_input_with_422(workspace: Path) -> None:
    _ = workspace
    client = _client()
    graph_id = _save_graph(client, linear_chat_graph())
    response = client.post(f"/api/graphs/{graph_id}/runs", json={"input": {"not": "a str"}})
    assert response.status_code == 422
    assert "str" in response.json()["detail"]


def test_start_run_refuses_stale_project_when_compile_disabled(workspace: Path) -> None:
    _ = workspace
    client = _client()
    graph_id = _save_graph(client, linear_chat_graph())
    response = client.post(
        f"/api/graphs/{graph_id}/runs", json={"input": "hi", "compileIfStale": False}
    )
    assert response.status_code == 409
    assert "compile first" in response.json()["detail"]


def test_start_run_404_for_unknown_graph(workspace: Path) -> None:
    _ = workspace
    response = _client().post("/api/graphs/nope/runs", json={"input": "hi"})
    assert response.status_code == 404


def test_run_list_is_empty_before_any_run(workspace: Path) -> None:
    _ = workspace
    client = _client()
    graph_id = _save_graph(client, linear_chat_graph())
    assert client.get(f"/api/graphs/{graph_id}/runs").json() == {"runs": []}
    assert client.get(f"/api/graphs/{graph_id}/runs/missing").status_code == 404


def test_registry_blocks_a_run_while_a_compile_is_live() -> None:
    registry = JobRegistry()
    registry.register("c1", "g1")
    with pytest.raises(GraphCompileInProgressError):
        registry.register("r1", "g1", kind="run")
    assert registry.get("c1").snapshot()["kind"] == "compile"


async def test_run_project_refuses_stale_without_compile_hook(tmp_path: Path) -> None:
    registry = JobRegistry()
    registry.register("r1", "linear-chat", kind="run")
    with pytest.raises(run_module.ProjectStaleError):
        await run_module.run_project(
            linear_chat_graph(),
            registry=registry,
            run_id="r1",
            project_dir=tmp_path / "project",
            input_value="hi",
            compile_if_stale=None,
        )
    job = registry.get("r1")
    assert job.status == "failed"
    terminal = job.events_after(None)[-1]
    assert terminal.event_type == "error"
    assert terminal.payload["code"] == "run_failed"


async def test_run_project_reports_bad_input_as_failed_job(tmp_path: Path) -> None:
    registry = JobRegistry()
    registry.register("r1", "linear-chat", kind="run")
    with pytest.raises(RunInputError):
        await run_module.run_project(
            linear_chat_graph(),
            registry=registry,
            run_id="r1",
            project_dir=tmp_path,
            input_value=123,
            compile_if_stale=None,
        )
    assert registry.get("r1").status == "failed"


# ---------------------------------------------------------------------------
# End to end: compile-if-stale + real subprocess run (keyless)
# ---------------------------------------------------------------------------


def _poll_until_terminal(client: TestClient, job_id: str) -> dict[str, object]:
    status = client.get(f"/api/jobs/{job_id}").json()
    polls = 0
    while status["status"] not in _TERMINAL_STATUSES and polls < _MAX_POLLS:
        status = client.get(f"/api/jobs/{job_id}").json()
        polls += 1
    return status


@pytest.mark.slow
def test_run_compiles_when_stale_then_traces_every_step(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The feature's proof: no project yet, so the run compiles first
    (fake fill, real ``uv sync``), then executes ``run/stream_run.py`` with
    a keyless ``TestModel`` and streams one ``node`` frame per step."""
    _ = workspace
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    monkeypatch.setenv("SWARM_RUN_TEST_MODEL", "1")
    graph = mixed_programmatic_graph()
    with _client() as client:
        _assert_compile_then_run(client, graph)


def _assert_compile_then_run(client: TestClient, graph: SwarmGraph) -> None:
    graph_id = _save_graph(client, graph)

    start = client.post(f"/api/graphs/{graph_id}/runs", json={"input": "swarm builder"})
    assert start.status_code == 202, start.text
    body = start.json()
    assert body["willCompile"] is True
    run_id = body["runId"]

    status = _poll_until_terminal(client, run_id)
    assert status["kind"] == "run"
    assert status["status"] == "succeeded", status

    with client.stream("GET", f"/api/jobs/{run_id}/events") as response:
        frames = _parse_frames(response.read().decode())
    events = [frame["event"] for frame in frames]
    assert events[-1] == "done"
    # Compile phases streamed into the run's job, but no compile `done`.
    assert "phase" in events
    assert events.count("done") == 1

    run_statuses = [frame["data"]["status"] for frame in frames if frame["event"] == "run"]
    assert run_statuses == ["compiling", "starting", "started"]

    node_frames = [frame["data"] for frame in frames if frame["event"] == "node"]
    step_ids = sorted(
        node.id for node in graph.nodes if node.kind in ("agent", "programmatic")
    )
    started = sorted({f["nodeId"] for f in node_frames if f["status"] == "started"})
    succeeded = sorted({f["nodeId"] for f in node_frames if f["status"] == "succeeded"})
    assert started == step_ids
    assert succeeded == step_ids
    assert all("output" in f for f in node_frames if f["status"] == "succeeded")
    # Steps start in graph order: the entry node first.
    assert node_frames[0]["nodeId"] == graph.entry_node_id

    done = frames[-1]["data"]
    assert done["compiled"] is True
    assert isinstance(done["state"], dict)
    assert done["output"] is not None
    assert status["result"]["output"] == done["output"]

    # Persisted record mirrors the job.
    listed = client.get(f"/api/graphs/{graph_id}/runs").json()["runs"]
    assert [record["runId"] for record in listed] == [run_id]
    record = client.get(f"/api/graphs/{graph_id}/runs/{run_id}").json()
    assert record["status"] == "succeeded"
    assert record["compiled"] is True
    assert set(record["nodes"]) == set(step_ids)
    assert record["input"] == "swarm builder"

    # Second run: project is fresh now, so no compile precedes it.
    second = client.post(f"/api/graphs/{graph_id}/runs", json={"input": "again"})
    assert second.status_code == 202
    assert second.json()["willCompile"] is False
    second_status = _poll_until_terminal(client, second.json()["runId"])
    assert second_status["status"] == "succeeded", second_status
    with client.stream("GET", f"/api/jobs/{second.json()['runId']}/events") as response:
        second_frames = _parse_frames(response.read().decode())
    assert "phase" not in [frame["event"] for frame in second_frames]


@pytest.mark.slow
def test_fanout_run_traces_both_arms(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ = workspace
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    monkeypatch.setenv("SWARM_RUN_TEST_MODEL", "1")
    graph = fanout_join_graph()
    with _client() as client:
        _assert_fanout_run(client, graph)


def _assert_fanout_run(client: TestClient, graph: SwarmGraph) -> None:
    graph_id = _save_graph(client, graph)
    start = client.post(f"/api/graphs/{graph_id}/runs", json={"input": "topic"})
    assert start.status_code == 202, start.text
    run_id = start.json()["runId"]
    status = _poll_until_terminal(client, run_id)
    assert status["status"] == "succeeded", status
    with client.stream("GET", f"/api/jobs/{run_id}/events") as response:
        frames = _parse_frames(response.read().decode())
    node_frames = [f["data"] for f in frames if f["event"] == "node"]
    succeeded = {f["nodeId"] for f in node_frames if f["status"] == "succeeded"}
    arms = {node.id for node in graph.nodes if node.kind in ("agent", "programmatic")}
    assert succeeded == arms

