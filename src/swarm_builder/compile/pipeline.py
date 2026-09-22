"""The five-phase compile orchestrator (PLAN.md "The compile pipeline").

This module owns phase ORDER, the retry policy, job lifecycle
transitions, and the SSE event stream's payload shapes. It owns no
generation logic of its own: phases 1, 2, 4 and 5 are deterministic
modules (``review.py``/``scaffold.py``/``boundary.py``/``validate.py``)
and phase 3 is a narrow injectable seam, so ``compile/agent.py`` (Group
5) can be dropped in without either module changing.

Phase order, exactly:

1. **Review** (deterministic). ``review()`` findings are emitted as
   events. Any error refuses the compile *before anything is written to
   disk* -- the project directory is not created, cleared, or touched.
   Warnings become ``warning`` events and never block. Route resolution
   (``resolve_effective_model`` -> ``to_resolved_model``) is part of
   this phase, because it is the same "refuse before scaffolding"
   decision and PLAN.md's failure-mode table places an unmappable route
   there ("refuse at Phase 1 naming the route, before any
   scaffolding").
2. **Scaffold** (deterministic), immediately followed by
   ``capture_baseline()`` -- before any fill can run, so the baseline
   describes Phase 2's own output rather than a partially-filled
   project.
3. **Fill** (the only phase that calls a model), bounded by
   :data:`~swarm_builder.compile.jobs.FILL_TIMEOUT_SECONDS` so a wedged
   model cannot hang a compile forever.
4. **Boundary check** (deterministic).
5. **Validate** (deterministic, subprocess-driven).

**Retry policy.** Phase 3 is retried at most once, and only when phase
4 or phase 5 fails, with the failure text handed to the retry as
``previous_failure``. A second failure is reported, not retried, so a
permanently-failing fill produces exactly two fill attempts.

**Cancellation.** The pipeline runs as the job's own ``asyncio.Task``,
so :meth:`~swarm_builder.compile.jobs.JobRegistry.cancel` interrupts it
by cancelling that task. No handler here catches
``asyncio.CancelledError`` -- cancellation is deliberately *not* an
error path -- and the job ends ``cancelled``.

**A failed compile leaves the project on disk** (``validate.py`` and
``scaffold.py`` never clean up), per PLAN.md's "keep the project for
inspection" response to every failure mode.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from swarm_builder.compile import ResolvedModel
from swarm_builder.compile.boundary import (
    BoundaryViolationError,
    ProjectBaseline,
    capture_baseline,
    check_boundary,
)
from swarm_builder.compile.fake_fill import apply_fake_fill
from swarm_builder.compile.jobs import FILL_TIMEOUT_SECONDS, Job, JobRegistry
from swarm_builder.compile.langgraph import (
    COMPILE_TARGETS,
    LANGGRAPH_PHASE_NAMES,
    TARGET_LANGGRAPH,
    TARGET_PYDANTIC_GRAPH,
)
from swarm_builder.compile.review import Finding, review
from swarm_builder.compile.scaffold import ScaffoldResult, scaffold
from swarm_builder.compile.validate import ValidationResult, validate_project
from swarm_builder.config import get_uv_cache_dir
from swarm_builder.inherit.routes import (
    LiveModel,
    UnmappableRouteError,
    build_live_model,
    is_known_model_name,
    to_resolved_model,
)
from swarm_builder.inherit.settings import EffectiveModel, RouteConfig, resolve_effective_model
from swarm_builder.models import SwarmGraph

# ---------------------------------------------------------------------------
# Event vocabulary
# ---------------------------------------------------------------------------

#: The five phase slugs, in execution order. ``index`` in a ``phase``
#: event payload is this tuple's 1-based position, so the frontend never
#: has to hardcode an ordering.
PHASE_REVIEW = "review"
PHASE_SCAFFOLD = "scaffold"
PHASE_FILL = "fill"
PHASE_BOUNDARY = "boundary"
PHASE_VALIDATE = "validate"

PHASE_NAMES: tuple[str, ...] = (
    PHASE_REVIEW,
    PHASE_SCAFFOLD,
    PHASE_FILL,
    PHASE_BOUNDARY,
    PHASE_VALIDATE,
)

#: The LangGraph target's four extra phases follow the five standard ones,
#: so their 1-based ``index`` continues the numbering and a client renders
#: them after ``validate``.
ALL_PHASE_NAMES: tuple[str, ...] = (*PHASE_NAMES, *LANGGRAPH_PHASE_NAMES)

PHASE_INDEX: dict[str, int] = {
    name: position for position, name in enumerate(ALL_PHASE_NAMES, start=1)
}

#: ``phase`` event ``status`` values. A phase emits exactly one
#: ``started`` and then either one ``succeeded`` or one ``failed`` --
#: never both, and never a second ``started`` without a terminal status
#: in between. A retried phase 3 therefore shows up as a second
#: ``started``, which is how the UI renders "retrying".
PHASE_STATUS_STARTED = "started"
PHASE_STATUS_SUCCEEDED = "succeeded"
PHASE_STATUS_FAILED = "failed"

#: Environment variable selecting the deterministic stub fill (PLAN.md
#: "SWARM_FAKE_FILL=1"). Read per compile, never cached, so a caller can
#: flip it in-process.
FAKE_FILL_ENV_VAR = "SWARM_FAKE_FILL"

#: The only value of :data:`FAKE_FILL_ENV_VAR` that enables the stub.
FAKE_FILL_ENABLED_VALUE = "1"

#: The ``error`` event's code. Every compile failure -- a refusing phase,
#: an unavailable filler, an unexpected exception -- carries this one
#: code; the failing phase is named by the preceding ``phase`` event's
#: ``failed`` status, and the reason is in the message. A client
#: therefore reads the phase list to learn *where* it failed and the
#: error event to learn *why*.
ERROR_CODE_PHASE_FAILED = "phase_failed"


# ---------------------------------------------------------------------------
# Result and seam shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FillResult:
    """What one Phase-3 fill attempt wrote.

    Attributes:
        filled_node_ids: The nodes whose marker regions were filled.
            Empty when a filler does not track this -- the pipeline
            reports it but never requires it.
    """

    filled_node_ids: tuple[str, ...] = ()


class Filler(Protocol):
    """Phase 3's injectable seam.

    The pipeline defines the contract and supplies every argument;
    ``compile/agent.py`` (Group 5) implements it for the real model
    path and :func:`fake_filler` backs the ``SWARM_FAKE_FILL=1`` path.
    An implementation mutates only ``project_dir``, and only inside the
    marker regions -- Phase 4 independently verifies that afterwards.

    Contract for implementors:

    - ``project_dir`` is a fully scaffolded project; write only inside
      the ``# --- swarm:imports/begin/end <nodeId> ---`` regions.
    - ``graph`` is the canvas document the project was scaffolded from.
    - ``resolved_model`` describes the model the *generated project*
      defaults to (pre-rendered source fragments, see
      :class:`~swarm_builder.compile.ResolvedModel`).
    - ``live_model`` is the object the *compile agent* itself should
      run on: a :class:`~swarm_builder.inherit.routes.LiveModel` whose
      ``.model`` field is what ``pydantic_ai.Agent(...)`` takes, or
      ``None`` when the selected filler needs no model at all.
    - ``previous_failure`` is ``None`` on the first attempt and the
      phase-4/5 failure text on the single retry.
    - Returning ``None`` is allowed and means "filled, ids not
      tracked"; returning a :class:`FillResult` lets the compile panel
      name the nodes that were filled.
    - Raise on failure. A raised exception fails the compile directly
      (it does NOT trigger the phase-4/5 retry, which reacts only to a
      boundary or validation failure). ``asyncio.CancelledError`` is
      never swallowed.
    """

    async def __call__(
        self,
        *,
        project_dir: Path,
        graph: SwarmGraph,
        resolved_model: ResolvedModel,
        live_model: object | None,
        previous_failure: str | None,
    ) -> FillResult | None: ...


class FillUnavailableError(RuntimeError):
    """Raised when no Phase-3 filler is available.

    Only reachable with ``SWARM_FAKE_FILL`` unset and no ``filler``
    injected, i.e. before ``compile/agent.py`` (Group 5) lands. A
    dedicated type so the route layer and the tests can distinguish "the
    model path is not built yet" from an actual fill failure.
    """


async def fake_filler(
    *,
    project_dir: Path,
    graph: SwarmGraph,
    resolved_model: ResolvedModel,
    live_model: object | None,
    previous_failure: str | None,
) -> FillResult:
    """Deterministic Phase-3 stand-in for ``SWARM_FAKE_FILL=1``.

    Thin :class:`Filler`-shaped adapter over
    :func:`~swarm_builder.compile.fake_fill.apply_fake_fill`, which does
    the work. It writes through the same marker-region splice the real
    agent's ``write_region`` tool performs, so a fake-fill compile
    exercises Phase 4 for real rather than bypassing it.

    Args:
        project_dir: The scaffolded project to fill.
        graph: The document the project was scaffolded from.
        resolved_model: Unused -- harness compatibility with
            :class:`Filler`.
        live_model: Unused -- the stub needs no model.
        previous_failure: Unused -- the stub is deterministic, so its
            output does not depend on why a previous attempt failed.

    Returns:
        The ids of the nodes whose bodies were filled.
    """
    del resolved_model, live_model, previous_failure
    return FillResult(filled_node_ids=apply_fake_fill(project_dir, graph))


@dataclass(frozen=True)
class CompileOutcome:
    """Everything a finished compile reports, and the ``done`` payload.

    Attributes:
        project_dir: The project directory left on disk.
        run_command: The copy-pasteable command that runs the generated
            project's validation gate.
        diagram: Phase 2's ``graph.render()`` golden diagram, for the
            "show the rendered diagram" step of the compile panel.
        filled_node_ids: Node ids Phase 3 filled, in graph order.
        attempts: How many Phase-3 fill attempts ran (1 or 2).
        model: The resolved model this compile spent, for the
            provenance line a UI must show so a compile never silently
            spends credentials on an unexpected route.
    """

    project_dir: Path
    run_command: str
    diagram: str
    filled_node_ids: tuple[str, ...]
    attempts: int
    model: EffectiveModel
    #: Present only for ``target="langgraph"``: phases 6-9's result.
    langgraph: object | None = None


@dataclass(frozen=True)
class _PhaseFailure:
    """One failed phase, in the shape both the event stream and the
    retry prompt need.

    Attributes:
        phase: The phase slug that failed.
        message: Human-readable reason, including the underlying detail
            (a validation stderr tail, a scaffold OSError, ...).
        details: JSON-serializable extra detail for the SSE payload
            (violations, review findings, failing step).
        retryable: Whether this is a phase-4/5 failure, i.e. the only
            kind the retry policy reacts to.
    """

    phase: str
    message: str
    details: dict[str, object]
    retryable: bool = False


class PhaseFailureError(Exception):
    """Aborts the compile at the phase that failed.

    Carries the same :class:`_PhaseFailure` the failing phase already
    emitted as its terminal ``phase`` event, so the pipeline's single
    error handler can append an ``error`` event without re-deriving why.
    """

    def __init__(self, failure: _PhaseFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


@dataclass
class _AttemptState:
    """Mutable per-attempt bookkeeping for the phase-3/4/5 loop."""

    attempt: int = 0
    previous_failure: str | None = None
    filled_node_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


def _phase_total(job: Job) -> int:
    """How many phases this job runs: five, or nine for a LangGraph compile.

    Read off the job (``routes/compile.py`` records the target there) so
    every phase frame of one compile reports the same ``total``.
    """
    target = getattr(job, "target", TARGET_PYDANTIC_GRAPH)
    return len(ALL_PHASE_NAMES) if target == TARGET_LANGGRAPH else len(PHASE_NAMES)


def _emit_phase_started(job: Job, phase: str) -> None:
    """Emit the ``started`` phase event for ``phase``."""
    job.append_event(
        "phase",
        {
            "name": phase,
            "index": PHASE_INDEX[phase],
            "total": _phase_total(job),
            "status": PHASE_STATUS_STARTED,
        },
    )


def _emit_phase_succeeded(job: Job, phase: str, details: dict[str, object] | None = None) -> None:
    """Emit the ``succeeded`` phase event for ``phase``.

    Args:
        job: The job to append to.
        phase: The phase slug that just succeeded.
        details: Optional phase-specific payload fields merged into the
            event body. Must never contain a ``name``/``index``/
            ``total``/``status`` key -- those are the event's own.
    """
    payload: dict[str, object] = {
        "name": phase,
        "index": PHASE_INDEX[phase],
        "total": _phase_total(job),
        "status": PHASE_STATUS_SUCCEEDED,
    }
    if details:
        payload.update(details)
    job.append_event("phase", payload)


def _emit_phase_failed(job: Job, failure: _PhaseFailure) -> None:
    """Emit the ``failed`` phase event for a phase that aborted."""
    job.append_event(
        "phase",
        {
            "name": failure.phase,
            "index": PHASE_INDEX[failure.phase],
            "total": _phase_total(job),
            "status": PHASE_STATUS_FAILED,
            "message": failure.message,
            "details": failure.details,
        },
    )


def _emit_log(job: Job, message: str) -> None:
    """Append one ``log`` line to the job's live log tail."""
    job.append_event("log", {"message": message})


