"""``/api/graphs/:id/runs``: start a run, list past runs, read one.

A run is a job in the same registry as a compile (``compile/jobs.py``,
``kind="run"``), so its live stream, status snapshot and cancel are
served by the generic job endpoints -- ``GET /api/jobs/:id/events``,
``GET /api/jobs/:id``, ``DELETE /api/jobs/:id`` in ``routes/compile.py``
-- and this module only owns what is run-specific: starting one, the
persisted history, and the staleness/credential checks that decide
whether starting is allowed.

Same lazy-import degradation seam as ``routes/compile.py``: the run
subsystem shells out to ``uv`` and (via compile-if-stale) calls a model,
so a broken import fails these endpoints with 503 rather than server
startup.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from swarm_builder import runtime
from swarm_builder.config import get_dsh_home, get_workspace_dir
from swarm_builder.models import SwarmGraph
from swarm_builder.routes.compile import (
    _load_filler,
    _load_run_compile,
    _PipelineUnavailableError,
    _require_registry,
)
from swarm_builder.routes.graphs import _load_graph_or_http_error
from swarm_builder.store._ids import InvalidGraphIdError
from swarm_builder.store.projects import project_dir
from swarm_builder.store.runs import (
    NodeRunRecord,
    RunRecord,
    RunStoreError,
    get_run,
    list_runs,
    put_run,
)

if TYPE_CHECKING:
    from swarm_builder.compile.jobs import Job, JobRegistry

router = APIRouter(tags=["runs"])


class _CamelModel(BaseModel):
    """camelCase-on-the-wire base, as in every other route module."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class StartRunRequest(_CamelModel):
    """Body of ``POST /api/graphs/:id/runs``.

    ``input`` is interpreted by the entry node's ``inputType``
    (``compile/run.py``'s ``coerce_input``): a ``str`` port takes the
    string verbatim; ``json``/``list[str]`` take the parsed value or a
    JSON string of it. ``compileIfStale`` (default on) recompiles a
    missing or out-of-date project before running; off, a stale project
    is a 409.
    """

    input: object
    compile_if_stale: bool = True


class StartRunResponse(_CamelModel):
    run_id: str
    will_compile: bool
    #: Whether this run executes against the generated project's keyless
    #: ``TestModel`` (dry run) rather than its real model.
    dry_run: bool


class RunListResponse(_CamelModel):
    runs: list[RunRecord]


def _load_run_module():
    """Import ``compile.run`` lazily (degradation seam)."""
    try:
        from swarm_builder.compile import run as run_module
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="run is not available: swarm_builder.compile.run could not be imported",
        ) from exc
    return run_module


@router.post(
    "/graphs/{graph_id}/runs",
    response_model=StartRunResponse,
    status_code=202,
    responses={
        404: {"description": "no such graph"},
        409: {"description": "a compile or run is already live, or the project is stale"},
        422: {"description": "the input does not fit the entry node's port type"},
        503: {"description": "the run subsystem is unavailable"},
    },
)
async def start_run(graph_id: str, body: StartRunRequest) -> StartRunResponse:
    """Start one run and return its job id.

    The input is coerced up front so a bad input is a 422 here rather
    than a failed job. Staleness is checked here too, so the response can
    say whether a compile will precede the run.
    """
    workspace_dir = get_workspace_dir()
    graph: SwarmGraph = _load_graph_or_http_error(workspace_dir, graph_id)
    run_module = _load_run_module()

    entry = next(node for node in graph.nodes if node.id == graph.entry_node_id)
    try:
        run_module.coerce_input(body.input, entry.io.input_type)
    except run_module.RunInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    target_dir = project_dir(workspace_dir, graph.id)
    stale = run_module.project_is_stale(target_dir, graph)
    if stale and not body.compile_if_stale:
        raise HTTPException(
            status_code=409,
            detail="the compiled project is missing or older than the graph; compile first "
            "or set compileIfStale",
        )

    # Lazy degradation seam -- see the module docstring.
    from swarm_builder.compile.jobs import GraphCompileInProgressError

    registry = _require_registry()
    run_id = uuid4().hex
    try:
        job = registry.register(run_id, graph.id, kind="run")
    except GraphCompileInProgressError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"graph {graph.id!r} already has a live job ({exc.compile_id!r}); "
            "cancel it or wait for it to finish",
        ) from exc

    # Frozen here, at job start, and passed explicitly to both the run and
    # the compile-it-may-perform-first: one run must never mix a stub compile
    # with a real model call (or the reverse) because the switch moved.
    dry_run = runtime.dry_run_active()

    _persist(
        workspace_dir, job, graph.id, body.input, created_at=datetime.now(UTC), dry_run=dry_run
    )
    job.task = asyncio.create_task(
        _run_job(
            graph,
            registry=registry,
            run_id=run_id,
            workspace_dir=workspace_dir,
            project_dir=target_dir,
            input_value=body.input,
            compile_if_stale=body.compile_if_stale,
            dry_run=dry_run,
        )
    )
    return StartRunResponse(run_id=run_id, will_compile=stale, dry_run=dry_run)


