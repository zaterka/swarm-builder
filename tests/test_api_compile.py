"""Tests for the compile HTTP routes (``/api/compile*``).

Replaces ``tests/test_api_compile_seam.py``, which asserted Group 3's
documented 501 seam; that seam is now the real job orchestration, so
these tests assert the contract instead of the placeholder.

Everything runs with no model: Phase 3 is either ``SWARM_FAKE_FILL=1``
or a scripted stand-in for the whole pipeline, and every test points
``SWARM_WORKSPACE``/``DSH_HOME``/``UV_CACHE_DIR`` at ``tmp_path`` so no
test can read or write the developer's real ``~/.dsh`` or this
repository's ``workspace/``. Only the end-to-end test runs a real
subprocess and a real ``uv sync``, so only that one is marked ``slow``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fixtures.graphs import mixed_programmatic_graph
from swarm_builder.compile.jobs import EventLogGapError, Job
from swarm_builder.compile.pipeline import (
    ERROR_CODE_PHASE_FAILED,
    PHASE_BOUNDARY,
    PHASE_FILL,
    PHASE_NAMES,
    PHASE_REVIEW,
    PHASE_SCAFFOLD,
    PHASE_STATUS_FAILED,
    PHASE_STATUS_STARTED,
    PHASE_STATUS_SUCCEEDED,
    PHASE_VALIDATE,
)
from swarm_builder.main import create_app
from swarm_builder.models import SwarmGraph
from swarm_builder.routes import compile as compile_routes

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Cache directory for the one real ``uv`` test. Named explicitly rather
#: than relying on ``config``'s default so the value the route resolves
#: is the same one this suite warms.
UV_CACHE_DIR = REPO_ROOT / ".uv-cache"

#: ``SWARM_FAKE_FILL`` enable value (``compile.pipeline.FAKE_FILL_ENABLED_VALUE``).
FAKE_FILL_ENABLED = "1"

#: A saved fixture graph, for the helpers that need a document but never
#: touch its contents.
TEST_GRAPH = mixed_programmatic_graph()

#: Event types that terminate an SSE stream.
TERMINAL_EVENT_TYPES = frozenset({"done", "error"})

_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")

#: Seconds between polls of the status snapshot in the end-to-end test.
#: A fake-fill compile runs a real ``uv sync``, so this is only an
#: upper bound on how often the test asks, never a deadline.
_POLL_INTERVAL_SECONDS = 0.05

#: Upper bound on those polls. Generous: it exists to fail the test with
#: a job status rather than to hang forever, not to time a compile.
_MAX_POLLS = 1200


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every path this suite resolves at ``tmp_path``.

    ``DSH_HOME`` deliberately points at a directory that does not exist,
    which is the ordinary "no harness installed" state: Phase 1 then
    resolves the bundle default route and needs no key, so a compile can
    run under ``SWARM_FAKE_FILL=1`` with no credentials at all.

    Returns:
        The (not yet created) workspace root.
    """
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    monkeypatch.setenv("UV_CACHE_DIR", str(UV_CACHE_DIR))
    monkeypatch.delenv("SWARM_MODEL", raising=False)
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)
    monkeypatch.delenv("SWARM_API_KEY_ENV", raising=False)
    return tmp_path / "workspace"


@pytest.fixture(autouse=True)
def fresh_registry() -> Iterator[None]:
    """Give every test its own compile-job registry.

    ``routes/compile.py`` deliberately keeps one process-wide registry
    (jobs must outlive a request); a test that leaked jobs into the next
    test would see spurious 409s from a graph id it never compiled, so
    the singleton is reset on both sides.
    """
    compile_routes._reset_registry()
    yield
    compile_routes._reset_registry()


def _client() -> TestClient:
    """Build a client over a fresh app against the test's temp dirs.

    Returns:
        A :class:`TestClient` with the app's lifespan active, so the
        shutdown hook really runs at the end of the ``with`` block.
    """
    return TestClient(create_app())


def _save_graph(client: TestClient, graph: SwarmGraph) -> str:
    """Persist a fixture graph through the real PUT route.

    Saving through HTTP rather than writing the JSON file directly is
    deliberate: the compile route must load what the graphs route
    actually writes.

    Args:
        client: The app to write through.
        graph: The document to save.

    Returns:
        The graph id.
    """
    response = client.put(
        f"/api/graphs/{graph.id}", json=graph.model_dump(mode="json", by_alias=True)
    )
    assert response.status_code == 200, response.text
    return graph.id