def _emit_warning(job: Job, finding: Finding) -> None:
    """Append one non-blocking ``warning``."""
    job.append_event(
        "warning",
        {
            "code": finding.code,
            "message": finding.message,
            "nodeIds": list(finding.node_ids),
        },
    )


def _emit_review_error(job: Job, finding: Finding) -> None:
    """Append one Phase-1 *error* finding as a ``log`` event.

    The SSE vocabulary has no per-finding error type
    (``phase``/``log``/``warning``/``done``/``error``), so an individual
    error finding rides a ``log`` event carrying the same
    ``code``/``message``/``nodeIds`` shape a ``warning`` uses, plus
    ``severity``. The phase's own terminal ``phase`` event carries the
    structured list of every error.
    """
    job.append_event(
        "log",
        {
            "message": finding.message,
            "code": finding.code,
            "nodeIds": list(finding.node_ids),
            "severity": "error",
        },
    )


def _emit_error(job: Job, code: str, message: str, exception: BaseException) -> None:
    """Append the terminal ``error`` event.

    Args:
        job: The job to append to.
        message: What went wrong, in the failing module's own words.
        exception: The exception that aborted the compile, so clients
            can show its type without the pipeline leaking a traceback
            into the event stream.
    """
    job.append_event(
        "error",
        {
            "code": code,
            "message": message,
            "exception": type(exception).__name__,
        },
    )


