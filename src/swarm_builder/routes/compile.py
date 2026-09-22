"""Every ``/api/compile*`` path: start a compile, stream its events,
read its status snapshot, cancel it (PLAN.md HTTP API table).

Replaces Group 3's documented 501 seam (GROUP3_PLAN.md finding 15) with
the real job orchestration. The four handlers are a thin HTTP skin over
``compile/jobs.py`` + ``compile/pipeline.py``, both of which are already
implemented and tested:

* ``POST /api/compile`` loads the saved graph, mints a ``compileId``,
  registers the job, and schedules ``run_compile`` as a background
  asyncio task. It returns as soon as the task is scheduled -- never
  awaits the compile.
* ``GET /api/compile/{compile_id}/events`` is the SSE stream, honoring
  ``Last-Event-ID`` for reconnect replay.
* ``GET /api/compile/{compile_id}`` is the status snapshot a fresh tab
  (no ``Last-Event-ID`` at all) uses.
* ``DELETE /api/compile/{compile_id}`` cancels.

The three job endpoints are also mounted under ``/api/jobs/{id}`` (hidden
from the OpenAPI document as duplicates): a *run* job (``routes/runs.py``,
``kind="run"``) lives in the same registry and is streamed, snapshotted
and cancelled through exactly the same handlers.

**The registry is the one deliberate exception to this package's no-
caching rule** (``routes/__init__.py``'s module docstring). Env vars and
``settings.yaml`` are hot-reloadable and must be re-read per request; a
job registry is the opposite -- process-lifetime state that must NOT be
re-created per request, or no job would survive long enough for its own
SSE stream to attach. It is therefore a lazily-created module singleton,
and ``main.py`` wires its ``shutdown()`` into the app's lifespan.

**Why the compile-subsystem imports are lazy (an arbitrated leave from
this project's "all imports at module level" convention).**
``swarm_builder.compile.*`` is the largest and most failure-prone
subsystem in this codebase: it shells out to ``uv`` and calls a model.
Importing it at module level would make a broken compile subsystem fail
*server startup* -- every unrelated endpoint (health, graphs, templates,
models, export) would go down with it. Importing it inside the handlers
makes the failure local: ``/api/compile*`` answers 503 and everything else
keeps working. Each such import below carries a one-line comment saying
so.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sse_starlette import EventSourceResponse, JSONServerSentEvent, ServerSentEvent

from swarm_builder.config import get_dsh_home, get_workspace_dir
from swarm_builder.models import SwarmGraph
from swarm_builder.routes.graphs import _load_graph_or_http_error
from swarm_builder.store.projects import langgraph_project_dir, project_dir

if TYPE_CHECKING:
    # Annotations only. The runtime import of the compile subsystem is
    # deliberately deferred into the functions that need it -- see this
    # module's docstring.
    from swarm_builder.compile.jobs import (
        CompileEvent,
        EventLogGapError,
        Job,
        JobRegistry,
    )

router = APIRouter(tags=["compile"])

#: The SSE keep-alive interval, in seconds. Frames are only produced when
#: the compile appends an event, and a phase (Phase 5's ``uv sync`` in
#: particular) can hold a connection idle for minutes; without a comment
#: heartbeat an idle connection looks dead to any intermediary that
#: proxies it, and the browser's ``fetch``-based reader sees nothing at
#: all. PLAN.md supersedes ``EventSource`` in favor of ``fetch`` +
#: ``ReadableStream``, so the heartbeat is also what keeps the client's
#: reader loop observably alive while no real event is pending.
SSE_PING_INTERVAL_SECONDS = 15

#: Header a reconnecting SSE client carries: the ``id`` of the last frame
#: it received. Read through Starlette's case-insensitive
#: ``Headers.get``, so the client's exact casing does not matter.
LAST_EVENT_ID_HEADER = "Last-Event-ID"

#: Prefix of the SSE comment emitted when the client's ``Last-Event-ID``
#: fell off the ring buffer *after* frames had already been flushed -- see
#: :func:`_job_events`. A comment line (``:``-prefixed) is invisible to a
#: naive ``data:``-only parser but is still a valid frame, so a client
#: that knows to look for it recovers, while one that does not at least
#: sees the stream end instead of a silently-truncated history.
GAP_COMMENT_PREFIX = "event-log-gap"

#: Media type of the event stream. An error that cannot be delivered as a
#: status code is delivered as a frame *inside* the 200 body instead, so
#: the client needs to know which kind of stream it opened.
SSE_MEDIA_TYPE = "text/event-stream"

#: The event types that END a compile's stream. ``stream_events`` never
#: stops on its own (it waits on a live subscriber queue forever), so the
#: SSE handler is the only thing that can complete this response, and it
#: must do so on exactly the one terminal event the pipeline emits.
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({"done", "error"})

#: ``phase`` event status that also ends the stream. The pipeline emits a
#: terminal ``done``/``error`` event, and a failing phase emits a
#: ``failed`` phase event before it; ending on both means a job that dies
#: without its own terminal event still closes the response instead of
#: leaving the client's reader open forever.
PHASE_STATUS_FAILED = "failed"

#: Job statuses that still have events ahead of them. A job in any other
#: status is terminal, so its retained log is its complete log.
_LIVE_JOB_STATUSES: frozenset[str] = frozenset({"queued", "running"})

#: The one job registry for this process. Deliberately module-level --
#: see the module docstring for why this is not per-request state. It is
#: typed as the concrete class only for readers; at runtime it holds
#: whatever ``compile/jobs.py`` exports once the lazy import succeeds.
_REGISTRY: JobRegistry | None = None


class _PipelineUnavailableError(RuntimeError):
    """Raised in lieu of a pipeline that could not be imported.

    A dedicated type so a job that failed because the compile subsystem
    is missing is distinguishable from one that failed while compiling,
    both in the job's recorded error and in the SSE ``error`` event's
    exception name.
    """


def _require_registry() -> JobRegistry:
    """Return the process-wide job registry, creating it on first use.

    Lazy for the degradation reason in this module's docstring: a
    ``compile/jobs.py`` that cannot be imported must fail the four
    compile endpoints, not ``create_app()`` (and with it every other
    endpoint in the app, plus the server's own startup).

    Returns:
        The singleton :class:`~swarm_builder.compile.jobs.JobRegistry`.

    Raises:
        HTTPException: 503 if the job registry module cannot be imported.
    """
    global _REGISTRY
    if _REGISTRY is None:
        try:
            # Lazy: degradation seam, so a broken compile subsystem fails
            # this endpoint instead of server startup (module docstring).
            from swarm_builder.compile.jobs import JobRegistry
        except ImportError as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "compile is not available: swarm_builder.compile.jobs "
                    "could not be imported"
                ),
            ) from exc
        _REGISTRY = JobRegistry()
    return _REGISTRY


async def shutdown_jobs() -> None:
    """Cancel and await every live compile, for the app's shutdown hook.

    A no-op when no compile has ever run: the registry is created on
    first use, so an untouched server never builds one just to shut it
    down.
    """
    if _REGISTRY is not None:
        await _REGISTRY.shutdown()


def _reset_registry() -> None:
    """Drop the process-wide registry so the next request builds a fresh one.

    For tests only: a test that leaked live jobs into the next test would
    see a spurious 409 for a graph it never compiled. Production code must
    never call this -- discarding the registry abandons every job (and
    every open SSE stream) along with it.
    """
    global _REGISTRY
    _REGISTRY = None


class _CamelModel(BaseModel):
    """Local replica of ``models.py``'s two-line camelCase config -- see
    ``routes/health.py``'s identical class for the rationale. Every
    response body this module defines is camelCase on the wire, matching
    the graph/health/export routes the frontend already consumes."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class StartCompileRequest(_CamelModel):
    """Body of ``POST /api/compile``.

    ``target`` selects the export: ``pydantic-graph`` (default, the five
    phases) or ``langgraph`` (the five phases, then a conversion of the
    validated project into a LangGraph export under
    ``workspace/projects-langgraph/<graphId>/``).
    """

    graph_id: str
    target: Literal["pydantic-graph", "langgraph"] = "pydantic-graph"


class StartCompileResponse(_CamelModel):
    """Body of a successful ``POST /api/compile``.

    Only the id: the compile itself runs in the background and reports
    everything else through the event stream and the status snapshot.
    """

    compile_id: str


class CompileSnapshotResponse(_CamelModel):
    """Body of ``GET``/``DELETE /api/compile/{compile_id}``.

    Mirrors :meth:`~swarm_builder.compile.jobs.Job.snapshot`'s dict
    one-for-one, including ``latestEventId`` -- the value a fresh client
    passes back as ``Last-Event-ID`` when it wants to resume rather than
    replay from the start.
    """

    compile_id: str
    graph_id: str
    kind: Literal["compile", "run"] = "compile"
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    created_at: str
    started_at: str | None
    finished_at: str | None
    latest_event_id: int | None
    result: dict[str, object] | None
    error: str | None


def _snapshot_response(job: Job) -> CompileSnapshotResponse:
    """Adapt one job's snapshot dict to the declared response model.

    Args:
        job: The job to snapshot.

    Returns:
        The validated response body.
    """
    return CompileSnapshotResponse.model_validate(job.snapshot())


def _job_or_http_error(compile_id: str) -> Job:
    """Look up a job, mapping a missing id to 404.

    Args:
        compile_id: The job to look up.

    Returns:
        The registered job.

    Raises:
        HTTPException: 404 when no such job is registered (never existed,
            or evicted); 503 if the compile subsystem cannot be imported.
    """
    # Lazy degradation seam -- see the module docstring.
    from swarm_builder.compile.jobs import JobNotFoundError

    try:
        return _require_registry().get(compile_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _parse_last_event_id(request: Request, compile_id: str) -> int | None:
    """Parse the ``Last-Event-ID`` request header.

    Args:
        request: The incoming request, for its headers.
        compile_id: The job the header applies to, named in the error.

    Returns:
        The client's last-seen event id, or ``None`` when the header is
        absent (a fresh subscription) or blank.

    Raises:
        HTTPException: 400 if the header is present but not an integer --
            a client that sent a malformed cursor is told so, rather than
            being silently served a full replay it did not ask for.
    """
    raw = request.headers.get(LAST_EVENT_ID_HEADER)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"compile {compile_id!r}: {LAST_EVENT_ID_HEADER} {raw!r} is not "
                "an integer event id"
            ),
        ) from exc