def _parse_frames(body: str) -> list[dict[str, object]]:
    """Parse an SSE body into one dict per frame.

    Normalizes ``\\r\\n`` first: ``sse-starlette`` separates frames with
    ``\\r\\n\\r\\n`` (the SSE spec's own separator), and slicing on a bare
    ``\\n\\n`` would silently return a single oversized frame.

    Args:
        body: The raw response text.

    Returns:
        One entry per frame, each with ``id``/``event`` (``None`` when
        absent) and ``data`` (the parsed JSON payload, ``None`` for a
        comment-only frame).
    """
    frames: list[dict[str, object]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, object] = {"id": None, "event": None, "data": None}
        for line in block.splitlines():
            if line.startswith("id: "):
                frame["id"] = line[4:]
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("data: "):
                frame["data"] = json.loads(line[6:])
        frames.append(frame)
    return frames


def _comments(body: str) -> list[str]:
    """Every ``:`` comment line of an SSE body, in order."""
    normalized = body.replace("\r\n", "\n")
    return [line[1:].strip() for line in normalized.splitlines() if line.startswith(":")]


def _frame_ids(frames: list[dict[str, object]]) -> list[int]:
    """The integer ``id`` of every frame that carries one."""
    return [int(str(frame["id"])) for frame in frames if frame["id"] is not None]


async def _blocked_pipeline(*args: object, **kwargs: object) -> None:
    """A never-completing stand-in for ``run_compile``.

    Used where a test needs a live job: the real pipeline finishes too
    fast (and, in fake-fill mode, spends 30+ seconds doing it) to be a
    reliable target. It marks the job ``running`` first, exactly as
    ``run_compile`` does, so "live" is observable rather than assumed.

    Args:
        *args: Ignored.
        **kwargs: Ignored (``registry``/``compile_id`` arrive as keywords).
    """
    del args
    kwargs["registry"].get(kwargs["compile_id"]).mark_running()
    await asyncio.Event().wait()


async def _quit_pipeline(*args: object, **kwargs: object) -> None:
    """An immediately-succeeding stand-in for ``run_compile``.

    Like the real pipeline, it leaves the job finished -- it does not
    emit ``run_compile``'s events, so it is only usable where the test
    cares about the job's *status* rather than about the stream.

    Args:
        *args: Ignored.
        **kwargs: Ignored (``compile_id`` arrives as a keyword).
    """
    del args
    registry = kwargs["registry"]
    registry.mark_succeeded(kwargs["compile_id"], {"projectPath": "/tmp/example"})


def _live_snapshot(client: TestClient, compile_id: str) -> dict[str, object]:
    """Return a job's snapshot, asserting it has not finished yet.

    Args:
        client: The app to ask.
        compile_id: The job to inspect.

    Returns:
        The snapshot, with a non-terminal status.
    """
    status = client.get(f"/api/compile/{compile_id}").json()
    assert status["status"] in ("queued", "running"), status
    return status