def _run_command(project_dir: Path, uv_cache_dir: Path) -> str:
    """Build the copy-pasteable command that runs the generated project.

    Kept byte-identical to ``routes/export.py``'s ``runCommand`` (both
    are the same user-facing instruction) -- ``UV_CACHE_DIR`` is spelled
    out because the generated project must not depend on the reader's
    shell having it set (fact 10).
    """
    return (
        f"cd {project_dir} && "
        f"UV_CACHE_DIR={uv_cache_dir} uv sync && "
        f"UV_CACHE_DIR={uv_cache_dir} uv run python validate/dry_run.py"
    )


def _outcome_payload(outcome: CompileOutcome) -> dict[str, object]:
    """The terminal ``done`` payload, JSON-serializable end to end.

    Also the job's registry-recorded success value, so a client that
    reconnects to an already-finished job (via the status snapshot)
    gets exactly the same fields the ``done`` event carried.

    Args:
        outcome: The finished compile.

    Returns:
        The payload dict.
    """
    payload: dict[str, object] = {
        "projectPath": str(outcome.project_dir),
        "runCommand": outcome.run_command,
        "diagram": outcome.diagram,
        "filledNodeIds": list(outcome.filled_node_ids),
        "attempts": outcome.attempts,
        "model": {
            "provider": outcome.model.provider,
            "model": outcome.model.model,
            "source": outcome.model.source,
        },
    }
    lg = outcome.langgraph
    if lg is not None:
        payload["langgraph"] = {
            "projectPath": str(lg.project_dir),
            "runCommand": lg.run_command,
            "diagram": lg.diagram,
            "convertedNodeIds": list(lg.converted_node_ids),
            "attempts": lg.attempts,
        }
    return payload


# ---------------------------------------------------------------------------
# Phase 1: review + route resolution
# ---------------------------------------------------------------------------