async def _run_job(
    graph: SwarmGraph,
    *,
    registry: JobRegistry,
    run_id: str,
    workspace_dir: Path,
    project_dir: Path,
    input_value: object,
    compile_if_stale: bool,
    dry_run: bool,
) -> None:
    """Drive one run to completion as its job's own task, then persist it.

    ``dry_run`` was decided at job start and is handed to both halves of this
    job: the compile that may run first (so a stub run never sits on top of a
    model-written project) and the tracer itself (via the child environment).
    """
    job = registry.get(run_id)
    created_at = job.created_at
    try:
        run_module = _load_run_module()
        compile_first = None
        if compile_if_stale:
            run_compile = _load_run_compile()
            # In dry run the model-backed filler will not be selected, so it is
            # not loaded: a job that cannot call a model must not fail because a
            # provider package is missing (health advertises it as runnable).
            filler = None if dry_run else _load_filler()

            async def compile_first() -> object:
                return await run_compile(
                    graph,
                    registry=registry,
                    compile_id=run_id,
                    project_dir=project_dir,
                    dsh_home=get_dsh_home(),
                    filler=filler,
                    finalize=False,
                    dry_run=dry_run,
                )

    except asyncio.CancelledError:
        # A cancel during the pre-flight is a cancelled run, not a stuck one.
        if job.is_live():
            job.mark_cancelled()
        _persist_best_effort(
            workspace_dir,
            job,
            graph.id,
            input_value,
            created_at=created_at,
            dry_run=dry_run,
        )
        raise
    except Exception as exc:  # noqa: BLE001
        # Deliberately broader than HTTPException: anything raised while the
        # run modules are imported would otherwise escape before the job was
        # ever marked terminal, leaving it ``queued`` forever with a task whose
        # exception nobody observes.
        registry.mark_failed(run_id, _PipelineUnavailableError(str(exc)))
        _persist(workspace_dir, job, graph.id, input_value, created_at=created_at, dry_run=dry_run)
        return

    try:
        await run_module.run_project(
            graph,
            registry=registry,
            run_id=run_id,
            project_dir=project_dir,
            input_value=input_value,
            compile_if_stale=compile_first,
            dry_run=dry_run,
            # The frozen decision, not a fresh read: the child must be
            # configured exactly as the rest of this job was.
            extra_env=runtime.child_env_overrides(dry_run=dry_run),
        )
    except asyncio.CancelledError:
        _persist_best_effort(
            workspace_dir,
            job,
            graph.id,
            input_value,
            created_at=created_at,
            dry_run=dry_run,
        )
        raise
    except Exception as exc:  # noqa: BLE001
        # Normally already recorded on the job by run_project (or by the
        # compile it performed). If it is somehow still live, marking it here
        # is what keeps a phantom ``running`` job out of the history.
        if job.is_live():
            job.append_event(
                "error",
                {"code": "run_failed", "message": str(exc), "exception": type(exc).__name__},
            )
            registry.mark_failed(run_id, exc)
    _persist(workspace_dir, job, graph.id, input_value, created_at=created_at, dry_run=dry_run)