def _scripted_pipeline(graph: SwarmGraph, *, outcome: str = PHASE_STATUS_SUCCEEDED):
    """Build a ``run_compile`` stand-in that emits one full phase cycle.

    The frames it emits are the pipeline's real event vocabulary, so the
    SSE assertions cover the actual shapes a browser will parse without
    paying for scaffold/``uv sync`` on every test.

    Args:
        graph: The document being "compiled" (unused beyond the
            signature; the scripted events are document-independent).
        outcome: ``succeeded`` for a ``done`` event, ``failed`` for an
            ``error`` event that follows a failed phase.

    Returns:
        An async callable matching ``run_compile``'s signature.
    """

    async def scripted(
        _graph: SwarmGraph, *, registry: object, compile_id: str, **kwargs: object
    ) -> None:
        del kwargs
        job: Job = registry.get(compile_id)  # type: ignore[attr-defined]
        job.mark_running()
        for index, name in enumerate(PHASE_NAMES, start=1):
            job.append_event(
                "phase",
                {
                    "name": name,
                    "index": index,
                    "total": len(PHASE_NAMES),
                    "status": PHASE_STATUS_STARTED,
                },
            )
            job.append_event("log", {"message": f"{name} running"})
            if name == PHASE_REVIEW:
                job.append_event(
                    "warning",
                    {"code": "w1", "message": "a warning", "nodeIds": []},
                )
            job.append_event(
                "phase",
                {
                    "name": name,
                    "index": index,
                    "total": len(PHASE_NAMES),
                    "status": PHASE_STATUS_SUCCEEDED,
                },
            )

        if outcome == PHASE_STATUS_SUCCEEDED:
            job.append_event("done", {"projectPath": "/tmp/example", "attempts": 1})
            registry.mark_succeeded(  # type: ignore[attr-defined]
                compile_id, {"projectPath": "/tmp/example", "attempts": 1}
            )
            return

        failure_message = "validate failed"
        job.append_event(
            "phase",
            {
                "name": PHASE_VALIDATE,
                "index": len(PHASE_NAMES),
                "total": len(PHASE_NAMES),
                "status": PHASE_STATUS_FAILED,
                "message": failure_message,
                "details": {},
            },
        )
        job.append_event(
            "error",
            {
                "code": ERROR_CODE_PHASE_FAILED,
                "message": failure_message,
                "exception": "PhaseFailureError",
            },
        )
        registry.mark_failed(compile_id, RuntimeError(failure_message))  # type: ignore[attr-defined]

    return scripted


def _terminal_events(frames: list[dict[str, object]]) -> list[dict[str, object]]:
    """Every frame that would end a compile's stream."""
    return [frame for frame in frames if frame["event"] in TERMINAL_EVENT_TYPES]


# ---------------------------------------------------------------------------
# POST /api/compile
# ---------------------------------------------------------------------------


def test_start_compile_returns_compile_id(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid saved graph yields a compile id, immediately."""
    _ = workspace
    monkeypatch.setenv("SWARM_FAKE_FILL", FAKE_FILL_ENABLED)
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())

        response = client.post("/api/compile", json={"graphId": graph_id})

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"compileId"}
        assert isinstance(body["compileId"], str)
        assert body["compileId"]


def test_start_compile_unknown_graph_is_404(workspace: Path) -> None:
    """An unknown graph id 404s and registers no job."""
    _ = workspace
    with _client() as client:
        response = client.post("/api/compile", json={"graphId": "no-such-graph"})

        assert response.status_code == 404
        assert "no-such-graph" in response.json()["detail"]


def test_start_compile_rejects_traversal_graph_id(workspace: Path) -> None:
    """A traversal-shaped id is refused the same way ``/api/graphs``
    refuses one -- never a 2xx, never a 500 -- and nothing escapes the
    workspace."""
    with _client() as client:
        response = client.post("/api/compile", json={"graphId": "../etc/passwd"})

        assert response.status_code in (404, 422)
        assert not (workspace / "graphs" / "etc").exists()


def test_second_concurrent_compile_of_same_graph_is_409(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first compile keeps the graph's only slot; the second 409s.

    The first compile is held open by a pipeline stand-in that never
    returns rather than by the real one, whose completion this test
    races against. Whether the background task has actually started its
    first step by the time the second request lands is irrelevant: the
    job is registered ``queued`` and still live either way, which is the
    condition the 409 enforces.
    """
    _ = workspace
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: _blocked_pipeline)
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())

        first = client.post("/api/compile", json={"graphId": graph_id})
        assert first.status_code == 200, first.text
        first_id = first.json()["compileId"]
        assert _live_snapshot(client, first_id)["status"] in ("queued", "running")

        second = client.post("/api/compile", json={"graphId": graph_id})

        assert second.status_code == 409
        assert "already has a live compile job" in second.json()["detail"]
        assert second.json()["detail"] != first_id

        # Clean teardown, and a check that the 409 did not disturb the
        # job that actually owns the slot.
        cancelled = client.delete(f"/api/compile/{first_id}")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"