def _resolve_route(graph: SwarmGraph, dsh_home: Path) -> tuple[EffectiveModel, ResolvedModel]:
    """Resolve the model this compile will spend, the production way.

    Never uses :func:`~swarm_builder.compile.default_scaffold_model` --
    that is a test fixture whose hard-coded known-name string would
    silently route a compile to a model the user never configured (I5).

    Args:
        graph: The document being compiled; its own ``model`` override,
            when set, wins over every settings/env fallback.
        dsh_home: The harness settings home to read ``settings.yaml``
            from. Absent is a normal state, not an error.

    Returns:
        The effective selection (carrying its ``source`` and matched
        ``route``, both needed by Phase 5's extras assertion) and the
        :class:`ResolvedModel` Phase 2 splices into the project.

    Raises:
        UnmappableRouteError: If the resolved route has no PydanticAI
            counterpart (fact 23).
    """
    override = None
    if graph.model is not None:
        override = (graph.model.provider, graph.model.model, graph.model.reasoning_effort)

    effective = resolve_effective_model(dsh_home, override)
    return effective, to_resolved_model(effective)



def _warn_if_model_name_unknown(
    job: Job, effective: EffectiveModel, resolved_model: ResolvedModel
) -> None:
    """Warn when the emitted default names a model PydanticAI does not know.

    Only the known-name emission path can be checked: a structural
    custom-``baseURL`` route names a model on someone else's endpoint, so
    membership in PydanticAI's union says nothing about it.

    This is a warning, not an error, for two reasons: the union is pinned
    to the installed ``pydantic-ai``, so a genuinely newer provider model
    id must not be refused; and the compile itself still succeeds, because
    agents are built with ``defer_model_check=True`` and the dry run
    injects ``TestModel``. Without the warning the mismatch is invisible
    until someone runs the exported project with real credentials.

    Args:
        job: The job to append the warning to.
        effective: The resolved selection.
        resolved_model: The emission the scaffolder will use.
    """
    for line in resolved_model.env_lines:
        if not line.startswith("SWARM_MODEL="):
            continue
        emitted = line.removeprefix("SWARM_MODEL=")
        # A structural route emits a bare id plus SWARM_BASE_URL; only a
        # prefixed known-name emission is checkable.
        if ":" not in emitted or any(
            other.startswith("SWARM_BASE_URL=") for other in resolved_model.env_lines
        ):
            return
        if not is_known_model_name(emitted):
            _emit_warning(
                job,
                Finding(
                    code="unknown_model_name",
                    message=(
                        f"the inherited default {emitted!r} is not a model name this "
                        "pydantic-ai knows, so the exported project will fail when run "
                        "with real credentials even though the keyless gate passes; "
                        f"check the {effective.provider!r} model id in your harness "
                        "settings"
                    ),
                    node_ids=(),
                ),
            )
        return


def _run_review_phase(
    job: Job, graph: SwarmGraph, dsh_home: Path
) -> tuple[EffectiveModel, ResolvedModel]:
    """Run Phase 1: review the graph and resolve the route it will spend.

    Route resolution is refused here rather than in its own phase
    because PLAN.md's failure-mode table places an unmappable route at
    Phase 1, "before any scaffolding" -- the same guarantee the review
    errors carry. Both are reported under Phase 1's one phase event.

    Args:
        job: The job to append events to.
        graph: The document being compiled.
        dsh_home: Harness settings home for route resolution.

    Returns:
        The effective selection and the resolved model.

    Raises:
        PhaseFailureError: If the review reported errors, or the route
            has no PydanticAI counterpart. Nothing has been written to
            disk at this point.
    """
    _emit_phase_started(job, PHASE_REVIEW)
    result = review(graph)
    for finding in result.errors:
        _emit_review_error(job, finding)
    for warning in result.warnings:
        _emit_warning(job, warning)

    if result.errors:
        failure = _PhaseFailure(
            phase=PHASE_REVIEW,
            message=f"review found {len(result.errors)} error(s); refusing to scaffold",
            details={
                "errors": [
                    {"code": f.code, "message": f.message, "nodeIds": list(f.node_ids)}
                    for f in result.errors
                ]
            },
        )
        _emit_phase_failed(job, failure)
        raise PhaseFailureError(failure)

    try:
        effective, resolved_model = _resolve_route(graph, dsh_home)
    except UnmappableRouteError as exc:
        failure = _PhaseFailure(
            phase=PHASE_REVIEW,
            message=str(exc),
            details={"errorType": type(exc).__name__, "provider": exc.provider},
        )
        _emit_phase_failed(job, failure)
        raise PhaseFailureError(failure) from exc

    _emit_log(
        job,
        f"model: {effective.provider}:{effective.model} (source={effective.source})",
    )
    _warn_if_model_name_unknown(job, effective, resolved_model)
    _emit_phase_succeeded(
        job,
        PHASE_REVIEW,
        {
            "errorCount": 0,
            "warningCount": len(result.warnings),
            "route": {
                "provider": effective.provider,
                "model": effective.model,
                "source": effective.source,
            },
        },
    )
    return effective, resolved_model


# ---------------------------------------------------------------------------
# Phase 2: scaffold + baseline
# ---------------------------------------------------------------------------