def _is_terminal_event(event: CompileEvent) -> bool:
    """Return whether ``event`` is the compile's terminal frame.

    Args:
        event: The event to test.

    Returns:
        Whether the stream should end after this event.
    """
    if event.event_type in TERMINAL_EVENT_TYPES:
        return True
    return event.event_type == "phase" and event.payload.get("status") == PHASE_STATUS_FAILED


def _to_sse_frame(event: CompileEvent) -> ServerSentEvent:
    """Render one compile event as an SSE frame.

    ``id`` is the event's own monotonic ``event_id``, so the browser
    echoes it back as ``Last-Event-ID`` on reconnect and the server can
    replay strictly after it. ``event`` is the compile's own event type
    (``phase``/``log``/``warning``/``done``/``error``), so a client
    dispatches on the SSE event name instead of inspecting a
    discriminator inside the JSON payload; ``data`` is the payload plus
    ``eventId`` and ``createdAt``, so a consumer that only parses
    ``data`` still sees the complete record.

    ``JSONServerSentEvent`` rather than ``ServerSentEvent``: the latter
    would emit a Python ``repr`` of the dict, which is not JSON and not
    parseable by ``JSON.parse`` on the client.

    Args:
        event: The compile event to render.

    Returns:
        The frame to yield.
    """
    data = dict(event.payload)
    data["eventId"] = event.event_id
    data["createdAt"] = event.created_at.isoformat()
    return JSONServerSentEvent(data=data, event=event.event_type, id=str(event.event_id))