def test_compile_of_a_graph_whose_compile_finished_is_allowed_again(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 409 is per *live* job: once a compile finishes, the graph can
    be compiled again."""
    _ = workspace
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: _scripted_pipeline(TEST_GRAPH))
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())

        first = client.post("/api/compile", json={"graphId": graph_id})
        with client.stream("GET", f"/api/compile/{first.json()['compileId']}/events") as stream:
            stream.read()
        first_snapshot = client.get(f"/api/compile/{first.json()['compileId']}").json()
        second = client.post("/api/compile", json={"graphId": graph_id})

        assert first.status_code == 200
        # The first job really is finished, not merely forgotten: the
        # second request succeeding must mean "the slot is free".
        assert first_snapshot["status"] == "succeeded"
        assert second.status_code == 200
        assert second.json()["compileId"] != first.json()["compileId"]


# ---------------------------------------------------------------------------
# GET /api/compile/{compile_id}/events  (SSE)
# ---------------------------------------------------------------------------


def test_events_streams_and_terminates_with_monotonic_frame_ids(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream carries every event, ends on the terminal one, and
    numbers its frames 1..n with no repeats."""
    _ = workspace
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: _scripted_pipeline(TEST_GRAPH))
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())
        compile_id = client.post("/api/compile", json={"graphId": graph_id}).json()["compileId"]

        with client.stream("GET", f"/api/compile/{compile_id}/events") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = response.read().decode()

        frames = _parse_frames(body)
        ids = _frame_ids(frames)

        # Terminated at (not merely before) the terminal event: the
        # response completed because the compile finished.
        assert _terminal_events(frames)[-1]["event"] == "done"
        assert frames[-1]["event"] == "done"
        assert ids == list(range(1, len(ids) + 1))
        assert len(ids) > len(PHASE_NAMES)

        # Each frame's SSE event name is the compile event type, and its
        # data carries the payload plus the frame's own id.
        for frame in frames:
            assert frame["event"] in {"phase", "log", "warning", "done", "error"}
            assert frame["data"]["eventId"] == int(str(frame["id"]))
            assert isinstance(frame["data"]["createdAt"], str)

        phase_names = [
            frame["data"]["name"] for frame in frames if frame["event"] == "phase"
        ]
        assert phase_names[0] == PHASE_REVIEW
        assert PHASE_BOUNDARY in phase_names
        assert PHASE_FILL in phase_names
        assert PHASE_SCAFFOLD in phase_names
        assert PHASE_VALIDATE in phase_names


def test_events_stream_ends_on_an_error_too(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed compile's stream terminates on its ``error`` event."""
    _ = workspace
    scripted = _scripted_pipeline(TEST_GRAPH, outcome=PHASE_STATUS_FAILED)
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: scripted)
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())
        compile_id = client.post("/api/compile", json={"graphId": graph_id}).json()["compileId"]

        with client.stream("GET", f"/api/compile/{compile_id}/events") as response:
            assert response.status_code == 200
            frames = _parse_frames(response.read().decode())

        assert frames[-1]["event"] == "error"
        assert frames[-1]["data"]["code"] == ERROR_CODE_PHASE_FAILED


def test_events_unknown_compile_id_is_404(workspace: Path) -> None:
    """An unknown job id 404s before any stream is opened."""
    _ = workspace
    with _client() as client:
        response = client.get("/api/compile/never-existed/events")

        assert response.status_code == 404
        assert "never-existed" in response.json()["detail"]


def test_events_non_integer_last_event_id_is_400(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed cursor is reported, not silently treated as absent."""
    _ = workspace
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: _quit_pipeline)
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())
        compile_id = client.post("/api/compile", json={"graphId": graph_id}).json()["compileId"]

        response = client.get(
            f"/api/compile/{compile_id}/events", headers={"Last-Event-ID": "not-a-number"}
        )

        assert response.status_code == 400
        assert "not an integer" in response.json()["detail"]


def test_events_last_event_id_replays_strictly_after_without_gaps(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconnecting with a cursor replays exactly what that client
    missed: no duplicate of an already-seen frame, no missing middle."""
    _ = workspace
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: _scripted_pipeline(TEST_GRAPH))
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())
        compile_id = client.post("/api/compile", json={"graphId": graph_id}).json()["compileId"]

        with client.stream("GET", f"/api/compile/{compile_id}/events") as response:
            all_ids = _frame_ids(_parse_frames(response.read().decode()))

        cursor = all_ids[3]
        with client.stream(
            "GET",
            f"/api/compile/{compile_id}/events",
            headers={"Last-Event-ID": str(cursor)},
        ) as response:
            assert response.status_code == 200
            resumed_ids = _frame_ids(_parse_frames(response.read().decode()))

        assert resumed_ids == [i for i in all_ids if i > cursor]
        assert cursor not in resumed_ids

        # An up-to-date cursor replays nothing at all (the job is over,
        # so there is no live event left to wait for either).
        with client.stream(
            "GET",
            f"/api/compile/{compile_id}/events",
            headers={"Last-Event-ID": str(all_ids[-1])},
        ) as response:
            assert _parse_frames(response.read().decode()) == []


