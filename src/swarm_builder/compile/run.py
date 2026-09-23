"""Run an already-compiled project and stream one event per step.

The *Run* feature (``PLAN-V2-FEATURES.md``, Feature 1). A run is a
:class:`~swarm_builder.compile.jobs.Job` of kind ``run`` that shares the
compile subsystem's registry, ring-buffered event log, SSE framing and
cancellation. This module owns what is specific to running:

1. **Input coercion** (:func:`coerce_input`): the entry node's declared
   ``inputType`` decides how the client's raw value is interpreted, and a
   value that cannot be read as that type is refused *before* any
   subprocess starts.
2. **Staleness** (:func:`project_is_stale`): a project is stale when it is
   missing, predates the tracer script (compiled before this feature), or
   its ``graph.py`` is older than the graph document's ``updatedAt``. A
   stale project is recompiled first, through the same
   :func:`~swarm_builder.compile.pipeline.run_compile` a plain compile
   uses, with ``finalize=False`` so the compile's phase events stream into
   this run's job and the run owns the terminal event.
3. **Execution** (:func:`run_project`): ``uv run python run/stream_run.py
   <input.json>`` in the project directory, in its own session, killed as
   a process *group* on timeout or cancellation (the same reason
   ``validate.py`` kills a group: ``uv`` forks the interpreter that
   actually runs the project). stdout is one JSON object per line
   (``compile/scaffold.py``'s tracer); each is re-emitted as a job event.

**This executes model-authored code with the caller's real credentials.**
That is the same trust boundary Phase 5 already crosses -- ``dry_run.py``
runs the filled bodies in a subprocess -- plus network access and whatever
credentials the server's environment carries. Unlike ``validate.py``, the
environment is passed through *unstripped*: a run that could not reach a
model would be pointless. The bounds are a wall-clock timeout, process-
group kill, no shell (the input travels in a file, never on a command
line), and the route layer's requirement that the health check reports
``runReady`` before the button is enabled.

**Event vocabulary.** ``run`` frames carry ``{status: "compiling" |
"starting" | "started", ...}``; ``node`` frames carry ``{nodeId, status:
"started" | "succeeded" | "failed", inputs?, output?, stateDelta?,
error?, durationMs?}``; the terminal ``done`` carries ``{output, state,
durationMs, model, compiled}`` and ``error`` carries ``{code:
"run_failed", message, exception}``. Non-JSON stdout and every stderr line
become ``log`` frames.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from swarm_builder import runtime
from swarm_builder.compile.jobs import Job, JobRegistry
from swarm_builder.compile.scaffold import STREAM_RUN_INPUT_KEY
from swarm_builder.config import get_uv_cache_dir
from swarm_builder.models import PortType, SwarmGraph

#: Wall-clock bound for one run of the generated project, tracer
#: included. Generous because a workflow may make many model calls; the
#: bound exists so a wedged model cannot hold the job (and the graph's
#: one-live-job slot) forever.
RUN_TIMEOUT_SECONDS = 900

#: The ``error`` frame's code for every run failure, mirroring the
#: compile's single ``phase_failed`` code: the message says why.
ERROR_CODE_RUN_FAILED = "run_failed"

#: How many trailing stderr lines a failure report keeps.
STDERR_TAIL_LINES = 40

#: Files whose presence and age decide staleness (relative to the project).
STREAM_RUN_RELATIVE_PATH = Path("run") / "stream_run.py"
GRAPH_MODULE_RELATIVE_PATH = Path("src") / "swarm_workflow" / "graph.py"

#: ``run`` frame statuses, in the order a compile-then-run job emits them.
RUN_STATUS_COMPILING = "compiling"
RUN_STATUS_STARTING = "starting"
RUN_STATUS_STARTED = "started"

#: ``node`` frame statuses. Distinct from the compile's phase statuses
#: (``started``/``succeeded``/``failed`` there too, but a different
#: vocabulary owner) and from a job's five-value status.
NODE_STATUS_STARTED = "started"
NODE_STATUS_SUCCEEDED = "succeeded"
NODE_STATUS_FAILED = "failed"

#: Tracer stdout ``event`` names -> the ``node`` frame status they map to.
_TRACER_NODE_EVENTS: dict[str, str] = {
    "node_started": NODE_STATUS_STARTED,
    "node_finished": NODE_STATUS_SUCCEEDED,
    "node_failed": NODE_STATUS_FAILED,
}


class RunInputError(ValueError):
    """The client's input cannot be read as the entry node's port type."""


class RunError(RuntimeError):
    """The run itself failed: the tracer reported a failure, exited
    without a result, or timed out."""


class ProjectStaleError(RunError):
    """The project is missing or stale and the caller disabled
    compile-if-stale."""


# ---------------------------------------------------------------------------
# Input coercion
# ---------------------------------------------------------------------------


def coerce_input(raw: object, port_type: PortType) -> object:
    """Interpret the client's raw input as the entry node's port type.

    A ``str`` port takes any string verbatim. ``json`` and ``list[str]``
    take either the already-parsed value or a JSON string of it -- the
    latter because the panel's input box is a text field, and making the
    client parse JSON before sending would put a second copy of this rule
    in the frontend.

    Args:
        raw: The value as it arrived in the request body.
        port_type: The entry node's declared ``inputType``.

    Returns:
        The value to hand to ``graph.run(inputs=...)``.

    Raises:
        RunInputError: If ``raw`` cannot be read as ``port_type``. The
            message names the port type and what was received.
    """
    if port_type == "str":
        if isinstance(raw, str):
            return raw
        raise RunInputError(f"entry node expects a str input; got {type(raw).__name__}")

    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunInputError(
                f"entry node expects {port_type}; the input is not valid JSON ({exc.msg})"
            ) from exc

    if port_type == "json":
        if isinstance(value, dict):
            return value
        raise RunInputError(f"entry node expects a JSON object; got {type(value).__name__}")

    if port_type == "list[str]":
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return value
        raise RunInputError("entry node expects a JSON list of strings")

    raise RunInputError(f"unsupported entry port type {port_type!r}")


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def _updated_at_timestamp(graph: SwarmGraph) -> float:
    updated_at = graph.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return updated_at.timestamp()


def project_is_stale(project_dir: Path, graph: SwarmGraph) -> bool:
    """Whether ``project_dir`` must be recompiled before it can be run.

    Stale means any of: no ``graph.py`` (never compiled, or a Phase-1
    refusal left nothing behind); no tracer script (compiled before the
    Run feature existed); or ``graph.py`` older than the document's
    ``updatedAt`` -- the frontend stamps ``updatedAt`` on every mutation
    and a compile writes ``graph.py`` after the save it compiled, so a
    newer ``updatedAt`` means an edit the project has not seen.

    Args:
        project_dir: The graph's project directory (may not exist).
        graph: The saved document.

    Returns:
        ``True`` when the project cannot be trusted to reflect ``graph``.
    """
    graph_module = project_dir / GRAPH_MODULE_RELATIVE_PATH
    tracer = project_dir / STREAM_RUN_RELATIVE_PATH
    if not graph_module.is_file() or not tracer.is_file():
        return True
    return graph_module.stat().st_mtime < _updated_at_timestamp(graph)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunOutcome:
    """What a finished run reported (also the ``done`` payload's source)."""

    output: object
    state: dict[str, object]
    duration_ms: int
    model: str | None
    compiled: bool
    finished_at: datetime
    #: Whether this run executed against the generated project's keyless
    #: ``TestModel`` instead of its real model (dry run). Reported so a UI can
    #: label stub output as stub output rather than presenting it as a real
    #: workflow result.
    dry_run: bool = False


#: The compile-first hook a run job is given: ``routes/runs.py`` binds
#: ``run_compile(..., finalize=False)`` with every path already resolved.
#: ``None`` means "refuse to run a stale project".
CompileFirst = Callable[[], Awaitable[object]]


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole session (``uv`` plus the interpreter it
    forked), falling back to the child alone if it is not a group leader."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _render_value(value: object) -> object:
    """Keep an event payload JSON-serializable even for odd tracer output."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


class _TracerState:
    """Accumulates what the tracer's stdout said, line by line."""

    def __init__(self, job: Job) -> None:
        self.job = job
        self.model: str | None = None
        self.result: dict[str, object] | None = None
        self.failure: dict[str, object] | None = None
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)

    def stdout_line(self, line: str) -> None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            self.job.append_event("log", {"message": line})
            return
        if not isinstance(record, dict):
            self.job.append_event("log", {"message": line})
            return

        event = record.get("event")
        if event == "run_started":
            model = record.get("model")
            self.model = model if isinstance(model, str) else None
            self.job.append_event(
                "run",
                {
                    "status": RUN_STATUS_STARTED,
                    "model": self.model,
                    "input": _render_value(record.get("input")),
                },
            )
        elif event in _TRACER_NODE_EVENTS:
            payload: dict[str, object] = {
                "nodeId": record.get("nodeId"),
                "status": _TRACER_NODE_EVENTS[event],
            }
            for key in ("inputs", "output", "stateDelta", "error", "traceback", "durationMs"):
                if key in record:
                    payload[key] = _render_value(record[key])
            self.job.append_event("node", payload)
        elif event == "run_finished":
            self.result = record
        elif event == "run_failed":
            self.failure = record
        else:
            self.job.append_event("log", {"message": line})

    def stderr_line(self, line: str) -> None:
        self.stderr_tail.append(line)
        self.job.append_event("log", {"message": line, "stream": "stderr"})