def _run_scaffold_phase(
    job: Job,
    graph: SwarmGraph,
    project_dir: Path,
    resolved_model: ResolvedModel,
) -> tuple[ScaffoldResult, ProjectBaseline]:
    """Run Phase 2 and capture the boundary baseline.

    The baseline is captured here, immediately after scaffolding and
    before any fill, so it describes Phase 2's own output -- the only
    moment at which that is true.

    A recompile rescaffolds from scratch rather than layering new files
    over a previous compile's, per the "rescaffold everything; v1 does
    not merge prior edits" edge case. The clear happens HERE, after
    Phase 1 has passed, so a refused compile never creates or destroys a
    project directory.

    Args:
        job: The job to append events to.
        graph: The document to scaffold from.
        project_dir: The project directory to create/replace.
        resolved_model: The resolved route, spliced into the project.

    Returns:
        Phase 2's result (including the golden diagram) and the baseline
        Phase 4 compares against.
    """
    _emit_phase_started(job, PHASE_SCAFFOLD)

    if project_dir.exists():
        _emit_log(job, f"recompile: clearing existing project at {project_dir}")
        _clear_project_dir(project_dir)

    result = scaffold(graph, project_dir, resolved_model)
    baseline = capture_baseline(project_dir)

    _emit_phase_succeeded(
        job,
        PHASE_SCAFFOLD,
        {"projectPath": str(project_dir), "fileCount": len(result.written_paths)},
    )
    return result, baseline


def _clear_project_dir(project_dir: Path) -> None:
    """Remove every existing entry inside ``project_dir``.

    The directory itself is recreated empty rather than deleted,
    matching :func:`~swarm_builder.store.projects.clear_project_dir`'s
    contract: any failure after this point must leave the project on
    disk for inspection, so the directory must exist before scaffolding
    begins.

    Args:
        project_dir: An existing directory to empty.
    """
    for entry in sorted(project_dir.iterdir()):
        _remove_entry(entry)


def _remove_entry(path: Path) -> None:
    """Delete one file, symlink, or directory tree.

    A symlinked directory is unlinked, never followed -- this runs on a
    user-configured workspace path and must not be talked into removing
    something outside it by a symlink a previous compile left behind.
    """
    if path.is_dir() and not path.is_symlink():
        for child in sorted(path.iterdir()):
            _remove_entry(child)
        path.rmdir()
    else:
        path.unlink()


# ---------------------------------------------------------------------------
# Phase 3: fill
# ---------------------------------------------------------------------------


def _fake_fill_enabled() -> bool:
    """Whether ``SWARM_FAKE_FILL=1`` selects the deterministic stub fill."""
    return os.environ.get(FAKE_FILL_ENV_VAR) == FAKE_FILL_ENABLED_VALUE


def _select_filler(real_filler: Filler | None) -> Filler:
    """Pick the Phase-3 filler for this compile.

    Args:
        real_filler: The model-backed filler the caller injected, or
            ``None`` before ``compile/agent.py`` exists.

    Returns:
        :func:`fake_filler` when ``SWARM_FAKE_FILL=1``, else
        ``real_filler``.

    Raises:
        FillUnavailableError: If neither is available. The message names
            both ways out, because this is a configuration seam a user
            can actually hit today.
    """
    if _fake_fill_enabled():
        return fake_filler
    if real_filler is None:
        raise FillUnavailableError(
            "no Phase 3 fill implementation is available: the model-backed filler "
            "(compile/agent.py) is not wired yet and no filler was injected. Set "
            f"{FAKE_FILL_ENV_VAR}={FAKE_FILL_ENABLED_VALUE} to compile with the "
            "deterministic stub fill, or inject a Filler."
        )
    return real_filler


async def _invoke_filler(
    filler: Filler,
    *,
    project_dir: Path,
    graph: SwarmGraph,
    resolved_model: ResolvedModel,
    live_model: object | None,
    previous_failure: str | None,
) -> tuple[str, ...]:
    """Call one :class:`Filler`, normalizing ``None`` to an empty id tuple.

    The whole call is bounded by
    :data:`~swarm_builder.compile.jobs.FILL_TIMEOUT_SECONDS` so a wedged
    model cannot hang a compile forever.

    Args:
        filler: The filler to invoke.
        project_dir: The scaffolded project to fill.
        graph: The document the project was scaffolded from.
        resolved_model: The generated project's default model source.
        live_model: The compile agent's own model, or ``None``.
        previous_failure: The prior phase-4/5 failure text, or ``None``.

    Returns:
        The node ids the filler reported, or ``()`` if it reported none.

    Raises:
        TimeoutError: If the fill exceeds the fill timeout.
        Exception: Whatever the filler itself raises, unmodified.
    """
    async with asyncio.timeout(FILL_TIMEOUT_SECONDS):
        result = await filler(
            project_dir=project_dir,
            graph=graph,
            resolved_model=resolved_model,
            live_model=live_model,
            previous_failure=previous_failure,
        )
    return result.filled_node_ids if result is not None else ()


