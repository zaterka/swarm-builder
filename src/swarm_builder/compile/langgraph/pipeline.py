"""Phases 6-9 of a ``target="langgraph"`` compile.

Runs after the standard five phases have produced a validated
pydantic-graph project, and mirrors their shape: a deterministic scaffold
that captures the boundary baseline, one model-driven phase retried at
most once when the boundary or validation phase that follows it fails,
and the same event vocabulary (``phase``/``log``) on the same job.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from swarm_builder.compile.boundary import (
    BoundaryViolationError,
    ProjectBaseline,
    capture_baseline,
    check_boundary,
)
from swarm_builder.compile.jobs import FILL_TIMEOUT_SECONDS, Job
from swarm_builder.compile.langgraph import (
    KEYLESS_IMPORT_SNIPPET,
    LANGGRAPH_PHASE_NAMES,
    NODES_DIR_PARTS,
    PHASE_LG_BOUNDARY,
    PHASE_LG_CONVERT,
    PHASE_LG_SCAFFOLD,
    PHASE_LG_VALIDATE,
)
from swarm_builder.compile.langgraph.convert import convert, fake_convert
from swarm_builder.compile.langgraph.models import to_langchain_model_source
from swarm_builder.compile.langgraph.scaffold import scaffold_langgraph
from swarm_builder.compile.validate import ValidationResult, run_keyless_gate
from swarm_builder.inherit.routes import UnmappableRouteError, build_live_model
from swarm_builder.inherit.settings import EffectiveModel
from swarm_builder.models import SwarmGraph

#: Signature of the Phase-7 seam (the real agent or the fake).
Converter = Callable[..., Awaitable[object]]


@dataclass(frozen=True)
class LangGraphOutcome:
    """What phases 6-9 produced, for the ``done`` payload."""

    project_dir: Path
    run_command: str
    diagram: str
    converted_node_ids: tuple[str, ...]
    attempts: int


@dataclass
class _Emitters:
    """The pipeline's own event helpers, injected so this module never
    imports ``compile/pipeline.py`` (which imports this module)."""

    started: Callable[[Job, str], None]
    succeeded: Callable[[Job, str, dict[str, object] | None], None]
    failed: Callable[[Job, str, str, dict[str, object]], None]
    log: Callable[[Job, str], None]


class LangGraphPhaseError(Exception):
    """A phase 6-9 refused; carries the slug and the message for the
    pipeline's terminal error handling."""

    def __init__(self, phase: str, message: str, details: dict[str, object]) -> None:
        self.phase = phase
        self.details = details
        super().__init__(message)


def _run_command(project_dir: Path, uv_cache_dir: Path) -> str:
    return (
        f"cd {project_dir} && "
        f"UV_CACHE_DIR={uv_cache_dir} uv sync && "
        f"UV_CACHE_DIR={uv_cache_dir} uv run python validate/dry_run.py"
    )