def _persist(
    workspace_dir: Path,
    job: Job,
    graph_id: str,
    input_value: object,
    *,
    created_at: datetime,
    dry_run: bool,
) -> None:
    """Write the run's record from the job's retained events. Best effort:
    a failed write must not fail the run itself.

    ``dry_run`` is the value frozen at job start, recorded directly rather than
    inferred from the terminal event: a job that failed or was cancelled before
    it finished never emits that event, and history must not present a stub run
    as a real (potentially billed) one.
    """
    record = _record_from_job(job, graph_id, input_value, created_at, dry_run=dry_run)
    try:
        put_run(workspace_dir, record)
    except (RunStoreError, InvalidGraphIdError):
        pass


def _persist_best_effort(
    workspace_dir: Path,
    job: Job,
    graph_id: str,
    input_value: object,
    *,
    created_at: datetime,
    dry_run: bool,
) -> None:
    """Persist from an exception path, never letting a failure escape.

    Used on cancellation: a write that raised here would replace the
    ``CancelledError`` and turn a cancelled run into a failed task.
    """
    try:
        _persist(
            workspace_dir,
            job,
            graph_id,
            input_value,
            created_at=created_at,
            dry_run=dry_run,
        )
    except Exception:  # noqa: BLE001 - a lost record must not mask a cancellation
        pass


def _record_from_job(
    job: Job, graph_id: str, input_value: object, created_at: datetime, *, dry_run: bool
) -> RunRecord:
    nodes: dict[str, NodeRunRecord] = {}
    model: str | None = None
    compiled = False
    for event in job.events_after(None):
        payload = event.payload
        if event.event_type == "node":
            node_id = payload.get("nodeId")
            if not isinstance(node_id, str):
                continue
            previous = nodes.get(node_id)
            state_delta = payload.get("stateDelta")
            duration = payload.get("durationMs")
            nodes[node_id] = NodeRunRecord(
                status=str(payload.get("status", "")),
                inputs=payload.get("inputs", previous.inputs if previous else None),
                output=payload.get("output", previous.output if previous else None),
                state_delta=state_delta if isinstance(state_delta, dict) else None,
                error=payload.get("error") if isinstance(payload.get("error"), str) else None,
                duration_ms=duration if isinstance(duration, int) else None,
            )
        elif event.event_type == "run":
            if payload.get("status") == "started" and isinstance(payload.get("model"), str):
                model = payload["model"]  # type: ignore[assignment]
            if payload.get("compiled") is True:
                compiled = True
            # The terminal payload is corroboration only; the frozen value
            # passed in is what gets recorded.
            if payload.get("dryRun") is True:
                dry_run = True

    result = job.result.value or {}
    state = result.get("state")
    duration = result.get("durationMs")
    return RunRecord(
        run_id=job.compile_id,
        graph_id=graph_id,
        status=job.status,
        created_at=created_at.isoformat(),
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
        input=input_value,
        output=result.get("output"),
        state=state if isinstance(state, dict) else None,
        error=str(job.result.error) if job.result.error is not None else None,
        model=model,
        duration_ms=duration if isinstance(duration, int) else None,
        compiled=compiled or result.get("compiled") is True,
        dry_run=dry_run or result.get("dryRun") is True,
        nodes=nodes,
    )


@router.get("/graphs/{graph_id}/runs", response_model=RunListResponse)
def list_runs_route(graph_id: str) -> RunListResponse:
    """Persisted runs for a graph, newest first (at most 20 are kept)."""
    try:
        return RunListResponse(runs=list_runs(get_workspace_dir(), graph_id))
    except InvalidGraphIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/graphs/{graph_id}/runs/{run_id}",
    response_model=RunRecord,
    responses={404: {"description": "no such run record"}},
)
def get_run_route(graph_id: str, run_id: str) -> RunRecord:
    """One persisted run record (the live view is the job snapshot)."""
    try:
        record = get_run(get_workspace_dir(), graph_id, run_id)
    except InvalidGraphIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RunStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} for graph {graph_id!r}")
    return record