async def _run_fill_once(
    job: Job,
    filler: Filler | None,
    *,
    graph: SwarmGraph,
    project_dir: Path,
    resolved_model: ResolvedModel,
    effective: EffectiveModel,
    state: _AttemptState,
) -> None:
    """Run Phase 3 once, emitting its terminal event.

    The filler is selected before the live model is built, so the
    fake-fill path never constructs (or imports) a provider model it
    will not use.

    Args:
        job: The job to append events to.
        filler: The caller-injected real filler, or ``None``.
        graph: The document being compiled.
        project_dir: The scaffolded project to fill.
        resolved_model: The generated project's default model source.
        effective: The resolved selection the compile spends.
        state: Per-attempt bookkeeping, updated in place.

    Raises:
        PhaseFailureError: If no filler is available, or the fill itself
            fails or overruns its timeout.
    """
    state.attempt += 1
    _emit_phase_started(job, PHASE_FILL)
    if state.attempt > 1:
        _emit_log(job, f"fill attempt {state.attempt} (one retry, previous failure appended)")

    try:
        selected = _select_filler(filler)
    except FillUnavailableError as exc:
        failure = _PhaseFailure(
            phase=PHASE_FILL,
            message=str(exc),
            details={"errorType": type(exc).__name__, "hint": f"{FAKE_FILL_ENV_VAR}=1"},
        )
        _emit_phase_failed(job, failure)
        raise PhaseFailureError(failure) from exc

    live_model: LiveModel | None = None
    if selected is not fake_filler:
        live_model = build_live_model(effective)
        _emit_log(job, f"fill model: {live_model.source_description}")

    try:
        filled = await _invoke_filler(
            selected,
            project_dir=project_dir,
            graph=graph,
            resolved_model=resolved_model,
            live_model=live_model,
            previous_failure=state.previous_failure,
        )
    except TimeoutError as exc:
        failure = _PhaseFailure(
            phase=PHASE_FILL,
            message=(
                f"fill did not finish within {FILL_TIMEOUT_SECONDS:g}s "
                "(a wedged model cannot hang a compile indefinitely)"
            ),
            details={"errorType": type(exc).__name__},
        )
        _emit_phase_failed(job, failure)
        raise PhaseFailureError(failure) from exc
    except Exception as exc:
        failure = _PhaseFailure(
            phase=PHASE_FILL,
            message=str(exc),
            details={"errorType": type(exc).__name__},
        )
        _emit_phase_failed(job, failure)
        raise PhaseFailureError(failure) from exc

    state.filled_node_ids = filled
    _emit_phase_succeeded(
        job,
        PHASE_FILL,
        {"attempt": state.attempt, "filledNodeIds": list(filled)},
    )


# ---------------------------------------------------------------------------
# Phases 4 and 5: boundary check, then validation
# ---------------------------------------------------------------------------


def _boundary_failure(
    job: Job, project_dir: Path, baseline: ProjectBaseline
) -> _PhaseFailure | None:
    """Run Phase 4, reporting (never raising) a violation.

    Returning the failure instead of raising keeps the retry decision in
    one place: the caller needs the outcome, not an exception.

    Args:
        job: The job to append events to.
        project_dir: The project to check.
        baseline: The Phase-2 snapshot to compare against.

    Returns:
        ``None`` when the project is clean, otherwise the failure --
        which has already been emitted as the phase's terminal event.
    """
    _emit_phase_started(job, PHASE_BOUNDARY)
    try:
        check_boundary(project_dir, baseline)
    except BoundaryViolationError as exc:
        failure = _PhaseFailure(
            phase=PHASE_BOUNDARY,
            message=str(exc),
            details={
                "violations": [
                    {"code": v.code, "message": v.message, "path": v.path}
                    for v in exc.result.violations
                ]
            },
            retryable=True,
        )
        _emit_phase_failed(job, failure)
        return failure

    _emit_phase_succeeded(job, PHASE_BOUNDARY)
    return None


def _validation_failure(
    job: Job,
    project_dir: Path,
    route: RouteConfig | None,
    resolved_model: ResolvedModel,
    uv_cache_dir: Path,
) -> tuple[ValidationResult | None, _PhaseFailure | None]:
    """Run Phase 5, reporting (never raising) a validation failure.

    Args:
        job: The job to append events to.
        project_dir: The project to validate.
        route: The matched ``RouteConfig``, so the fact-30 extras
            assertion checks the extras this route requires.
        resolved_model: The same resolved model Phase 2 scaffolded
            with -- never re-resolved here.
        uv_cache_dir: Explicit ``UV_CACHE_DIR`` for every ``uv`` step
            (fact 10).

    Returns:
        ``(result, None)`` on success, ``(None, failure)`` otherwise.
    """
    _emit_phase_started(job, PHASE_VALIDATE)
    try:
        result = validate_project(
            project_dir,
            route=route,
            resolved_model=resolved_model,
            uv_cache_dir=uv_cache_dir,
        )
    except Exception as exc:
        failure = _PhaseFailure(
            phase=PHASE_VALIDATE,
            message=str(exc),
            details=_validation_failure_details(exc),
            retryable=True,
        )
        _emit_phase_failed(job, failure)
        return None, failure

    _emit_phase_succeeded(job, PHASE_VALIDATE, {"steps": [step.step for step in result.steps]})
    return result, None


def _validation_failure_details(exc: Exception) -> dict[str, object]:
    """Extract a JSON-serializable summary from a ``validate.py`` error.

    ``validate.py`` puts the useful part -- which step failed, and the
    tail of its stderr -- in the exception's *message*, and hangs the
    full structured result off attributes. Both are surfaced, but the
    captured stdout/stderr stay out of the event payload: an SSE frame
    is not the place for a whole ``uv sync`` log, and a reconnecting
    client replaying the retained log should not have to carry it. The
    message already quotes the stderr tail.

    Args:
        exc: The exception raised by :func:`validate_project`.

    Returns:
        A payload dict with ``errorType`` and, when available, the
        failing step's name, exit code, and command.
    """
    details: dict[str, object] = {"errorType": type(exc).__name__}
    failed_step = getattr(exc, "failed_step", None)
    if failed_step is not None:
        details["step"] = failed_step.step
        details["returncode"] = failed_step.returncode
        details["command"] = list(failed_step.command)
    return details


# ---------------------------------------------------------------------------
# The phase-3/4/5 loop and its retry policy
# ---------------------------------------------------------------------------