def test_events_last_event_id_off_the_ring_buffer_is_400_with_snapshot_hint(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An evicted cursor gets a real 400 telling the client to refetch the
    snapshot, never a partial replay that looks complete.

    The gap is produced by making ``Job.events_after`` report one -- the
    genuine trigger is a 2000-event ring buffer, which would make this
    test cost more than the behavior it verifies.
    """
    _ = workspace
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: _scripted_pipeline(TEST_GRAPH))

    def evicted(self: Job, last_event_id: int | None) -> list[object]:
        raise EventLogGapError(self.compile_id, last_event_id or 0, 500)

    monkeypatch.setattr(Job, "events_after", evicted)
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())
        compile_id = client.post("/api/compile", json={"graphId": graph_id}).json()["compileId"]

        response = client.get(
            f"/api/compile/{compile_id}/events", headers={"Last-Event-ID": "1"}
        )

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["gap"] is True
        assert detail["requestedLastEventId"] == 1
        assert detail["oldestRetainedEventId"] == 500
        assert detail["snapshotUrl"] == f"/api/compile/{compile_id}"
        assert "snapshot" in detail["message"]


def test_events_gap_after_frames_is_reported_in_band_and_closes(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the cursor falls off the ring buffer *after* frames have gone
    out, the status is already committed to 200, so the gap is reported
    in band and the stream closes. Closing is what makes the client
    reconnect, and its next attempt -- same cursor -- is refused up front
    with the 400 the other test covers.

    The replay generator is stubbed here rather than driven from a real
    job: a job that finishes before the client connects replays in one
    shot (``Job.events_after`` returns a list, so there is no gap *during*
    the replay), and one that is still running races this test's own
    request. The stub therefore isolates the branch under test: a frame
    out, then a gap.
    """
    _ = workspace
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: _scripted_pipeline(TEST_GRAPH))

    def one_frame_then_gap(job: Job, last_event_id: int | None, compile_id: str):
        """A replay that sends one frame and then reports an eviction."""
        del compile_id
        yield compile_routes._to_sse_frame(job.append_event("log", {"message": "before the gap"}))
        raise EventLogGapError(job.compile_id, last_event_id or 0, 4)

    monkeypatch.setattr(compile_routes, "_replayed_frames", one_frame_then_gap)
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())
        compile_id = client.post("/api/compile", json={"graphId": graph_id}).json()["compileId"]

        with client.stream("GET", f"/api/compile/{compile_id}/events") as response:
            assert response.status_code == 200
            body = response.read().decode()

        comments = _comments(body)
        assert len(comments) == 1, body
        assert comments[0].startswith("event-log-gap ")
        assert json.loads(comments[0].split(" ", 1)[1])["gap"] is True

        # The one frame that went out before the gap is intact (its id is
        # whatever the job assigned it), and the response ended on the gap
        # rather than on a terminal event.
        frames = _parse_frames(body)
        assert _frame_ids(frames) == [frames[0]["data"]["eventId"]]
        assert frames_never_reached_done(body)


def frames_never_reached_done(body: str) -> bool:
    """Whether the stream body contains no ``done`` frame."""
    return all(frame["event"] != "done" for frame in _parse_frames(body))


# ---------------------------------------------------------------------------
# GET /api/compile/{compile_id}  (status snapshot)
# ---------------------------------------------------------------------------


