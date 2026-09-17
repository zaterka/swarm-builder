"""Tests for the compile-job registry (``compile/jobs.py``).

Covers: monotonic event ids, ``Last-Event-ID`` replay semantics
(strictly-after, no duplicates/gaps), ring-buffer overflow being
detectable rather than silently partial, the one-concurrent-compile-
per-graph invariant and its dedicated exception, eviction of finished
jobs (never a running one), cancellation actually cancelling the
task, multiple live subscribers each seeing every event, a vanished
subscriber not wedging the job, and registry-wide shutdown.
"""

from __future__ import annotations

import asyncio

import pytest

from swarm_builder.compile.jobs import (
    RING_BUFFER_LINES,
    CompileEvent,
    EventLogGapError,
    GraphCompileInProgressError,
    Job,
    JobNotFoundError,
    JobRegistry,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Pin the anyio backend to asyncio -- trio is not installed."""
    return "asyncio"


async def _never_finishes() -> None:
    """A coroutine that just waits forever, for cancellation tests."""
    await asyncio.Event().wait()


def _register_running(registry: JobRegistry, compile_id: str, graph_id: str) -> Job:
    """Register a job and drive it straight to ``running``."""
    job = registry.register(compile_id, graph_id)
    job.mark_running()
    return job


# ---------------------------------------------------------------------------
# Event log: monotonic ids, replay, ring-buffer overflow
# ---------------------------------------------------------------------------


async def test_event_ids_are_monotonic_starting_at_one() -> None:
    job = Job("c1", "g1")
    e1 = job.append_event("phase", {"name": "review"})
    e2 = job.append_event("log", {"message": "hi"})
    e3 = job.append_event("done", {})

    assert (e1.event_id, e2.event_id, e3.event_id) == (1, 2, 3)


async def test_events_after_none_returns_full_retained_log() -> None:
    job = Job("c1", "g1")
    job.append_event("phase", {"name": "review"})
    job.append_event("log", {"message": "hi"})

    events = job.events_after(None)

    assert [e.event_id for e in events] == [1, 2]


async def test_events_after_returns_strictly_after_with_no_duplicates_or_gaps() -> None:
    job = Job("c1", "g1")
    for i in range(5):
        job.append_event("log", {"i": i})

    events = job.events_after(2)

    assert [e.event_id for e in events] == [3, 4, 5]


async def test_events_after_latest_id_returns_empty() -> None:
    job = Job("c1", "g1")
    job.append_event("log", {"i": 0})
    last = job.append_event("log", {"i": 1})

    assert job.events_after(last.event_id) == []


async def test_ring_buffer_overflow_raises_gap_error_instead_of_partial_replay() -> None:
    job = Job("c1", "g1")
    # Push two more events than the ring buffer retains, so events 1
    # AND 2 fall off the back -- a client that last saw event 1 is
    # missing event 2, a genuine gap.
    for i in range(RING_BUFFER_LINES + 2):
        job.append_event("log", {"i": i})

    with pytest.raises(EventLogGapError) as exc_info:
        job.events_after(1)

    err = exc_info.value
    assert err.compile_id == "c1"
    assert err.last_event_id == 1
    assert err.oldest_retained_id == 3


async def test_ring_buffer_still_replays_correctly_when_id_is_within_retained_range() -> None:
    job = Job("c1", "g1")
    for i in range(RING_BUFFER_LINES + 10):
        job.append_event("log", {"i": i})

    # Oldest retained id is 11 (ids 1-10 evicted). Requesting after 11
    # is a normal, non-gappy replay.
    events = job.events_after(11)

    assert events[0].event_id == 12
    assert events[-1].event_id == RING_BUFFER_LINES + 10
    assert len(events) == RING_BUFFER_LINES + 10 - 11


async def test_events_after_oldest_retained_id_minus_one_is_not_a_gap() -> None:
    """Requesting exactly one before the oldest retained id is the
    boundary case of "no history lost" and must not raise."""
    job = Job("c1", "g1")
    for i in range(RING_BUFFER_LINES + 1):
        job.append_event("log", {"i": i})

    oldest_id = job.events_after(None)[0].event_id
    events = job.events_after(oldest_id - 1)

    assert events[0].event_id == oldest_id


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------


async def test_snapshot_reports_status_and_latest_event_id() -> None:
    job = Job("c1", "g1")
    job.mark_running()
    job.append_event("phase", {"name": "review"})
    event = job.append_event("phase", {"name": "scaffold"})

    snapshot = job.snapshot()

    assert snapshot["compileId"] == "c1"
    assert snapshot["graphId"] == "g1"
    assert snapshot["status"] == "running"
    assert snapshot["latestEventId"] == event.event_id
    assert snapshot["startedAt"] is not None
    assert snapshot["finishedAt"] is None


async def test_snapshot_on_empty_log_has_no_latest_event_id() -> None:
    job = Job("c1", "g1")

    assert job.snapshot()["latestEventId"] is None


async def test_snapshot_reports_error_message_for_failed_job() -> None:
    job = Job("c1", "g1")
    job.mark_running()
    job.mark_failed(ValueError("boom"))

    snapshot = job.snapshot()

    assert snapshot["status"] == "failed"
    assert snapshot["error"] == "boom"
    assert snapshot["result"] is None


# ---------------------------------------------------------------------------
# One concurrent compile per graph_id
# ---------------------------------------------------------------------------


async def test_second_concurrent_compile_for_same_graph_raises() -> None:
    registry = JobRegistry()
    registry.register("c1", "g1")

    with pytest.raises(GraphCompileInProgressError) as exc_info:
        registry.register("c2", "g1")

    assert exc_info.value.graph_id == "g1"
    assert exc_info.value.compile_id == "c1"


async def test_concurrent_compile_allowed_for_a_different_graph() -> None:
    registry = JobRegistry()
    registry.register("c1", "g1")

    job2 = registry.register("c2", "g2")

    assert job2.graph_id == "g2"


async def test_new_compile_allowed_once_prior_job_for_graph_finishes() -> None:
    registry = JobRegistry()
    job1 = registry.register("c1", "g1")
    job1.mark_running()
    registry.mark_succeeded("c1", {"ok": True})

    job2 = registry.register("c2", "g1")

    assert job2.graph_id == "g1"


async def test_new_compile_allowed_after_cancellation() -> None:
    registry = JobRegistry()
    _register_running(registry, "c1", "g1")
    registry.cancel("c1")

    job2 = registry.register("c2", "g1")

    assert job2.graph_id == "g1"


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


async def test_eviction_drops_oldest_finished_jobs_past_the_cap() -> None:
    registry = JobRegistry()
    for i in range(25):
        job = registry.register(f"c{i}", f"g{i}")
        job.mark_running()
        registry.mark_succeeded(f"c{i}", {"i": i})

    with pytest.raises(JobNotFoundError):
        registry.get("c0")
    with pytest.raises(JobNotFoundError):
        registry.get("c4")
    # The 20 most recent finished jobs (c5..c24) survive.
    assert registry.get("c5").compile_id == "c5"
    assert registry.get("c24").compile_id == "c24"


async def test_eviction_never_drops_a_running_job() -> None:
    registry = JobRegistry()
    running = _register_running(registry, "c-running", "g-running")

    for i in range(25):
        job = registry.register(f"c{i}", f"g{i}")
        job.mark_running()
        registry.mark_succeeded(f"c{i}", {"i": i})

    # Still present despite 25 finished jobs having been registered
    # after it -- eviction only ever considers finished jobs.
    assert registry.get("c-running") is running
    running.task = asyncio.get_event_loop().create_task(_never_finishes())
    running.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running.task


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_cancel_transitions_to_cancelled_and_cancels_the_task() -> None:
    registry = JobRegistry()
    job = _register_running(registry, "c1", "g1")
    task = asyncio.ensure_future(_never_finishes())
    job.task = task

    registry.cancel("c1")
    # Let the cancellation actually propagate into the task.
    with pytest.raises(asyncio.CancelledError):
        await task

    assert job.status == "cancelled"
    assert task.cancelled()


async def test_cancel_unknown_job_raises_job_not_found() -> None:
    registry = JobRegistry()

    with pytest.raises(JobNotFoundError):
        registry.cancel("nope")


async def test_cancel_already_finished_job_raises_value_error() -> None:
    registry = JobRegistry()
    job = registry.register("c1", "g1")
    job.mark_running()
    job.mark_succeeded({"ok": True})

    with pytest.raises(ValueError):
        registry.cancel("c1")


# ---------------------------------------------------------------------------
# Live subscription: multiple subscribers, vanished subscriber
# ---------------------------------------------------------------------------


async def test_two_concurrent_subscribers_each_receive_every_event() -> None:
    job = Job("c1", "g1")
    queue_a = job.subscribe()
    queue_b = job.subscribe()

    job.append_event("phase", {"name": "review"})
    job.append_event("log", {"message": "hi"})

    events_a = [queue_a.get_nowait(), queue_a.get_nowait()]
    events_b = [queue_b.get_nowait(), queue_b.get_nowait()]

    assert [e.event_type for e in events_a] == ["phase", "log"]
    assert [e.event_type for e in events_b] == ["phase", "log"]


async def test_vanished_subscriber_does_not_block_further_events() -> None:
    job = Job("c1", "g1")
    vanished_queue = job.subscribe()

    # Simulate a client that disconnected without draining its queue.
    job.unsubscribe(vanished_queue)

    # Must return promptly (no blocking on the unbounded queue put)
    # and must not raise even though nothing is draining vanished_queue.
    event = job.append_event("log", {"message": "still alive"})

    assert event.event_id == 1
    assert vanished_queue.empty()


async def test_unsubscribe_is_idempotent() -> None:
    job = Job("c1", "g1")
    queue = job.subscribe()

    job.unsubscribe(queue)
    job.unsubscribe(queue)  # must not raise


async def test_subscriber_queue_receives_events_appended_after_subscribing_only() -> None:
    job = Job("c1", "g1")
    job.append_event("log", {"message": "before"})
    queue = job.subscribe()
    job.append_event("log", {"message": "after"})

    event: CompileEvent = queue.get_nowait()

    assert event.payload == {"message": "after"}
    assert queue.empty()


# ---------------------------------------------------------------------------
# Registry-wide shutdown
# ---------------------------------------------------------------------------


async def test_shutdown_cancels_every_live_job() -> None:
    registry = JobRegistry()
    job1 = _register_running(registry, "c1", "g1")
    job2 = _register_running(registry, "c2", "g2")
    task1 = asyncio.ensure_future(_never_finishes())
    task2 = asyncio.ensure_future(_never_finishes())
    job1.task = task1
    job2.task = task2

    await registry.shutdown()

    assert job1.status == "cancelled"
    assert job2.status == "cancelled"
    assert task1.cancelled()
    assert task2.cancelled()


async def test_shutdown_leaves_finished_jobs_untouched() -> None:
    registry = JobRegistry()
    job = registry.register("c1", "g1")
    job.mark_running()
    job.mark_succeeded({"ok": True})

    await registry.shutdown()

    assert job.status == "succeeded"