def _record_retry(job: Job, state: _AttemptState, previous_failure: str) -> None:
    """Remember a phase-4/5 failure as the retry's ``previous_failure``.

    Only the failure text crosses into the retry: the next fill attempt
    gets the reason the previous one was rejected, which is what makes
    the single retry useful rather than a blind repeat.

    Args:
        job: The job to append events to.
        state: Per-attempt bookkeeping, updated in place.
        previous_failure: The failure text to hand to the next attempt.
    """
    state.previous_failure = previous_failure
    _emit_log(job, "retrying fill once with the failure appended")


async def _run_fill_boundary_validate(
    job: Job,
    filler: Filler | None,
    *,
    graph: SwarmGraph,
    project_dir: Path,
    baseline: ProjectBaseline,
    resolved_model: ResolvedModel,
    effective: EffectiveModel,
    uv_cache_dir: Path,
) -> tuple[ValidationResult, tuple[str, ...], int]:
    """Run phases 3 -> 4 -> 5, retrying phase 3 at most once.

    The retry policy lives in exactly one place: a phase-4 or phase-5
    failure earns one more fill attempt with the failure text appended;
    a second failure is reported rather than retried; a fill failure
    itself aborts immediately (it is not one of the two conditions the
    policy names).

    Args:
        job: The job to append events to.
        filler: The caller-injected real filler, or ``None``.
        graph: The document being compiled.
        project_dir: The scaffolded project to fill and validate.
        baseline: The Phase-2 baseline Phase 4 compares against.
        resolved_model: The generated project's default model source.
        effective: The resolved selection the compile spends.
        uv_cache_dir: Explicit ``UV_CACHE_DIR`` for every ``uv`` step.

    Returns:
        The successful validation result, the filled node ids, and the
        number of fill attempts made.

    Raises:
        PhaseFailureError: When the compile cannot be made to pass.
    """
    state = _AttemptState()
    is_retry = False

    while True:
        await _run_fill_once(
            job,
            filler,
            graph=graph,
            project_dir=project_dir,
            resolved_model=resolved_model,
            effective=effective,
            state=state,
        )

        failure = _boundary_failure(job, project_dir, baseline)
        if failure is None:
            result, failure = _validation_failure(
                job, project_dir, effective.route, resolved_model, uv_cache_dir
            )
            if failure is None:
                return result, state.filled_node_ids, state.attempt

        if is_retry:
            raise PhaseFailureError(failure)
        is_retry = True
        _record_retry(job, state, failure.message)


# ---------------------------------------------------------------------------
# The LangGraph target (phases 6-9), adapted to this module's emitters
# ---------------------------------------------------------------------------


async def _run_langgraph_target(
    job: Job,
    *,
    graph: SwarmGraph,
    source_project_dir: Path,
    project_dir: Path,
    effective: EffectiveModel,
    uv_cache_dir: Path,
    converter: object | None,
) -> object:
    """Run ``compile/langgraph``'s phases, translating its refusal into a
    :class:`PhaseFailureError` so the terminal handling below is shared."""
    # Imported here: compile/langgraph imports this module's FillResult, so a
    # module-level import would be circular.
    from swarm_builder.compile.langgraph.pipeline import (
        LangGraphPhaseError,
        _Emitters,
        run_langgraph_phases,
    )

    def failed(job_: Job, phase: str, message: str, details: dict[str, object]) -> None:
        _emit_phase_failed(job_, _PhaseFailure(phase=phase, message=message, details=details))

    emitters = _Emitters(
        started=_emit_phase_started,
        succeeded=_emit_phase_succeeded,
        failed=failed,
        log=_emit_log,
    )
    try:
        return await run_langgraph_phases(
            job,
            graph=graph,
            source_project_dir=source_project_dir,
            project_dir=project_dir,
            effective=effective,
            use_fake=_fake_fill_enabled(),
            uv_cache_dir=uv_cache_dir,
            emitters=emitters,
            converter=converter,  # type: ignore[arg-type]
        )
    except LangGraphPhaseError as exc:
        raise PhaseFailureError(
            _PhaseFailure(phase=exc.phase, message=str(exc), details=exc.details)
        ) from exc


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


