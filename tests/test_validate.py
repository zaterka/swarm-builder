"""Unit + integration tests for :mod:`swarm_builder.compile.validate`
(PLAN.md "The compile pipeline" Phase 5; "Spike amendments" fact 30).

Two tiers, mirroring ``tests/test_codegen_fixtures.py``'s approach to
the same real-``uv`` cost tradeoff:

- **fast, no subprocess**: the fact-30 static extras assertion
  (:func:`check_pyproject_extras`) is pure string/dataclass logic and is
  tested directly against hand-written ``pyproject.toml`` text, plus a
  deterministic timeout path against a real-but-trivial subprocess.
- **slow, real ``uv``**: a full passing gate against a scaffolded fixture
  project, and a failing gate against the same kind of project with its
  emitted ``graph.py`` deliberately broken. Marked ``slow`` (registered
  in ``tests/conftest.py``) but not deselected by default, per this
  project's Definition of Done ("make sure they actually run and pass").
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fixtures.graphs import linear_chat_graph
from fixtures.stub_fill import apply_stub_fill
from swarm_builder.compile import ResolvedModel, default_scaffold_model
from swarm_builder.compile.scaffold import scaffold
from swarm_builder.compile.validate import (
    STEP_KEYLESS_IMPORT,
    STEP_UV_SYNC,
    TIMEOUT_RETURNCODE,
    ExtrasCheckResult,
    MissingExtraError,
    ValidationStepError,
    _run_bounded,
    check_pyproject_extras,
    validate_project,
)
from swarm_builder.inherit.settings import RouteConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
UV_CACHE_DIR = REPO_ROOT / ".uv-cache"


def _route(
    key: str, *, api: str | None = None, base_url: str | None = None
) -> RouteConfig:
    return RouteConfig(
        key=key,
        api=api,
        base_url=base_url,
        api_key_env=None,
        aws_profile=None,
        aws_region=None,
        models=(),
    )


def _resolved_model(*, extras: tuple[str, ...]) -> ResolvedModel:
    return ResolvedModel(
        helper_source=(
            'DEFAULT_MODEL: str = "bedrock:us.anthropic.claude-opus-5"\n'
            "\n\n"
            "def _resolve_default_model() -> str:\n"
            '    return DEFAULT_MODEL\n'
        ),
        default_factory_name="_resolve_default_model",
        extra_imports=(),
        pyproject_extras=extras,
        env_lines=(),
        readme_model_note="test fixture",
    )


def _write_pyproject(project_dir: Path, *, extras: tuple[str, ...]) -> None:
    bracket = f"[{','.join(extras)}]" if extras else ""
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "pyproject.toml").write_text(
        "[project]\n"
        'name = "swarm-workflow"\n'
        "dependencies = [\n"
        f'    "pydantic-ai-slim{bracket}==2.43.0",\n'
        '    "pydantic-graph==2.43.0",\n'
        "]\n"
    )


# ---------------------------------------------------------------------------
# Fact 30: static pyproject.toml extras assertion (fast, no subprocess).
# ---------------------------------------------------------------------------


def test_check_pyproject_extras_passes_when_extra_is_declared(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, extras=("bedrock",))
    resolved_model = _resolved_model(extras=("bedrock",))

    result = check_pyproject_extras(tmp_path, route=None, resolved_model=resolved_model)

    assert isinstance(result, ExtrasCheckResult)
    assert result.ok
    assert result.required_extras == ("bedrock",)
    assert result.declared_extras == ("bedrock",)
    assert result.missing_extras == ()


def test_check_pyproject_extras_rejects_a_missing_extra(tmp_path: Path) -> None:
    # pyproject.toml declares no extras at all, but the resolved model
    # requires "bedrock" -- this is exactly the fact-30 gap: a keyless
    # import would still succeed against this file.
    _write_pyproject(tmp_path, extras=())
    resolved_model = _resolved_model(extras=("bedrock",))

    with pytest.raises(MissingExtraError) as exc_info:
        check_pyproject_extras(tmp_path, route=None, resolved_model=resolved_model)

    assert exc_info.value.result.missing_extras == ("bedrock",)
    assert "bedrock" in str(exc_info.value)


def test_check_pyproject_extras_also_derives_the_requirement_from_the_route(
    tmp_path: Path,
) -> None:
    """Even when ``resolved_model`` itself under-declares (a hypothetical
    drift bug), the route's own ``api`` protocol independently requires
    the extra via ``classify_route`` -- proving the two decision
    procedures are cross-checked, not just resolved_model trusted
    blindly."""
    _write_pyproject(tmp_path, extras=())
    route = _route("amazon-bedrock", api="bedrock-converse-stream")
    resolved_model = _resolved_model(extras=())  # deliberately under-declares

    with pytest.raises(MissingExtraError) as exc_info:
        check_pyproject_extras(tmp_path, route=route, resolved_model=resolved_model)

    assert "bedrock" in exc_info.value.result.missing_extras


def test_check_pyproject_extras_passes_with_extra_extras_declared(tmp_path: Path) -> None:
    """Declaring MORE extras than required is fine -- only a missing
    required extra is a violation."""
    _write_pyproject(tmp_path, extras=("bedrock", "openai"))
    resolved_model = _resolved_model(extras=("bedrock",))

    result = check_pyproject_extras(tmp_path, route=None, resolved_model=resolved_model)

    assert result.ok


# ---------------------------------------------------------------------------
# Timeout path (fast, real subprocess, deterministic).
# ---------------------------------------------------------------------------


def test_run_bounded_kills_a_wedged_process_and_reports_the_timeout(tmp_path: Path) -> None:
    """A process that outlives its timeout budget must be killed, not
    left to leak (a documented failure mode: "Validation subprocess
    leaks") -- and the timeout must be visible in the result rather than
    silently swallowed."""
    started = time.monotonic()

    result = _run_bounded(
        ["python3", "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={},
        timeout_s=0.5,
    )

    elapsed = time.monotonic() - started
    assert result.returncode != 0
    assert elapsed < 10, "the wedged process was not actually killed promptly"
    assert "timed out" in result.stderr


def test_run_bounded_kills_the_whole_process_group_not_just_the_child(
    tmp_path: Path,
) -> None:
    """A timed-out step must not leave a *grandchild* holding the pipes.

    Every command this helper runs is ``uv``, which forks the interpreter
    that actually runs the generated project. Killing only the direct
    child leaves that grandchild alive with the inherited stdout/stderr
    pipes still open, and the follow-up ``communicate()`` -- which waits
    for end-of-file on those pipes, not merely for the child to exit --
    then blocks until the grandchild decides to exit on its own. That is
    a hang, not a timeout, which is why the child is started in its own
    session and killed as a process group.

    The shim below reproduces exactly that topology: a parent that exits
    promptly, and a ``sleep(600)`` grandchild holding the pipes. A 1s
    budget must still return, and return quickly.
    """
    grandchild = "import time; time.sleep(600)"
    parent_that_forks_and_outlives_its_budget = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "time.sleep(300)"
    )

    started = time.monotonic()
    result = _run_bounded(
        ["python3", "-c", parent_that_forks_and_outlives_its_budget],
        cwd=tmp_path,
        env={},
        timeout_s=1.0,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == TIMEOUT_RETURNCODE
    assert "timed out" in result.stderr
    assert elapsed < 30, (
        "communicate() blocked on the grandchild's inherited pipes -- the "
        "process group was not killed"
    )


# ---------------------------------------------------------------------------
# Full gate, real uv (slow).
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_full_gate_passes_against_a_real_scaffolded_project(tmp_path: Path) -> None:
    graph = linear_chat_graph()
    project_dir = tmp_path / "linear_chat"
    resolved_model = default_scaffold_model()
    scaffold(graph, project_dir, resolved_model)
    apply_stub_fill(project_dir, "linear_chat")

    result = validate_project(
        project_dir,
        route=None,
        resolved_model=resolved_model,
        uv_cache_dir=UV_CACHE_DIR,
    )

    assert result.ok
    assert [step.step for step in result.steps] == [
        "uv_sync",
        "keyless_import",
        "dry_run",
    ]
    for step in result.steps:
        assert step.returncode == 0
    assert "ALL CHECKS PASSED" in result.steps[-1].stdout
    # The project must still be there after a successful run -- validate
    # never deletes or moves what it validates.
    assert project_dir.exists()
    assert (project_dir / "pyproject.toml").exists()


@pytest.mark.slow
def test_full_gate_reports_the_failing_step_stderr_tail_and_keeps_the_project(
    tmp_path: Path,
) -> None:
    """Break the emitted ``graph.py`` so the keyless-import step fails.
    ``uv sync`` must still succeed (the break is a pure-Python runtime
    error, not a dependency problem), and the failure must surface the
    captured stderr tail while leaving the whole project directory on
    disk for inspection."""
    graph = linear_chat_graph()
    project_dir = tmp_path / "linear_chat_broken"
    resolved_model = default_scaffold_model()
    scaffold(graph, project_dir, resolved_model)
    apply_stub_fill(project_dir, "linear_chat")

    graph_py = project_dir / "src" / "swarm_workflow" / "graph.py"
    original = graph_py.read_text()
    graph_py.write_text(
        original + '\nraise RuntimeError("intentionally broken for test_validate.py")\n'
    )

    with pytest.raises(ValidationStepError) as exc_info:
        validate_project(
            project_dir,
            route=None,
            resolved_model=resolved_model,
            uv_cache_dir=UV_CACHE_DIR,
        )

    error = exc_info.value
    assert error.failed_step.step == STEP_KEYLESS_IMPORT
    assert "intentionally broken" in error.failed_step.stderr
    assert "intentionally broken" in str(error)
    # uv sync ran and succeeded; the dry run never got a chance to.
    assert [step.step for step in error.result.steps] == [STEP_UV_SYNC, STEP_KEYLESS_IMPORT]
    assert error.result.steps[0].ok

    # The broken project must still exist on disk for inspection.
    assert project_dir.exists()
    assert graph_py.exists()
    assert "intentionally broken" in graph_py.read_text()