def test_status_snapshot_shape_for_a_finished_job(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot a fresh tab reads is camelCase and complete."""
    _ = workspace
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: _scripted_pipeline(TEST_GRAPH))
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())
        compile_id = client.post("/api/compile", json={"graphId": graph_id}).json()["compileId"]
        with client.stream("GET", f"/api/compile/{compile_id}/events") as stream:
            stream.read()

        response = client.get(f"/api/compile/{compile_id}")

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "compileId",
            "graphId",
            "status",
            "createdAt",
            "startedAt",
            "finishedAt",
            "latestEventId",
            "result",
            "error",
        }
        assert body["compileId"] == compile_id
        assert body["graphId"] == graph_id
        assert body["status"] == "succeeded"
        assert body["error"] is None
        assert body["result"]["projectPath"] == "/tmp/example"
        assert isinstance(body["latestEventId"], int)
        assert body["startedAt"] is not None
        assert body["finishedAt"] is not None


def test_status_unknown_compile_id_is_404(workspace: Path) -> None:
    """An unknown (or evicted) id 404s."""
    _ = workspace
    with _client() as client:
        response = client.get("/api/compile/never-existed")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/compile/{compile_id}  (cancel)
# ---------------------------------------------------------------------------


def test_delete_cancels_a_running_compile(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a live job ends it ``cancelled`` and returns that
    snapshot, all in one response."""
    _ = workspace
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: _blocked_pipeline)
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())
        compile_id = client.post("/api/compile", json={"graphId": graph_id}).json()["compileId"]
        assert _live_snapshot(client, compile_id)["status"] in ("queued", "running")

        response = client.delete(f"/api/compile/{compile_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "cancelled"
        assert body["finishedAt"] is not None
        # The cancel really interrupted the task rather than merely
        # relabelling the job.
        assert client.get(f"/api/compile/{compile_id}").json()["status"] == "cancelled"


def test_delete_unknown_compile_id_is_404(workspace: Path) -> None:
    """Cancelling an unknown job 404s."""
    _ = workspace
    with _client() as client:
        response = client.delete("/api/compile/never-existed")

        assert response.status_code == 404
        assert "never-existed" in response.json()["detail"]


def test_delete_on_a_finished_compile_is_409_with_the_snapshot(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling is a state transition, so a job that already finished
    is a conflict -- and the 409 body still reports the outcome."""
    _ = workspace
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: _scripted_pipeline(TEST_GRAPH))
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())
        compile_id = client.post("/api/compile", json={"graphId": graph_id}).json()["compileId"]
        with client.stream("GET", f"/api/compile/{compile_id}/events") as stream:
            stream.read()
        assert client.get(f"/api/compile/{compile_id}").json()["status"] == "succeeded"

        response = client.delete(f"/api/compile/{compile_id}")

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["status"] == "succeeded"
        assert detail["compileId"] == compile_id
        assert detail["snapshot"]["result"]["projectPath"] == "/tmp/example"
        assert "already" in detail["message"]


def test_delete_is_idempotent_only_while_the_job_still_runs(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first cancel wins; the second sees a finished job (a 409),
    and no third state is invented."""
    _ = workspace
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: _blocked_pipeline)
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())
        compile_id = client.post("/api/compile", json={"graphId": graph_id}).json()["compileId"]

        assert client.delete(f"/api/compile/{compile_id}").status_code == 200
        second = client.delete(f"/api/compile/{compile_id}")

        assert second.status_code == 409
        assert second.json()["detail"]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Shutdown hook
# ---------------------------------------------------------------------------