async def run_compile(
    graph: SwarmGraph,
    *,
    registry: JobRegistry,
    compile_id: str,
    project_dir: Path,
    dsh_home: Path,
    filler: Filler | None = None,
    uv_cache_dir: Path | None = None,
    finalize: bool = True,
    target: str = TARGET_PYDANTIC_GRAPH,
    langgraph_project_dir: Path | None = None,
    langgraph_converter: object | None = None,
) -> CompileOutcome:
    """Run the five-phase compile for ``graph``, streaming progress into
    the job registered under ``compile_id``.

    Intended to run as the job's own asyncio task: the route layer calls
    ``registry.register(...)``, schedules this coroutine, and stores the
    task on ``job.task`` so :meth:`JobRegistry.cancel` can interrupt it.

    Args:
        graph: The document to compile.
        registry: The job registry holding this compile's job, which
            must already be registered and still live.
        compile_id: The id the job was registered under.
        project_dir: Where to write the generated project. A previous
            compile's contents are cleared after Phase 1 passes, so a
            recompile never layers new files over stale ones.
        dsh_home: Harness settings home for route resolution.
        filler: The model-backed Phase-3 filler, or ``None`` before
            ``compile/agent.py`` lands. Ignored when
            ``SWARM_FAKE_FILL=1``.
        uv_cache_dir: Explicit ``UV_CACHE_DIR`` for every ``uv`` step and
            for the reported run command. Defaults to
            :func:`swarm_builder.config.get_uv_cache_dir`, read at call
            time (never cached -- the environment is hot-reloadable).
        finalize: When ``True`` (the default, and the plain-compile
            path) a successful compile emits the terminal ``done`` event
            and marks the job ``succeeded``. ``compile/run.py`` passes
            ``False`` so a compile-then-run job stays live after the
            compile: the run that follows owns the terminal event.
            Failure and cancellation paths are unaffected -- a compile
            that refuses still ends the job, whatever follows it.
        target: ``"pydantic-graph"`` (the five phases) or ``"langgraph"``
            (the five phases, then ``compile/langgraph``'s four: the
            validated project is converted into a LangGraph export).
        langgraph_project_dir: Where the LangGraph export is written;
            required when ``target="langgraph"``.
        langgraph_converter: Test seam for phase 7 (replaces the agent).

    Returns:
        The :class:`CompileOutcome` describing the finished compile --
        also what the terminal ``done`` event carries.

    Raises:
        PhaseFailureError: Any phase refused the compile. The job is
            already marked ``failed`` and the project (if Phase 2 ran)
            is left on disk.
        asyncio.CancelledError: Re-raised unchanged when the job is
            cancelled; the job is marked ``cancelled`` first.
    """
    if target not in COMPILE_TARGETS:
        raise ValueError(f"unknown compile target {target!r}; expected one of {COMPILE_TARGETS}")
    if target == TARGET_LANGGRAPH and langgraph_project_dir is None:
        raise ValueError("target='langgraph' requires langgraph_project_dir")

    job = registry.get(compile_id)
    job.target = target  # type: ignore[attr-defined]
    if job.status == "queued":
        job.mark_running()

    resolved_uv_cache_dir = uv_cache_dir if uv_cache_dir is not None else get_uv_cache_dir()

    try:
        effective, resolved_model = _run_review_phase(job, graph, dsh_home)
        scaffold_result, baseline = _run_scaffold_phase(
            job, graph, project_dir, resolved_model
        )

        _emit_log(job, f"validating project at {project_dir}")
        validation_result, filled_node_ids, attempts = await _run_fill_boundary_validate(
            job,
            filler,
            graph=graph,
            project_dir=project_dir,
            baseline=baseline,
            resolved_model=resolved_model,
            effective=effective,
            uv_cache_dir=resolved_uv_cache_dir,
        )

        langgraph_outcome = None
        if target == TARGET_LANGGRAPH:
            assert langgraph_project_dir is not None
            langgraph_outcome = await _run_langgraph_target(
                job,
                graph=graph,
                source_project_dir=project_dir,
                project_dir=langgraph_project_dir,
                effective=effective,
                uv_cache_dir=resolved_uv_cache_dir,
                converter=langgraph_converter,
            )

        outcome = CompileOutcome(
            project_dir=project_dir,
            run_command=_run_command(project_dir, resolved_uv_cache_dir),
            diagram=scaffold_result.golden_render,
            filled_node_ids=filled_node_ids,
            attempts=attempts,
            model=effective,
            langgraph=langgraph_outcome,
        )
        if finalize:
            _emit_done(job, outcome, validation_result)
            registry.mark_succeeded(compile_id, _outcome_payload(outcome))
        else:
            _emit_log(job, "compile finished; project is up to date")
        return outcome
    except asyncio.CancelledError:
        # Cancellation is not a compile failure. Re-raise it unchanged --
        # a compile is one asyncio.Task, so cancelling that task IS the
        # whole cancel mechanism (fact 26) -- and only make sure the job
        # reflects it, in case the canceller did not (server shutdown
        # marks the job itself; a direct task cancel does not).
        if job.is_live():
            job.mark_cancelled()
        raise
    except PhaseFailureError as exc:
        _emit_error(job, ERROR_CODE_PHASE_FAILED, exc.failure.message, exc)
        registry.mark_failed(compile_id, exc)
        raise
    except Exception as exc:
        # A phase module raised something it did not wrap (a scaffold
        # bug, an unwritable workspace, a route-resolution failure).
        # Report it the same way rather than letting the task die with no
        # terminal event for the SSE client to close on.
        _emit_error(job, ERROR_CODE_PHASE_FAILED, str(exc), exc)
        registry.mark_failed(compile_id, exc)
        raise


def _emit_done(job: Job, outcome: CompileOutcome, validation_result: ValidationResult) -> None:
    """Append the terminal ``done`` event.

    Args:
        job: The job to append to.
        outcome: The finished compile.
        validation_result: Phase 5's report, used for the step summary a
            client shows in the result card.
    """
    payload = _outcome_payload(outcome)
    payload["validationSteps"] = [step.step for step in validation_result.steps]
    job.append_event("done", payload)


__all__ = [
    "ALL_PHASE_NAMES",
    "ERROR_CODE_PHASE_FAILED",
    "FAKE_FILL_ENABLED_VALUE",
    "FAKE_FILL_ENV_VAR",
    "PHASE_BOUNDARY",
    "PHASE_FILL",
    "PHASE_INDEX",
    "PHASE_NAMES",
    "PHASE_REVIEW",
    "PHASE_SCAFFOLD",
    "PHASE_STATUS_FAILED",
    "PHASE_STATUS_STARTED",
    "PHASE_STATUS_SUCCEEDED",
    "PHASE_VALIDATE",
    "CompileOutcome",
    "FillResult",
    "FillUnavailableError",
    "Filler",
    "PhaseFailureError",
    "fake_filler",
    "run_compile",
]