def _gap_details(exc: EventLogGapError, compile_id: str) -> dict[str, object]:
    """Describe an evicted-history gap in the terms a client can act on.

    The client's cursor fell off the back of the job's ring buffer, so a
    correct replay is impossible. Rather than sending a partial history
    that looks complete, the details name the range that was missed and
    point at the two recovery routes: the status snapshot (which the
    client already knows how to render) and a fresh stream with no
    cursor at all.

    Args:
        exc: The ring-buffer gap the replay hit.
        compile_id: The job whose history is incomplete.

    Returns:
        A JSON-serializable body.
    """
    return {
        "gap": True,
        "message": (
            "compile log history was evicted before this client could reconnect: "
            f"events after id {exc.last_event_id} are no longer retained (oldest "
            f"retained id is {exc.oldest_retained_id}); fetch the status snapshot "
            "instead of resuming the stream"
        ),
        "compileId": compile_id,
        "requestedLastEventId": exc.last_event_id,
        "oldestRetainedEventId": exc.oldest_retained_id,
        "snapshotUrl": f"/api/compile/{compile_id}",
    }


async def _job_events(
    job: Job, last_event_id: int | None, compile_id: str
) -> AsyncIterator[ServerSentEvent]:
    """Yield SSE frames for ``job``, ending on its terminal event.

    ``stream_events`` (the jobs module's own replay-then-live generator)
    is the source of compile events; this wrapper owns only the HTTP
    concerns -- frame rendering, and closing the response on the terminal
    event.

    An ``EventLogGapError`` is handled in two places, because the correct
    HTTP answer depends on how much has already been sent:

    * Raised before the first frame, it propagates to
      :func:`compile_events`, which can still answer with a real HTTP
      status (400) instead of a body the client must scrub for a warning.
    * Raised after frames are already on the wire, the status is already
      committed to 200, so a ``:`` comment frame carrying the same
      recovery instructions is emitted and the stream is closed. Closing
      is what forces the client's fetch reader to reconnect, and that
      next request -- carrying its unchanged cursor -- is refused up
      front with the 400 above, so the client converges on the snapshot
      rather than looping on a partial log.

    Args:
        job: The job to stream.
        last_event_id: The client's last-seen event id, or ``None``.
        compile_id: The job's id, for the gap frame's body.

    Yields:
        One frame per compile event, ending after the terminal one.

    Raises:
        EventLogGapError: Before the first frame, when the client's
            cursor has already fallen off the ring buffer.
    """
    # Lazy degradation seam -- see the module docstring.
    from swarm_builder.compile.jobs import EventLogGapError, stream_events

    frames_sent = False

    # A job that already reached a terminal state retains its *complete*
    # log, so its whole response is a replay: nothing is sent until that
    # replay finishes, which is why a gap found here can still be
    # answered with a real HTTP status. ``stream_events`` never returns
    # on its own, so subscribing would instead leave this client waiting
    # forever on a queue no producer will ever feed again -- for a
    # compile whose outcome is already known.
    if job.status not in _LIVE_JOB_STATUSES:
        try:
            for frame in _replayed_frames(job, last_event_id, compile_id):
                frames_sent = True
                yield frame
        except EventLogGapError as exc:
            if not frames_sent:
                raise
            yield _gap_frame(exc, compile_id)
        return

    # A live job's history is replayed by ``stream_events`` itself, which
    # yields ``events_after(last_event_id)`` before it subscribes to the
    # live tail. Replaying here as well sent every retained frame twice
    # (verified: cursor 2 against a 4-event job delivered ids 3, 4, 3, 4),
    # which is precisely the duplicate-log failure the Last-Event-ID
    # design exists to prevent. The first event is pulled eagerly so that
    # a cursor which has fallen off the ring buffer can still be answered
    # with a real HTTP status rather than an in-band warning.
    stream = stream_events(job, last_event_id)
    first_event = await anext(stream, None)
    if first_event is None:
        return

    frames_sent = True
    yield _to_sse_frame(first_event)
    if _is_terminal_event(first_event):
        return

    try:
        async for event in stream:
            yield _to_sse_frame(event)
            if _is_terminal_event(event):
                return
    except EventLogGapError as exc:
        # Frames are already on the wire, so the status is committed to
        # 200 and the gap can only be reported in band. Closing the
        # stream is what makes the client reconnect; that next request,
        # carrying the same cursor, gets the 400 above, so a client
        # converges on the snapshot instead of looping on a partial log.
        yield _gap_frame(exc, compile_id)


