"""In-memory compile-job registry (PLAN.md "Compile job lifecycle (I4)").

This module owns job bookkeeping and the per-job event log ONLY. It
knows nothing about the five compile phases and never imports
``scaffold``/``review``/``validate``/``boundary`` -- it does not run a
compile. ``pipeline.py`` (a later, separate module) drives the five
phases and calls into :class:`JobRegistry` to create a job, append
events as phases progress, and record the terminal result. The route
layer (``routes/compile.py``) calls into this module to serve the SSE
stream, the status-snapshot endpoint, and the cancel endpoint -- it
never reaches into a job's internals directly.

**Framework-agnostic on purpose.** No FastAPI import, no
``HTTPException``. :class:`GraphCompileInProgressError` is a plain
exception the route layer catches and turns into a 409; this module
has no notion of HTTP status codes.

**Event log and replay.** Every event appended to a job gets a
strictly increasing integer id (monotonic *within that job*, starting
at 1) and is retained in a ring buffer of the last
:data:`RING_BUFFER_LINES` events. :meth:`Job.events_after` serves the
``Last-Event-ID`` reconnect design: it returns events strictly after a
given id, and raises :class:`EventLogGapError` when the requested id
has already fallen off the back of the ring buffer, rather than
silently returning a partial history that looks complete. A client
with no ``Last-Event-ID`` at all (a fresh tab) instead wants
:meth:`Job.snapshot`, a point-in-time status summary.

**Live subscription.** :meth:`Job.subscribe` hands back a fresh
``asyncio.Queue`` fed by :meth:`Job.append_event`, for
``routes/compile.py`` to drain into an SSE stream. Multiple concurrent
subscribers each get every event, because each has its own queue.
Per PLAN.md's documented failure mode ("SSE client vanishes -> job
continues; log retained for Last-Event-ID reconnect"), a subscriber
that stops draining its queue never blocks the job: queues are
unbounded, so :meth:`Job.append_event` never awaits a slow or dead
consumer, and :meth:`Job.unsubscribe` simply drops the reference when
the route handler's stream ends (including on disconnect).

**Cancellation.** There is no subprocess to kill (fact 26) -- a
compile is one ``asyncio.Task`` running the fill agent in-process, so
:meth:`JobRegistry.cancel` cancels that task directly and the job
transitions to ``"cancelled"``.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

#: SSE event type discriminator (PLAN.md "The compile pipeline" intro:
#: "Five phases, each emitting SSE progress" and the endpoint table's
#: ``GET /api/compile/:compileId/events`` row lists exactly these five
#: frame types).
EventType = Literal["phase", "log", "warning", "done", "error"]

#: Job lifecycle status. A job is "live" (see :meth:`Job.is_live`) in
#: the ``queued``/``running`` states and "finished" in the rest.
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

#: Maximum number of *finished* jobs retained in the registry, dropped
#: oldest-first once exceeded. A running job is never evicted (PLAN.md
#: "Compile job lifecycle (I4)": "The map holds at most 20 finished
#: jobs, dropped oldest-first; a running job is never evicted").
MAX_RETAINED_JOBS = 20

#: Wall-clock bound the fill step (Phase 3, the only phase that calls
#: the model) must apply to its model request. Lives here -- not in
#: ``pipeline.py`` -- so the documented 10-minute bound (PLAN.md: "the
#: fill run sets it explicitly (10 minutes) -- otherwise a wedged model
#: hangs the compile permanently") has exactly one home instead of a
#: literal duplicated across the module that defines it and the module
#: that enforces it.
FILL_TIMEOUT_SECONDS = 600

#: Ring-buffer depth for each job's retained event log (PLAN.md:
#: "replays strictly *after* its last seen id from the retained ring
#: buffer (last 2000 lines)").
RING_BUFFER_LINES = 2000


class GraphCompileInProgressError(Exception):
    """Raised by :meth:`JobRegistry.register` when a compile is already
    live for the given ``graph_id``.

    Distinct and catchable so the route layer can turn it into a 409
    (PLAN.md: "One concurrent compile per graphId; a second request
    gets 409") without this module knowing what a 409 is.
    """

    def __init__(self, graph_id: str, compile_id: str) -> None:
        self.graph_id = graph_id
        self.compile_id = compile_id
        super().__init__(
            f"graph {graph_id!r} already has a live compile job ({compile_id!r})"
        )


class JobNotFoundError(LookupError):
    """Raised when a ``compile_id`` has no registered job.

    Covers both "never existed" and "existed but was evicted" -- the
    route layer reports both as a 404, so this module does not
    distinguish them either.
    """

    def __init__(self, compile_id: str) -> None:
        self.compile_id = compile_id
        super().__init__(f"no compile job {compile_id!r}")


class EventLogGapError(LookupError):
    """Raised by :meth:`Job.events_after` when ``last_event_id`` has
    already fallen off the back of the ring buffer.

    A reconnecting client whose ``Last-Event-ID`` predates the oldest
    retained event cannot be served a correct replay: returning
    whatever remains would silently look like a complete history while
    actually skipping a gap in the middle. Raising lets the route layer
    decide how to recover (typically: fall back to
    :meth:`Job.snapshot` and tell the client it missed history).
    """

    def __init__(self, compile_id: str, last_event_id: int, oldest_retained_id: int) -> None:
        self.compile_id = compile_id
        self.last_event_id = last_event_id
        self.oldest_retained_id = oldest_retained_id
        super().__init__(
            f"compile job {compile_id!r}: requested events after id "
            f"{last_event_id}, but the oldest retained event is "
            f"{oldest_retained_id} -- the gap in between was evicted "
            "from the ring buffer"
        )


#: Statuses a job may still be transitioned out of. Every other status
#: is terminal.
_LIVE_STATUSES: frozenset[JobStatus] = frozenset({"queued", "running"})


@dataclass(frozen=True)
class CompileEvent:
    """One SSE frame in a job's event log.

    Args:
        event_id: Strictly increasing integer id, unique and monotonic
            within one job (starts at 1). This is the SSE frame's
            ``id`` field and the value a client echoes back as
            ``Last-Event-ID`` on reconnect.
        event_type: One of ``phase``/``log``/``warning``/``done``/
            ``error`` (see :data:`EventType`).
        payload: The event body. Shape is per-``event_type`` and owned
            by ``pipeline.py``/``routes/compile.py`` -- this module
            only stores and replays it.
        created_at: UTC timestamp the event was appended.
    """

    event_id: int
    event_type: EventType
    payload: dict[str, object]
    created_at: datetime


@dataclass
class JobResult:
    """Terminal outcome of a finished compile job.

    Exactly one of ``error`` or a non-``None`` ``value`` is meaningful
    for a given :attr:`Job.status`: ``succeeded`` jobs set ``value``,
    ``failed`` jobs set ``error``, ``cancelled`` jobs set neither.

    Args:
        value: The pipeline's success payload (owned by
            ``pipeline.py``; opaque here), or ``None`` if the job did
            not succeed.
        error: The exception the pipeline raised, or ``None`` if the
            job did not fail.
    """

    value: dict[str, object] | None = None
    error: BaseException | None = None


class Job:
    """One compile job's bookkeeping, event log, and live subscribers.

    Constructed only by :meth:`JobRegistry.register` -- callers never
    instantiate this directly, so the registry can enforce the
    one-concurrent-compile-per-graph invariant at creation time.
    """

    def __init__(self, compile_id: str, graph_id: str) -> None:
        self.compile_id = compile_id
        self.graph_id = graph_id
        self.status: JobStatus = "queued"
        self.created_at: datetime = datetime.now(UTC)
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.result: JobResult = JobResult()
        #: Set by the caller once the pipeline coroutine is scheduled;
        #: absent (``None``) only in the brief window between
        #: registration and scheduling.
        self.task: asyncio.Task[None] | None = None
        self._events: deque[CompileEvent] = deque(maxlen=RING_BUFFER_LINES)
        self._next_event_id = itertools.count(start=1)
        self._subscribers: list[asyncio.Queue[CompileEvent]] = []

    def is_live(self) -> bool:
        """Return whether this job is still queued or running."""
        return self.status in _LIVE_STATUSES

    def mark_running(self) -> None:
        """Transition ``queued`` -> ``running`` and stamp ``started_at``.

        Raises:
            ValueError: If the job is not currently ``queued``.
        """
        if self.status != "queued":
            raise ValueError(
                f"cannot mark job {self.compile_id!r} running from status {self.status!r}"
            )
        self.status = "running"
        self.started_at = datetime.now(UTC)

    def mark_succeeded(self, value: dict[str, object]) -> None:
        """Transition to ``succeeded`` and record the pipeline's result.

        Raises:
            ValueError: If the job is not currently live.
        """
        self._mark_finished("succeeded")
        self.result = JobResult(value=value)

    def mark_failed(self, error: BaseException) -> None:
        """Transition to ``failed`` and record the raised exception.

        Raises:
            ValueError: If the job is not currently live.
        """
        self._mark_finished("failed")
        self.result = JobResult(error=error)

    def mark_cancelled(self) -> None:
        """Transition to ``cancelled``.

        Raises:
            ValueError: If the job is not currently live.
        """
        self._mark_finished("cancelled")

    def _mark_finished(self, status: JobStatus) -> None:
        if not self.is_live():
            raise ValueError(
                f"cannot mark job {self.compile_id!r} {status!r} from terminal status "
                f"{self.status!r}"
            )
        self.status = status
        self.finished_at = datetime.now(UTC)

    def append_event(self, event_type: EventType, payload: dict[str, object]) -> CompileEvent:
        """Append one event to the log and fan it out to live subscribers.

        Never awaits a subscriber: each subscriber queue is unbounded,
        so a subscriber that stopped draining (a vanished SSE client)
        cannot block this call or the pipeline task calling it.

        Args:
            event_type: One of ``phase``/``log``/``warning``/``done``/
                ``error``.
            payload: The event body, opaque to this module.

        Returns:
            The appended :class:`CompileEvent`, including its assigned
            monotonic id.
        """
        event = CompileEvent(
            event_id=next(self._next_event_id),
            event_type=event_type,
            payload=payload,
            created_at=datetime.now(UTC),
        )
        self._events.append(event)
        for queue in self._subscribers:
            queue.put_nowait(event)
        return event

    def events_after(self, last_event_id: int | None) -> list[CompileEvent]:
        """Return retained events with id strictly greater than ``last_event_id``.

        Args:
            last_event_id: The client's last-seen event id (typically
                parsed from the ``Last-Event-ID`` header), or ``None``
                to request the entire retained log from the start.

        Returns:
            Events in ascending id order, with no duplicates and no
            gaps relative to the retained log.

        Raises:
            EventLogGapError: If ``last_event_id`` is not ``None`` and
                is older than the oldest retained event -- the events
                between them were already evicted from the ring
                buffer, so a partial replay would silently look
                complete while actually skipping history.
        """
        if last_event_id is not None and self._events:
            oldest_id = self._events[0].event_id
            if last_event_id < oldest_id - 1:
                raise EventLogGapError(self.compile_id, last_event_id, oldest_id)
        if last_event_id is None:
            return list(self._events)
        return [event for event in self._events if event.event_id > last_event_id]

    def snapshot(self) -> dict[str, object]:
        """Return a point-in-time status summary for a client with no
        ``Last-Event-ID`` (PLAN.md: "for a client that has no
        Last-Event-ID at all, e.g. a fresh tab").

        Returns:
            A dict with ``compileId``, ``graphId``, ``status``,
            timestamps, the latest retained event id (``None`` if the
            log is empty), and, for a finished job, the terminal
            result or error message.
        """
        latest_event_id = self._events[-1].event_id if self._events else None
        error_message = str(self.result.error) if self.result.error is not None else None
        return {
            "compileId": self.compile_id,
            "graphId": self.graph_id,
            "status": self.status,
            "createdAt": self.created_at.isoformat(),
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "finishedAt": self.finished_at.isoformat() if self.finished_at else None,
            "latestEventId": latest_event_id,
            "result": self.result.value,
            "error": error_message,
        }

    def subscribe(self) -> asyncio.Queue[CompileEvent]:
        """Register a new live subscriber and return its event queue.

        The queue is unbounded and starts empty -- a subscriber that
        also wants prior history should first call :meth:`events_after`
        (or :meth:`snapshot`) and then :meth:`subscribe`, accepting the
        small window in between as an at-most-one-duplicate-event risk
        the caller can dedupe on event id if it matters.

        Returns:
            The queue :meth:`append_event` will feed. Pass it back to
            :meth:`unsubscribe` when the consumer stops draining it.
        """
        queue: asyncio.Queue[CompileEvent] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[CompileEvent]) -> None:
        """Remove a previously registered subscriber queue.

        Safe to call even if ``queue`` is already absent (e.g. called
        twice from overlapping ``finally`` blocks), so a route handler
        can call it unconditionally on stream teardown.

        Args:
            queue: The queue returned by :meth:`subscribe`.
        """
        try:
            self._subscribers.remove(queue)
        except ValueError:
            # Already removed -- unsubscribe is idempotent by design so
            # a route handler's cleanup path never has to track whether
            # it already ran.
            pass


class JobRegistry:
    """In-memory registry of compile jobs, keyed by ``compile_id``.

    Not thread-safe across OS threads -- this project runs one asyncio
    event loop (uvicorn's), and every method here is a plain
    (non-``async``) call that never yields control mid-mutation, so
    concurrent ``asyncio`` tasks calling into it interleave only at
    ``await`` points outside this class.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        #: Insertion order, oldest first, used to find the oldest
        #: finished job to evict without a linear rescan of self._jobs.
        self._order: list[str] = []

    def register(self, compile_id: str, graph_id: str) -> Job:
        """Create and register a new job for ``graph_id``.

        Args:
            compile_id: Unique id for the new job (the route layer
                mints this, typically a uuid).
            graph_id: The graph being compiled.

        Returns:
            The newly created, ``queued`` :class:`Job`.

        Raises:
            GraphCompileInProgressError: If a live (queued or running)
                job already exists for ``graph_id`` (PLAN.md: "One
                concurrent compile per graphId; a second request gets
                409").
        """
        for job in self._jobs.values():
            if job.graph_id == graph_id and job.is_live():
                raise GraphCompileInProgressError(graph_id, job.compile_id)
        job = Job(compile_id, graph_id)
        self._jobs[compile_id] = job
        self._order.append(compile_id)
        self._evict_finished_overflow()
        return job

    def get(self, compile_id: str) -> Job:
        """Look up a job by id.

        Raises:
            JobNotFoundError: If no such job is registered (never
                existed, or was evicted).
        """
        try:
            return self._jobs[compile_id]
        except KeyError:
            raise JobNotFoundError(compile_id) from None

    def cancel(self, compile_id: str) -> Job:
        """Cancel a live job's task and transition it to ``cancelled``.

        There is no subprocess to kill (fact 26) -- cancelling the
        ``asyncio.Task`` running the pipeline coroutine is the entire
        mechanism.

        Args:
            compile_id: The job to cancel.

        Returns:
            The now-``cancelled`` job.

        Raises:
            JobNotFoundError: If no such job is registered.
            ValueError: If the job is already finished (not live).
        """
        job = self.get(compile_id)
        if not job.is_live():
            raise ValueError(
                f"cannot cancel job {compile_id!r}: already {job.status!r}"
            )
        if job.task is not None:
            job.task.cancel()
        job.mark_cancelled()
        self._evict_finished_overflow()
        return job

    def mark_succeeded(self, compile_id: str, value: dict[str, object]) -> Job:
        """Transition a job to ``succeeded`` and evict overflow.

        The pipeline orchestrator (``pipeline.py``) must finish a job
        through the registry rather than calling :meth:`Job.mark_succeeded`
        directly, because eviction (PLAN.md: "at most 20 finished jobs")
        is a registry-wide invariant the registry alone can enforce --
        it has no way to notice a status change made directly on a
        ``Job`` instance it handed out earlier.

        Args:
            compile_id: The job to finish.
            value: The pipeline's success payload.

        Returns:
            The now-``succeeded`` job.

        Raises:
            JobNotFoundError: If no such job is registered.
            ValueError: If the job is not currently live.
        """
        job = self.get(compile_id)
        job.mark_succeeded(value)
        self._evict_finished_overflow()
        return job

    def mark_failed(self, compile_id: str, error: BaseException) -> Job:
        """Transition a job to ``failed`` and evict overflow.

        See :meth:`mark_succeeded` for why this must go through the
        registry rather than :meth:`Job.mark_failed` directly.

        Args:
            compile_id: The job to finish.
            error: The exception the pipeline raised.

        Returns:
            The now-``failed`` job.

        Raises:
            JobNotFoundError: If no such job is registered.
            ValueError: If the job is not currently live.
        """
        job = self.get(compile_id)
        job.mark_failed(error)
        self._evict_finished_overflow()
        return job

    async def shutdown(self) -> None:
        """Cancel every still-live job's task, for the server's shutdown hook.

        Awaits each cancelled task so the server does not exit while a
        pipeline coroutine is mid-step against, e.g., a half-written
        project directory.
        """
        live_jobs = [job for job in self._jobs.values() if job.is_live()]
        for job in live_jobs:
            if job.task is not None:
                job.task.cancel()
            job.mark_cancelled()
        tasks = [job.task for job in live_jobs if job.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _evict_finished_overflow(self) -> None:
        """Drop the oldest finished jobs past :data:`MAX_RETAINED_JOBS`.

        A running job is never evicted (PLAN.md), so eviction only ever
        considers finished jobs in insertion order and leaves every
        live job (and every finished job within the cap) in place.
        """
        finished_ids = [
            compile_id for compile_id in self._order if not self._jobs[compile_id].is_live()
        ]
        overflow = len(finished_ids) - MAX_RETAINED_JOBS
        if overflow <= 0:
            return
        for compile_id in finished_ids[:overflow]:
            del self._jobs[compile_id]
            self._order.remove(compile_id)


async def stream_events(
    job: Job, last_event_id: int | None
) -> AsyncIterator[CompileEvent]:
    """Yield replayed events after ``last_event_id`` and then live events.

    A thin convenience wrapper around :meth:`Job.events_after` and
    :meth:`Job.subscribe` for ``routes/compile.py``'s SSE handler: it
    replays retained history first, then switches to the live queue,
    always unsubscribing on exit (including when the consumer stops
    iterating early, e.g. the client disconnected).

    Args:
        job: The job to stream.
        last_event_id: The client's last-seen event id, or ``None`` for
            no prior history (a fresh subscription still only gets
            events from this point forward, since the caller should use
            :meth:`Job.snapshot` for a fresh tab's initial state).

    Yields:
        Each replayed event, then each subsequently appended event,
        until the job finishes and no more events arrive (the caller
        decides when to stop iterating, typically on a ``done``/
        ``error`` event type).

    Raises:
        EventLogGapError: Propagated from :meth:`Job.events_after` when
            ``last_event_id`` has fallen off the ring buffer.
    """
    for event in job.events_after(last_event_id):
        yield event
    queue = job.subscribe()
    try:
        while True:
            event = await queue.get()
            yield event
    finally:
        job.unsubscribe(queue)


__all__ = [
    "FILL_TIMEOUT_SECONDS",
    "MAX_RETAINED_JOBS",
    "RING_BUFFER_LINES",
    "CompileEvent",
    "EventLogGapError",
    "EventType",
    "GraphCompileInProgressError",
    "Job",
    "JobNotFoundError",
    "JobRegistry",
    "JobResult",
    "JobStatus",
    "stream_events",
]