async def _pump(stream: asyncio.StreamReader, sink: Callable[[str], None]) -> None:
    while True:
        raw = await stream.readline()
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if line:
            sink(line)


async def _execute(
    job: Job,
    *,
    project_dir: Path,
    input_value: object,
    uv_cache_dir: Path,
    timeout_s: float,
    extra_env: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> tuple[_TracerState, int]:
    """Spawn the tracer, stream its output into ``job``, and reap it.

    Returns:
        The accumulated tracer state and the process exit code.

    Raises:
        RunError: On timeout (the process group is killed first).
        asyncio.CancelledError: Propagated after the process group is
            killed, so a cancelled run never leaks a child.
    """
    env = runtime.child_process_env(
        uv_cache_dir=uv_cache_dir, extra_env=extra_env, dry_run=dry_run
    )
    fd, input_path_str = tempfile.mkstemp(prefix="swarm-run-", suffix=".json")
    input_path = Path(input_path_str)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({STREAM_RUN_INPUT_KEY: input_value}, handle)

    state = _TracerState(job)
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "python",
            str(STREAM_RUN_RELATIVE_PATH),
            str(input_path),
            cwd=project_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _pump(process.stdout, state.stdout_line),
                    _pump(process.stderr, state.stderr_line),
                    process.wait(),
                ),
                timeout=timeout_s,
            )
        except TimeoutError as exc:
            _kill_process_group(process)
            await process.wait()
            raise RunError(f"run timed out after {timeout_s:.0f}s and was killed") from exc
        return state, process.returncode if process.returncode is not None else -1
    finally:
        if process is not None and process.returncode is None:
            _kill_process_group(process)
            await process.wait()
        try:
            input_path.unlink()
        except OSError:
            pass