def _gap_frame(exc: EventLogGapError, compile_id: str) -> ServerSentEvent:
    """Render an in-band ring-buffer gap report.

    A comment frame, not a data frame: it must be ignorable by a parser
    that only understands ``data:``, and the details ride along as
    compact JSON so a client that does understand it can act on the
    snapshot URL without another round trip.

    Args:
        exc: The gap the replay hit.
        compile_id: The job whose history is incomplete.

    Returns:
        The comment frame to yield.
    """
    return ServerSentEvent(
        comment=f"{GAP_COMMENT_PREFIX} {json.dumps(_gap_details(exc, compile_id))}"
    )


def _replayed_frames(
    job: Job, last_event_id: int | None, compile_id: str
) -> Iterator[ServerSentEvent]:
    """Yield the retained frames of a job's log after ``last_event_id``.

    Synchronous on purpose: the retained log is read in one shot
    (``Job.events_after`` returns a list), and keeping the replay
    synchronous is what lets the caller notice a ring-buffer gap *before*
    any frame has been sent.

    Args:
        job: The job to replay.
        last_event_id: The client's last-seen event id, or ``None``.
        compile_id: The job's id (unused; frames carry their own id).

    Yields:
        Every retained frame strictly after ``last_event_id``.

    Raises:
        EventLogGapError: If ``last_event_id`` fell off the ring buffer.
    """
    del compile_id
    for event in job.events_after(last_event_id):
        yield _to_sse_frame(event)