def _clear_dir(project_dir: Path) -> None:
    for entry in sorted(project_dir.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            _clear_dir(entry)
            entry.rmdir()
        else:
            entry.unlink()


async def run_langgraph_phases(
    job: Job,
    *,
    graph: SwarmGraph,
    source_project_dir: Path,
    project_dir: Path,
    effective: EffectiveModel,
    use_fake: bool,
    uv_cache_dir: Path,
    emitters: _Emitters,
    converter: Converter | None = None,
) -> LangGraphOutcome:
    """Scaffold, convert (retry once), boundary-check and validate the export.

    Args:
        job: The compile job to stream events into.
        graph: The canvas document.
        source_project_dir: The validated pydantic-graph project.
        project_dir: Where the LangGraph project is written.
        effective: The resolved route (rendered for LangChain here).
        use_fake: ``SWARM_FAKE_FILL=1``: deterministic stub conversion.
        uv_cache_dir: Explicit ``UV_CACHE_DIR`` for the gate.
        emitters: The pipeline's phase/log event helpers.
        converter: Test seam replacing the real agent.

    Raises:
        LangGraphPhaseError: The phase that refused, with its message.
        asyncio.CancelledError: Propagated untouched.
    """
    # -- Phase 6: scaffold --------------------------------------------
    emitters.started(job, PHASE_LG_SCAFFOLD)
    try:
        model_source = to_langchain_model_source(effective)
    except UnmappableRouteError as exc:
        emitters.failed(job, PHASE_LG_SCAFFOLD, str(exc), {"errorType": type(exc).__name__})
        raise LangGraphPhaseError(PHASE_LG_SCAFFOLD, str(exc), {}) from exc
    if project_dir.exists():
        emitters.log(job, f"recompile: clearing existing LangGraph project at {project_dir}")
        _clear_dir(project_dir)
    try:
        scaffold_result = scaffold_langgraph(graph, project_dir, model_source)
        baseline: ProjectBaseline = capture_baseline(
            project_dir, permitted_dirs=("/".join(NODES_DIR_PARTS),)
        )
    except Exception as exc:
        emitters.failed(job, PHASE_LG_SCAFFOLD, str(exc), {"errorType": type(exc).__name__})
        raise LangGraphPhaseError(PHASE_LG_SCAFFOLD, str(exc), {}) from exc
    emitters.log(job, f"LangGraph model route: {model_source.description}")
    emitters.succeeded(
        job,
        PHASE_LG_SCAFFOLD,
        {"projectPath": str(project_dir), "fileCount": len(scaffold_result.written_paths)},
    )

    # -- Phases 7 -> 8 -> 9 with one retry --------------------------------
    selected: Converter = (
        converter if converter is not None else (fake_convert if use_fake else convert)
    )
    live_model = (
        None if use_fake and converter is None else _live_model_or_none(effective, use_fake)
    )
    attempts = 0
    previous_failure: str | None = None
    converted: tuple[str, ...] = ()
    while True:
        attempts += 1
        emitters.started(job, PHASE_LG_CONVERT)
        if attempts > 1:
            emitters.log(job, f"lg_convert attempt {attempts} (one retry, failure appended)")
        try:
            async with asyncio.timeout(FILL_TIMEOUT_SECONDS):
                result = await selected(
                    project_dir=project_dir,
                    source_dir=source_project_dir,
                    graph=graph,
                    live_model=live_model,
                    previous_failure=previous_failure,
                )
        except TimeoutError as exc:
            message = f"lg_convert did not finish within {FILL_TIMEOUT_SECONDS:g}s"
            emitters.failed(job, PHASE_LG_CONVERT, message, {"errorType": type(exc).__name__})
            raise LangGraphPhaseError(PHASE_LG_CONVERT, message, {}) from exc
        except Exception as exc:
            emitters.failed(job, PHASE_LG_CONVERT, str(exc), {"errorType": type(exc).__name__})
            raise LangGraphPhaseError(PHASE_LG_CONVERT, str(exc), {}) from exc
        converted = tuple(getattr(result, "filled_node_ids", ()) or ())
        emitters.succeeded(
            job, PHASE_LG_CONVERT, {"attempt": attempts, "convertedNodeIds": list(converted)}
        )

        failure = _boundary(job, project_dir, baseline, emitters)
        validation: ValidationResult | None = None
        if failure is None:
            validation, failure = _validate(job, project_dir, uv_cache_dir, emitters)
        if failure is None and validation is not None:
            return LangGraphOutcome(
                project_dir=project_dir,
                run_command=_run_command(project_dir, uv_cache_dir),
                diagram=scaffold_result.golden_mermaid,
                converted_node_ids=converted,
                attempts=attempts,
            )
        phase, message = failure  # type: ignore[misc]
        if attempts >= 2:
            raise LangGraphPhaseError(phase, message, {})
        previous_failure = message
        emitters.log(job, "retrying lg_convert once with the failure appended")


def _live_model_or_none(effective: EffectiveModel, use_fake: bool) -> object | None:
    if use_fake:
        return None
    return build_live_model(effective)


def _boundary(
    job: Job, project_dir: Path, baseline: ProjectBaseline, emitters: _Emitters
) -> tuple[str, str] | None:
    emitters.started(job, PHASE_LG_BOUNDARY)
    try:
        check_boundary(project_dir, baseline)
    except BoundaryViolationError as exc:
        emitters.failed(
            job,
            PHASE_LG_BOUNDARY,
            str(exc),
            {
                "violations": [
                    {"code": v.code, "message": v.message, "path": v.path}
                    for v in exc.result.violations
                ]
            },
        )
        return PHASE_LG_BOUNDARY, str(exc)
    emitters.succeeded(job, PHASE_LG_BOUNDARY, None)
    return None


def _validate(
    job: Job, project_dir: Path, uv_cache_dir: Path, emitters: _Emitters
) -> tuple[ValidationResult | None, tuple[str, str] | None]:
    emitters.started(job, PHASE_LG_VALIDATE)
    try:
        result = run_keyless_gate(
            project_dir, import_snippet=KEYLESS_IMPORT_SNIPPET, uv_cache_dir=uv_cache_dir
        )
    except Exception as exc:
        details: dict[str, object] = {"errorType": type(exc).__name__}
        failed_step = getattr(exc, "failed_step", None)
        if failed_step is not None:
            details["step"] = failed_step.step
            details["returncode"] = failed_step.returncode
        emitters.failed(job, PHASE_LG_VALIDATE, str(exc), details)
        return None, (PHASE_LG_VALIDATE, str(exc))
    emitters.succeeded(job, PHASE_LG_VALIDATE, {"steps": [s.step for s in result.steps]})
    return result, None


__all__ = [
    "LANGGRAPH_PHASE_NAMES",
    "Converter",
    "LangGraphOutcome",
    "LangGraphPhaseError",
    "run_langgraph_phases",
]