def test_app_shutdown_cancels_a_live_compile(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exiting the app's lifespan cancels live jobs and awaits them.

    Without this, the server could exit with a pipeline coroutine
    mid-step against a half-written project directory.
    """
    _ = workspace
    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: _blocked_pipeline)
    with _client() as client:
        graph_id = _save_graph(client, mixed_programmatic_graph())
        compile_id = client.post("/api/compile", json={"graphId": graph_id}).json()["compileId"]
        assert _live_snapshot(client, compile_id)["status"] in ("queued", "running")

        registry = compile_routes._require_registry()

    # The ``with`` block just ran the lifespan shutdown hook.
    assert registry.get(compile_id).status == "cancelled"


# ---------------------------------------------------------------------------
# End to end, over HTTP, with the deterministic stub fill
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_fake_fill_compile_end_to_end_over_http_reaches_succeeded(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SWARM_FAKE_FILL=1``, a real scaffold, a real ``uv sync`` gate,
    and a real project on disk -- every step driven through HTTP.

    The status snapshot is polled rather than the SSE stream read
    directly: the TestClient drives the app's own event loop for the
    duration of each request, so successive polling requests are what let
    the background compile task advance.
    """
    _ = workspace
    monkeypatch.setenv("SWARM_FAKE_FILL", FAKE_FILL_ENABLED)
    graph = mixed_programmatic_graph()
    client = _client()

    graph_id = _save_graph(client, graph)
    start = client.post("/api/compile", json={"graphId": graph_id})
    assert start.status_code == 200, start.text
    compile_id = start.json()["compileId"]

    status = client.get(f"/api/compile/{compile_id}").json()
    polls = 0
    while status["status"] not in _TERMINAL_STATUSES and polls < _MAX_POLLS:
        status = client.get(f"/api/compile/{compile_id}").json()
        polls += 1

    assert status["status"] == "succeeded", f"{status} (after {polls} polls)"

    # The replay is the whole retained log and ends on exactly one
    # terminal event, whose payload carries the same result the snapshot
    # recorded (plus the validation step summary, which is `done`-only).
    with client.stream("GET", f"/api/compile/{compile_id}/events") as response:
        frames = _parse_frames(response.read().decode())
    done_frames = [frame for frame in frames if frame["event"] == "done"]
    assert len(done_frames) == 1
    assert frames[-1]["event"] == "done"
    assert _frame_ids(frames) == list(range(1, len(frames) + 1))

    # Every phase ran and succeeded, in order.
    phase_frames = [
        (frame["data"]["name"], frame["data"]["status"])
        for frame in frames
        if frame["event"] == "phase"
    ]
    assert phase_frames == [
        (name, status_value)
        for name in PHASE_NAMES
        for status_value in (PHASE_STATUS_STARTED, PHASE_STATUS_SUCCEEDED)
    ]

    done_data = done_frames[0]["data"]
    assert done_data["validationSteps"] == ["uv_sync", "keyless_import", "dry_run"]
    assert done_data["attempts"] == 1

    result = status["result"]
    # The snapshot's recorded value is the same payload the `done` event
    # carried, minus the stream-only fields (`eventId`/`createdAt`) and
    # minus `validationSteps`, which is `done`-event-only.
    assert set(result) == set(done_data) - {"eventId", "createdAt", "validationSteps"}
    for key in result:
        assert result[key] == done_data[key]

    project_path = Path(str(done_data["projectPath"]))
    assert project_path.is_dir()
    assert result["projectPath"] == str(project_path)
    assert result["runCommand"] == done_data["runCommand"]
    assert result["runCommand"].startswith(f"cd {project_path}")
    assert result["attempts"] == 1
    assert result["diagram"].strip().startswith("stateDiagram-v2")
    assert (project_path / "validate" / "dry_run.py").is_file()
    # Proof the keyless gate really ran rather than being reported.
    assert (project_path / ".venv").is_dir()

    # The stub fill filled the programmatic node's body, so the scaffold's
    # sentinel is gone.
    step_text = (project_path / "src" / "swarm_workflow" / "steps" / "fetch.py").read_text()
    assert "unfilled step body" not in step_text


def test_run_job_injects_the_real_fill_agent() -> None:
    """The route must hand ``run_compile`` the real Phase-3 filler.

    Regression guard for a wiring bug that made the product's central
    feature unreachable: ``_run_job`` called ``run_compile`` without a
    ``filler=``, so every compile that was *not* running under
    ``SWARM_FAKE_FILL=1`` failed Phase 3 with ``FillUnavailableError``
    even though ``compile/agent.py`` was complete and tested. The offline
    path kept passing, so no existing test noticed.
    """
    from swarm_builder.compile.agent import fill
    from swarm_builder.routes.compile import _load_filler

    assert _load_filler() is fill


def test_run_job_passes_the_filler_through_to_run_compile(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_run_job`` forwards the injected filler to the pipeline.

    Asserting on ``_load_filler`` alone would not catch a ``_run_job``
    that resolved the filler and then forgot to pass it on, so capture
    the actual keyword arguments the pipeline is invoked with.
    """
    from swarm_builder.compile.agent import fill
    from swarm_builder.routes import compile as compile_routes

    captured: dict[str, object] = {}

    async def fake_run_compile(graph: SwarmGraph, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(compile_routes, "_load_run_compile", lambda: fake_run_compile)

    graph = mixed_programmatic_graph()
    registry = compile_routes._require_registry()
    registry.register("cid-filler", graph.id)

    asyncio.run(
        compile_routes._run_job(
            graph,
            registry=registry,
            compile_id="cid-filler",
            workspace_dir=workspace,
        )
    )

    assert captured["filler"] is fill