async def _prepend_frame(
    first_frame: ServerSentEvent, rest: AsyncIterator[ServerSentEvent]
) -> AsyncIterator[ServerSentEvent]:
    """Re-yield ``first_frame`` ahead of the rest of the stream.

    :func:`compile_events` pulls the first frame eagerly, so it can turn a
    ring-buffer gap into a real status code; that frame must then be put
    back in front of the frames the response sends, or the client's very
    first event would be silently dropped.

    Args:
        first_frame: The frame already pulled.
        rest: The remaining frames.

    Yields:
        ``first_frame``, then every frame of ``rest``.
    """
    yield first_frame
    async for frame in rest:
        yield frame


@router.post(
    "/compile",
    response_model=StartCompileResponse,
    responses={
        404: {"description": "no such graph"},
        409: {"description": "a compile is already live for this graph"},
        503: {"description": "the compile subsystem is unavailable"},
    },
)
async def start_compile(body: StartCompileRequest) -> StartCompileResponse:
    """Start one compile job for a saved graph and return its id.

    The job is registered (which enforces one live compile per graph) and
    its coroutine is scheduled as an asyncio task; this handler never
    awaits the compile, so the response returns as soon as the work is
    scheduled.

    ``job.task`` is assigned before returning, and that assignment is
    load-bearing rather than bookkeeping: without it
    :meth:`~swarm_builder.compile.jobs.JobRegistry.cancel` has no task to
    cancel and the shutdown hook has nothing to await, so a ``DELETE``
    would mark the job cancelled while the pipeline kept running.

    The project directory is deliberately never created or cleared here.
    ``pipeline.run_compile`` clears a stale one itself, *after* Phase 1
    passes, which is what keeps the "a Phase-1 refusal leaves no project
    directory behind" invariant true.

    Raises:
        HTTPException: 404 if no such graph is saved; 422 for a
            malformed/path-unsafe graph id or an unreadable graph file;
            409 if a live compile already exists for this graph; 500 for
            a filesystem failure while reading the graph; 503 if the
            compile subsystem cannot be imported.
    """
    workspace_dir = get_workspace_dir()
    graph: SwarmGraph = _load_graph_or_http_error(workspace_dir, body.graph_id)

    # Lazy degradation seam -- see the module docstring.
    from swarm_builder.compile.jobs import GraphCompileInProgressError

    registry = _require_registry()
    compile_id = uuid4().hex

    try:
        job = registry.register(compile_id, graph.id)
    except GraphCompileInProgressError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"graph {graph.id!r} already has a live compile job "
                f"({exc.compile_id!r}); cancel it or wait for it to finish"
            ),
        ) from exc

    job.task = asyncio.create_task(
        _run_job(
            graph,
            registry=registry,
            compile_id=compile_id,
            workspace_dir=workspace_dir,
            target=body.target,
        )
    )
    return StartCompileResponse(compile_id=compile_id)