def _failure_message(state: _TracerState, exit_code: int) -> str:
    if state.failure is not None:
        error = state.failure.get("error")
        message = error if isinstance(error, str) else "the workflow raised"
        trace = state.failure.get("traceback")
        if isinstance(trace, str) and trace.strip():
            return f"{message}\n{trace.rstrip()}"
        return message
    tail = "\n".join(state.stderr_tail)
    suffix = f"\nstderr tail:\n{tail}" if tail else ""
    return f"the runner exited with code {exit_code} without reporting a result{suffix}"


def _done_payload(outcome: RunOutcome) -> dict[str, object]:
    return {
        "output": _render_value(outcome.output),
        "state": _render_value(outcome.state),
        "durationMs": outcome.duration_ms,
        "model": outcome.model,
        "compiled": outcome.compiled,
        "dryRun": outcome.dry_run,
        "finishedAt": outcome.finished_at.isoformat(),
    }


async def run_project(
    graph: SwarmGraph,
    *,
    registry: JobRegistry,
    run_id: str,
    project_dir: Path,
    input_value: object,
    compile_if_stale: CompileFirst | None,
    uv_cache_dir: Path | None = None,
    timeout_s: float = RUN_TIMEOUT_SECONDS,
    dry_run: bool | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> RunOutcome:
    """Run ``graph``'s compiled project as the job registered under ``run_id``.

    Intended to run as the job's own asyncio task, like ``run_compile``.
    Coerces the input, recompiles first when the project is stale, then
    executes the tracer and relays its events. On success the terminal
    ``done`` event is appended and the job marked ``succeeded``; on
    failure ``error`` is appended and the job marked ``failed`` -- unless
    the compile-first step already did both, which a refusing Phase 1
    does on its own.

    Args:
        graph: The saved document (for the entry port type and staleness).
        registry: The registry holding this run's job.
        run_id: The job's id.
        project_dir: The graph's project directory.
        input_value: The client's raw input.
        compile_if_stale: Bound compile-first hook, or ``None`` to refuse
            a stale project with :class:`ProjectStaleError`.
        uv_cache_dir: Explicit ``UV_CACHE_DIR``; defaults to the env.
        timeout_s: Wall-clock bound for the tracer subprocess.
        dry_run: Whether the tracer should run against a keyless
            ``TestModel``. ``None`` reads the application's current state
            once, here, at job start, and the answer is then passed down to
            the child process as an environment override -- never re-read, so
            toggling the switch mid-run cannot change which model a
            half-finished run is using.
        extra_env: Extra environment variables for the tracer subprocess,
            merged last (so a caller's explicit decision wins over an
            inherited value). Used for the dry-run test model, and available
            for any future per-run override.

    Returns:
        The finished run.

    Raises:
        RunInputError: The input does not fit the entry port type.
        ProjectStaleError: Stale project and no compile hook.
        RunError: The workflow failed, the runner exited without a
            result, or the run timed out.
        Exception: Whatever ``compile_if_stale`` raised (typically
            ``PhaseFailureError``), after the compile marked the job.
        asyncio.CancelledError: Re-raised unchanged; the job is marked
            cancelled and the child process group killed.
    """
    job = registry.get(run_id)
    if job.status == "queued":
        job.mark_running()
    resolved_uv_cache_dir = uv_cache_dir if uv_cache_dir is not None else get_uv_cache_dir()
    if dry_run is None:
        from swarm_builder import runtime

        dry_run = runtime.dry_run_active()
    elif not dry_run:
        from swarm_builder import runtime

        # As in ``run_compile``: an exported ``SWARM_FAKE_*``/``SWARM_RUN_TEST_MODEL``
        # wins over an explicit "run for real", one-time, at the freeze point.
        dry_run = runtime.dry_run_forced_by_env()
    started = time.monotonic()

    try:
        entry = next(node for node in graph.nodes if node.id == graph.entry_node_id)
        value = coerce_input(input_value, entry.io.input_type)

        compiled = False
        if project_is_stale(project_dir, graph):
            if compile_if_stale is None:
                raise ProjectStaleError(
                    "the compiled project is missing or older than the graph; compile first"
                )
            job.append_event("run", {"status": RUN_STATUS_COMPILING})
            await compile_if_stale()
            compiled = True

        job.append_event("run", {"status": RUN_STATUS_STARTING, "compiled": compiled})
        state, exit_code = await _execute(
            job,
            project_dir=project_dir,
            input_value=value,
            uv_cache_dir=resolved_uv_cache_dir,
            timeout_s=timeout_s,
            extra_env=extra_env,
            dry_run=dry_run,
        )
        if state.result is None or exit_code != 0:
            raise RunError(_failure_message(state, exit_code))

        raw_state = state.result.get("state")
        duration = state.result.get("durationMs")
        outcome = RunOutcome(
            output=state.result.get("output"),
            state=raw_state if isinstance(raw_state, dict) else {},
            duration_ms=(
                duration if isinstance(duration, int) else int((time.monotonic() - started) * 1000)
            ),
            model=state.model,
            compiled=compiled,
            finished_at=datetime.now(UTC),
            dry_run=dry_run,
        )
        payload = _done_payload(outcome)
        job.append_event("done", payload)
        registry.mark_succeeded(run_id, payload)
        return outcome
    except asyncio.CancelledError:
        if job.is_live():
            job.mark_cancelled()
        raise
    except Exception as exc:
        # A compile-first failure has already appended `error` and marked
        # the job failed (run_compile owns its own terminal handling); only
        # a failure that reached here with the job still live is ours.
        if job.is_live():
            job.append_event(
                "error",
                {
                    "code": ERROR_CODE_RUN_FAILED,
                    "message": str(exc),
                    "exception": type(exc).__name__,
                },
            )
            registry.mark_failed(run_id, exc)
        raise


__all__ = [
    "ERROR_CODE_RUN_FAILED",
    "GRAPH_MODULE_RELATIVE_PATH",
    "NODE_STATUS_FAILED",
    "NODE_STATUS_STARTED",
    "NODE_STATUS_SUCCEEDED",
    "RUN_STATUS_COMPILING",
    "RUN_STATUS_STARTED",
    "RUN_STATUS_STARTING",
    "RUN_TIMEOUT_SECONDS",
    "STREAM_RUN_RELATIVE_PATH",
    "CompileFirst",
    "ProjectStaleError",
    "RunError",
    "RunInputError",
    "RunOutcome",
    "coerce_input",
    "project_is_stale",
    "run_project",
]