def _load_run_compile() -> Callable[..., Awaitable[None]]:
    """Import and return the pipeline's ``run_compile`` entry point.

    Lazy (see this module's docstring): the pipeline pulls in
    ``scaffold``/``boundary``/``validate`` and the ``uv``-invoking
    validation gate, so a failure there must degrade the compile
    endpoints rather than the whole server. Importing it here, on the
    job's own task, also means a broken pipeline fails *that job* (the
    caller marks it failed) instead of the process -- there is one
    import site, so there is one degradation path.

    Returns:
        ``compile.pipeline.run_compile``.

    Raises:
        HTTPException: 503 if the pipeline module cannot be imported.
    """
    try:
        # Lazy degradation seam (module docstring). The pipeline is the
        # heaviest module in the subsystem -- it pulls in the agent and the
        # validation gate -- so this is the import most worth keeping out
        # of server startup.
        from swarm_builder.compile.pipeline import run_compile
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "compile is not available: swarm_builder.compile.pipeline "
                "could not be imported"
            ),
        ) from exc
    return run_compile


def _load_filler() -> Callable[..., Awaitable[object]]:
    """Import and return the real Phase-3 fill agent.

    Lazy for the same reason as :func:`_load_run_compile` (see this
    module's docstring): the agent imports ``pydantic_ai``, and a
    provider extra missing from this server's environment must degrade
    the compile endpoints rather than prevent startup.

    Returns:
        ``compile.agent.fill``, satisfying the pipeline's ``Filler``
        protocol.

    Raises:
        HTTPException: 503 if the fill agent cannot be imported.
    """
    try:
        # Lazy degradation seam (module docstring): pydantic_ai and its
        # provider extras are only needed once a real compile runs, and
        # an offline SWARM_FAKE_FILL=1 compile must work even if a
        # provider package is absent.
        from swarm_builder.compile.agent import fill
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "compile is not available: swarm_builder.compile.agent "
                "could not be imported"
            ),
        ) from exc
    return fill


async def _run_job(
    graph: SwarmGraph,
    *,
    registry: JobRegistry,
    compile_id: str,
    workspace_dir: Path,
    target: str = "pydantic-graph",
) -> None:
    """Drive one compile to completion as its job's own asyncio task.

    Every path argument is resolved here, at task start, and every
    compile failure is already recorded on the job by ``run_compile``
    (it marks the job failed and emits the terminal ``error`` event
    itself), so all this wrapper adds is resolving the project directory
    and the harness settings home for the pipeline.

    A pipeline that cannot be imported fails the job through the
    registry rather than escaping as an unhandled task exception: nobody
    awaits this task, so an escaping exception would leave the job
    ``queued`` forever and any SSE client waiting for a terminal event
    that can never arrive.

    The real fill agent is injected here, which is what makes a
    model-backed compile reachable over HTTP at all. ``run_compile``
    ignores the injected filler when ``SWARM_FAKE_FILL=1``, so the
    offline path is unaffected; without this injection every real
    compile would fail Phase 3 with ``FillUnavailableError``.

    Args:
        graph: The document to compile.
        registry: The registry holding this compile's job.
        compile_id: The job's id.
        workspace_dir: The workspace root, for the project directory.
    """
    try:
        run_compile = _load_run_compile()
        filler = _load_filler()
    except HTTPException as exc:
        registry.mark_failed(compile_id, _PipelineUnavailableError(str(exc.detail)))
        return

    await run_compile(
        graph,
        registry=registry,
        compile_id=compile_id,
        project_dir=project_dir(workspace_dir, graph.id),
        dsh_home=get_dsh_home(),
        filler=filler,
        target=target,
        langgraph_project_dir=langgraph_project_dir(workspace_dir, graph.id),
    )


@router.get(
    "/jobs/{compile_id}/events",
    response_model=None,
    include_in_schema=False,
)
@router.get(
    "/compile/{compile_id}/events",
    response_model=None,
    responses={
        200: {"content": {SSE_MEDIA_TYPE: {}}, "description": "compile event stream"},
        400: {"description": "Last-Event-ID predates the retained log"},
        404: {"description": "no such compile job"},
        503: {"description": "the compile subsystem is unavailable"},
    },
)
async def compile_events(compile_id: str, request: Request) -> EventSourceResponse:
    """Stream one compile's events as SSE until it finishes.

    A reconnecting client sends ``Last-Event-ID`` and gets every retained
    event strictly after that id, then live events. The response is
    deliberately *finite*: it ends after the compile's terminal
    ``done``/``error`` event, so a client's fetch reader completes
    instead of hanging on a stream that will never produce another frame.

    Raises:
        HTTPException: 404 for an unknown (or evicted) ``compile_id``;
            400 when ``Last-Event-ID`` is not an integer, or when it
            predates the retained log (the body then carries the gap
            details and a snapshot URL); 503 if the compile subsystem
            cannot be imported.
    """
    job = _job_or_http_error(compile_id)
    last_event_id = _parse_last_event_id(request, compile_id)

    # Lazy degradation seam -- see the module docstring.
    from swarm_builder.compile.jobs import EventLogGapError

    # Pulling the first frame eagerly is what surfaces a ring-buffer gap
    # while a real HTTP status is still available: once a frame has been
    # sent the status is committed to 200, and only an in-band comment
    # can report the gap.
    stream = _job_events(job, last_event_id, compile_id)
    try:
        first_frame = await anext(stream)
    except StopAsyncIteration:
        # The first frame was also the terminal event, so the generator
        # finished on that one pull and there is nothing left to chain.
        return EventSourceResponse(
            _replay_frames(job, last_event_id, compile_id),
            ping=SSE_PING_INTERVAL_SECONDS,
            media_type=SSE_MEDIA_TYPE,
        )
    except EventLogGapError as exc:
        raise HTTPException(status_code=400, detail=_gap_details(exc, compile_id)) from exc

    return EventSourceResponse(
        _prepend_frame(first_frame, stream),
        ping=SSE_PING_INTERVAL_SECONDS,
        media_type=SSE_MEDIA_TYPE,
    )


async def _replay_frames(
    job: Job, last_event_id: int | None, compile_id: str
) -> AsyncIterator[ServerSentEvent]:
    """Re-render a job's stream when its first frame completed it.

    Args:
        job: The job to stream.
        last_event_id: The client's last-seen event id, or ``None``.
        compile_id: The job's id, for a gap frame's body.

    Yields:
        Every frame of the job, in order.
    """
    async for frame in _job_events(job, last_event_id, compile_id):
        yield frame


@router.get("/jobs/{compile_id}", response_model=CompileSnapshotResponse, include_in_schema=False)
@router.get(
    "/compile/{compile_id}",
    response_model=CompileSnapshotResponse,
    responses={
        404: {"description": "no such compile job"},
        503: {"description": "the compile subsystem is unavailable"},
    },
)
def compile_status(compile_id: str) -> CompileSnapshotResponse:
    """Return a compile's status snapshot.

    This is the path a fresh tab uses: it has no ``Last-Event-ID`` and no
    way to reconstruct history, so the snapshot gives it the current
    status, the latest retained event id (which it can then use as a
    cursor), and the terminal result or error.

    Raises:
        HTTPException: 404 for an unknown (or evicted) ``compile_id``;
            503 if the compile subsystem cannot be imported.
    """
    return _snapshot_response(_job_or_http_error(compile_id))


@router.delete(
    "/jobs/{compile_id}", response_model=CompileSnapshotResponse, include_in_schema=False
)
@router.delete(
    "/compile/{compile_id}",
    response_model=CompileSnapshotResponse,
    responses={
        404: {"description": "no such compile job"},
        409: {"description": "the compile has already finished"},
        503: {"description": "the compile subsystem is unavailable"},
    },
)
def cancel_compile(compile_id: str) -> CompileSnapshotResponse:
    """Cancel a live compile.

    There is no subprocess to kill (PLAN.md fact 26): a compile is one
    asyncio task, so cancelling that task IS the whole mechanism, and the
    job ends ``cancelled``.

    **A job that already finished is a 409, not a no-op 200.** The
    request asks for a transition that is no longer possible and cannot
    be retried into success, so the honest answer is a conflict naming
    the status the job actually reached -- with the full snapshot in the
    body, so the client needs no second request to learn the outcome it
    was too late to prevent.

    Raises:
        HTTPException: 404 for an unknown (or evicted) ``compile_id``;
            409 if the job is no longer live; 503 if the compile
            subsystem cannot be imported.
    """
    # Lazy degradation seam -- see the module docstring.
    from swarm_builder.compile.jobs import JobNotFoundError

    registry = _require_registry()
    try:
        job = registry.cancel(compile_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # registry.cancel raises ValueError only for "already finished",
        # and the job is still registered (a missing one is the 404
        # above), so it cannot have been evicted between these two looks.
        finished = registry.get(compile_id)
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "compileId": compile_id,
                "status": finished.status,
                "snapshot": finished.snapshot(),
            },
        ) from exc

    return _snapshot_response(job)
