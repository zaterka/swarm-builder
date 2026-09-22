"""Phase-5 validation runner (PLAN.md "The compile pipeline" Phase 5).

``validate_project`` runs the keyless validation gate against an already
-scaffolded (and, in the real pipeline, already-filled) project
directory, in order:

1. a **static** check that ``pyproject.toml`` declares every
   ``pydantic-ai-slim`` extra the inherited route's ``api`` protocol
   requires (fact 30) -- no subprocess, no ``uv``, so it runs before
   spending any time on the real gate;
2. ``uv sync``;
3. ``uv run python -c "import swarm_workflow.graph"`` -- proves the
   project imports with **no** model API key or AWS credentials present
   (fact 7);
4. ``uv run python validate/dry_run.py`` -- the script ``scaffold.py``
   already emitted; it asserts ``build()``, the ``render()`` golden, the
   node-key set, and the ``TestModel`` dry run (facts 15, 16, 18). This
   module does not reimplement any of those assertions -- it only runs
   the script and reports the result.

**Why the extras check cannot be a runtime assertion (fact 30).**
``Agent("bedrock:...", defer_model_check=True)`` constructs with *zero*
extras installed -- the ``[bedrock]`` extra is only needed for a real
``.run()``. Step 3's keyless import would therefore pass even if the
route's required extra were entirely missing from ``pyproject.toml``.
Nothing in the keyless gate can prove extras sufficiency, so step 1 is a
static, string-level check instead.

**Why this module never re-resolves settings.** The caller (the compile
pipeline) already resolved the route via ``inherit.settings`` and turned
it into the :class:`~swarm_builder.compile.ResolvedModel` that
``scaffold.py`` used to write ``pyproject.toml`` in the first place. This
module receives both the matched
:class:`~swarm_builder.inherit.settings.RouteConfig` and that
:class:`~swarm_builder.compile.ResolvedModel` as parameters and only
*classifies* the route (a pure function, no I/O) -- it never reads
``$DSH_HOME/settings.yaml`` itself.

**Failure reporting.** Every failure raises rather than returning
``bool``/``None`` (this project's Python conventions), and every
exception carries the full result built so far, so a caller cannot
silently ignore a failure or a subset of the steps that ran. On a
non-zero subprocess exit, the project directory is left untouched on
disk for inspection -- this module never deletes or repairs it.

**Bounded subprocesses (a documented failure mode: "Validation
subprocess leaks").** Every ``uv`` invocation runs under an explicit
timeout, in its own session, and under a ``try``/``finally`` that
guarantees the child is terminated and reaped even when ``communicate()``
raises for a reason other than a timeout, so a wedged ``uv`` (or a wedged
generated project) can never leak a process. On timeout the whole process
*group* is killed, not just the direct child: ``uv`` forks the interpreter
that actually runs the generated project, and that grandchild inherits the
output pipes -- killing only ``uv`` would leave ``communicate()`` blocked
until the grandchild exits on its own.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from swarm_builder.compile import ResolvedModel
from swarm_builder.config import get_uv_cache_dir
from swarm_builder.inherit.routes import classify_route
from swarm_builder.inherit.settings import RouteConfig

#: Step names, in the fixed order Phase 5 runs them.
STEP_UV_SYNC = "uv_sync"
STEP_KEYLESS_IMPORT = "keyless_import"
STEP_DRY_RUN = "dry_run"

#: The exact snippet PLAN.md Phase 5 step 2 names: proves the generated
#: project imports with no model API key or AWS credentials present
#: (fact 7). Named here so it appears in exactly one place.
KEYLESS_IMPORT_SNIPPET = "import swarm_workflow.graph"

#: Per-step subprocess timeouts, in seconds. `uv sync` gets the longest
#: budget since a cold cache resolves and (occasionally) builds wheels;
#: the other two are plain interpreter invocations against an
#: already-synced venv and should return in a few seconds.
UV_SYNC_TIMEOUT_S: float = 180.0
KEYLESS_IMPORT_TIMEOUT_S: float = 30.0
DRY_RUN_TIMEOUT_S: float = 60.0

#: Synthetic returncode reported for a step killed by our own timeout,
#: distinct from any real process exit code. Mirrors the exit code the
#: GNU coreutils `timeout` command itself uses for this situation, which
#: is why this specific value was picked over an arbitrary one.
TIMEOUT_RETURNCODE: int = 124

#: How many trailing lines of stderr to surface in a failure report.
#: Full stderr is still on the captured StepOutcome; this only bounds
#: what gets embedded in the exception message shown to a human/UI.
STDERR_TAIL_LINE_COUNT: int = 40

#: Ambient credential env vars stripped before every `uv`/subprocess
#: call this module makes, so a pass here actually proves keylessness
#: rather than merely running to succeed because the developer's own
#: shell happens to have working credentials exported (mirrors
#: tests/test_codegen_fixtures.py's reproduce-script env, and fact 7's
#: "no key" requirement).
CREDENTIAL_ENV_VARS_TO_STRIP: tuple[str, ...] = (
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "SWARM_API_KEY",
)

#: Matches the `pydantic-ai-slim[...]` dependency line `scaffold.py`
#: renders, capturing the bracketed extras list (possibly empty).
_PYPROJECT_EXTRAS_RE = re.compile(r"pydantic-ai-slim\[(?P<extras>[^\]]*)\]")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ValidationError(Exception):
    """Base class for every Phase-5 validation failure.

    Never raised directly -- always one of :class:`MissingExtraError` or
    :class:`ValidationStepError`, both of which carry the full result
    accumulated up to the point of failure.
    """


class MissingExtraError(ValidationError):
    """Raised by :func:`check_pyproject_extras` when the emitted
    ``pyproject.toml`` does not declare an extra the inherited route
    requires (fact 30). The project directory is untouched; this is a
    read-only check.
    """

    def __init__(self, project_dir: Path, result: ExtrasCheckResult) -> None:
        self.project_dir = project_dir
        self.result = result
        declared = ", ".join(result.declared_extras) or "none"
        missing = ", ".join(result.missing_extras)
        super().__init__(
            f"{project_dir / 'pyproject.toml'} is missing required extra(s) "
            f"[{missing}] (declared: [{declared}])"
        )


class ValidationStepError(ValidationError):
    """Raised by :func:`validate_project` when a subprocess step exits
    non-zero. Carries the :class:`ValidationResult` built from every
    step that ran (including the failing one) plus the failing
    :class:`StepOutcome` itself, so a caller can report which step
    failed and inspect every step's captured output -- not just the
    first line of a message.
    """

    def __init__(self, result: ValidationResult, failed_step: StepOutcome) -> None:
        self.result = result
        self.failed_step = failed_step
        tail = _tail_lines(failed_step.stderr, STDERR_TAIL_LINE_COUNT)
        super().__init__(
            f"validation step {failed_step.step!r} failed (exit "
            f"{failed_step.returncode}) for project {result.project_dir}; "
            f"stderr tail:\n{tail}"
        )


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtrasCheckResult:
    """Outcome of :func:`check_pyproject_extras`.

    Attributes:
        required_extras: Every ``pydantic-ai-slim`` extra this route/
            resolved-model combination requires, sorted.
        declared_extras: Every extra actually found in the project's
            ``pyproject.toml``, sorted.
    """

    required_extras: tuple[str, ...]
    declared_extras: tuple[str, ...]

    @property
    def missing_extras(self) -> tuple[str, ...]:
        """Required extras absent from ``declared_extras``."""
        declared = set(self.declared_extras)
        return tuple(extra for extra in self.required_extras if extra not in declared)

    @property
    def ok(self) -> bool:
        """``True`` when every required extra is declared."""
        return not self.missing_extras


@dataclass(frozen=True)
class StepOutcome:
    """Captured result of one bounded subprocess step.

    Attributes:
        step: One of ``STEP_UV_SYNC``/``STEP_KEYLESS_IMPORT``/``STEP_DRY_RUN``.
        command: The argv actually invoked.
        returncode: The process exit code, or :data:`TIMEOUT_RETURNCODE`
            when the step was killed by its own timeout.
        stdout: Captured standard output.
        stderr: Captured standard error.
    """

    step: str
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class ValidationResult:
    """The full Phase-5 report: which steps ran, and with what outcome.

    A caller receives this on success. On failure, the same shape is
    instead attached to whichever :class:`ValidationError` subclass was
    raised -- there is no path through this module that discards a
    step's captured output.

    Attributes:
        project_dir: The validated project directory.
        extras: The fact-30 static extras-declaration check result.
        steps: Every subprocess step that ran, in order, up to and
            including the first failure (or all three, on success).
    """

    project_dir: Path
    extras: ExtrasCheckResult
    steps: tuple[StepOutcome, ...]

    @property
    def ok(self) -> bool:
        return self.extras.ok and all(step.ok for step in self.steps)


# ---------------------------------------------------------------------------
# Fact 30: static pyproject.toml extras assertion
# ---------------------------------------------------------------------------


def _declared_extras(pyproject_text: str) -> frozenset[str]:
    """Extract the bracketed ``pydantic-ai-slim[...]`` extras from a
    rendered ``pyproject.toml``'s text. Empty when the dependency line
    has no brackets at all (no extras declared)."""
    match = _PYPROJECT_EXTRAS_RE.search(pyproject_text)
    if match is None:
        return frozenset()
    return frozenset(part.strip() for part in match.group("extras").split(",") if part.strip())


def check_pyproject_extras(
    project_dir: Path, *, route: RouteConfig | None, resolved_model: ResolvedModel
) -> ExtrasCheckResult:
    """Verify ``project_dir/pyproject.toml`` declares every extra the
    inherited route's ``api`` protocol requires (PLAN.md fact 30).

    The required-extras set is the union of ``resolved_model``'s own
    declared extras (what ``scaffold.py`` actually wrote them from) and,
    when ``route`` is given, whatever :func:`classify_route` -- an
    independent decision procedure over the route's own ``api``/
    ``base_url`` fields -- says that route requires. Checking both
    catches not only a missing extra but a drift between the two
    decision procedures that derive an extra requirement (fact 22/23):
    ``to_resolved_model`` (which produced ``resolved_model``) and
    ``classify_route`` are deliberately independent implementations
    (see ``inherit/routes.py``'s module docstring), so agreement between
    them and the file on disk is a real property being tested, not a
    tautology.

    Args:
        project_dir: The scaffolded project directory (its
            ``pyproject.toml`` must already exist).
        route: The matched route this compile inherited, or ``None``
            when no route was resolved (env-fallback/bundle-default
            paths carry no route to independently re-derive an extra
            from; the check then falls back to ``resolved_model`` alone).
        resolved_model: The same :class:`ResolvedModel` `scaffold.py`
            used to render this project's ``pyproject.toml``.

    Returns:
        The :class:`ExtrasCheckResult`. Only returned when every
        required extra is declared -- otherwise this function raises.

    Raises:
        MissingExtraError: If one or more required extras are absent
            from the declared extras.
    """
    required: set[str] = set(resolved_model.pyproject_extras)
    if route is not None:
        emission = classify_route(route)
        if emission.required_extra is not None:
            required.add(emission.required_extra)

    pyproject_text = (project_dir / "pyproject.toml").read_text()
    result = ExtrasCheckResult(
        required_extras=tuple(sorted(required)),
        declared_extras=tuple(sorted(_declared_extras(pyproject_text))),
    )
    if not result.ok:
        raise MissingExtraError(project_dir, result)
    return result


# ---------------------------------------------------------------------------
# Bounded subprocess runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawResult:
    """Internal handoff from :func:`_run_bounded` to
    :func:`validate_project` -- not part of the public result shape,
    which additionally carries the step name and the exact command."""

    returncode: int
    stdout: str
    stderr: str


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill ``process`` and every descendant that shares its session.

    Every command this module runs is ``uv``, which forks the real
    interpreter that runs the generated project. Killing only the direct
    child leaves that grandchild holding the stdout/stderr pipes open,
    and a subsequent ``communicate()`` then blocks until the grandchild
    exits -- measured in ``agent.py``'s copy of this helper as a
    ``time.sleep(600)`` snippet that hung the whole test suite. The child
    is started with ``start_new_session=True`` (see :func:`_run_bounded`),
    so its process group contains exactly that child's tree and nothing
    of ours.

    Args:
        process: The live child, already started in its own session.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone, or not a group leader after all: fall back to the
        # single-process kill so this never becomes the reason a
        # subprocess leaks.
        process.kill()


def _run_bounded(
    command: Sequence[str], *, cwd: Path, env: dict[str, str], timeout_s: float
) -> _RawResult:
    """Run ``command`` with a hard wall-clock timeout, guaranteeing the
    child process is terminated and reaped no matter how this function
    exits (a documented failure mode: "Validation subprocess leaks").

    Uses :class:`subprocess.Popen` directly rather than
    ``subprocess.run(timeout=...)`` so the termination guarantee is
    explicit and independent of ``subprocess.run``'s own (already
    correct, but not the point) timeout handling: the ``finally`` clause
    below still fires and kills the process even if
    ``communicate()`` raises something other than
    :class:`subprocess.TimeoutExpired`.

    The child is started in its own session and killed as a process
    *group* rather than as a single process: every command here is
    ``uv``, which forks the interpreter that runs the generated project,
    and killing only ``uv`` would leave that grandchild holding the
    output pipes open forever. See :func:`_kill_process_tree`.

    Args:
        command: The argv to execute.
        cwd: Working directory for the subprocess.
        env: The complete environment to run it in.
        timeout_s: Wall-clock seconds to allow before killing it.

    Returns:
        A :class:`_RawResult`. On timeout, ``returncode`` is
        :data:`TIMEOUT_RETURNCODE` and ``stderr`` has a trailing note
        describing the timeout appended.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            _kill_process_tree(process)
            stdout, stderr = process.communicate()
            stderr = f"{stderr}\n[validate.py] step timed out after {timeout_s}s and was killed"
            returncode = TIMEOUT_RETURNCODE
    finally:
        # Belt-and-suspenders: if anything above raised before the
        # process was reaped (including a KeyboardInterrupt racing the
        # timeout branch), make sure it is not left running.
        if process.poll() is None:
            _kill_process_tree(process)
            process.wait()
    return _RawResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _tail_lines(text: str, count: int) -> str:
    """Return the last ``count`` lines of ``text`` (fewer if shorter)."""
    lines = text.splitlines()
    return "\n".join(lines[-count:])


def _build_subprocess_env(uv_cache_dir: Path) -> dict[str, str]:
    """The environment every ``uv`` invocation in this module runs
    under: an explicit ``UV_CACHE_DIR`` (fact 10 -- ``~/.cache/uv`` is
    not writable under this project's sandbox) and every ambient
    credential var stripped, so a pass here actually proves the
    keyless-gate claim rather than incidentally succeeding because the
    caller's shell happens to have working credentials exported."""
    env = dict(os.environ)
    env["UV_CACHE_DIR"] = str(uv_cache_dir)
    for var in CREDENTIAL_ENV_VARS_TO_STRIP:
        env.pop(var, None)
    return env


# ---------------------------------------------------------------------------
# validate_project
# ---------------------------------------------------------------------------


def validate_project(
    project_dir: Path,
    *,
    route: RouteConfig | None,
    resolved_model: ResolvedModel,
    uv_cache_dir: Path | None = None,
    uv_sync_timeout_s: float = UV_SYNC_TIMEOUT_S,
    keyless_import_timeout_s: float = KEYLESS_IMPORT_TIMEOUT_S,
    dry_run_timeout_s: float = DRY_RUN_TIMEOUT_S,
) -> ValidationResult:
    """Run PLAN.md Phase 5's full keyless validation gate.

    Runs, in order, stopping at the first failure:

    1. :func:`check_pyproject_extras` (fact 30, no subprocess);
    2. ``uv sync``;
    3. ``uv run python -c "import swarm_workflow.graph"`` (fact 7);
    4. ``uv run python validate/dry_run.py`` (facts 15, 16, 18 --
       asserted by the emitted script itself, not reimplemented here).

    The project directory is never modified, moved, or deleted by this
    function, regardless of outcome, so a failure always leaves it on
    disk for inspection.

    Args:
        project_dir: A scaffolded (and, in the real pipeline,
            already-filled and boundary-checked) project directory
            containing ``pyproject.toml`` and ``validate/dry_run.py``.
        route: The matched route this compile inherited, or ``None``
            when none was resolved. Passed straight to
            :func:`check_pyproject_extras`; never re-resolved here.
        resolved_model: The same :class:`ResolvedModel` `scaffold.py`
            used to write this project's ``pyproject.toml``.
        uv_cache_dir: Explicit ``UV_CACHE_DIR`` for every ``uv``
            invocation (fact 10). Defaults to
            :func:`swarm_builder.config.get_uv_cache_dir`.
        uv_sync_timeout_s: Wall-clock timeout for the ``uv sync`` step.
        keyless_import_timeout_s: Wall-clock timeout for the keyless
            import step.
        dry_run_timeout_s: Wall-clock timeout for the dry-run step.

    Returns:
        A :class:`ValidationResult` with every step's outcome. Only
        returned when every step -- extras check included -- succeeded.

    Raises:
        MissingExtraError: If the fact-30 extras check fails. Raised
            before any subprocess runs.
        ValidationStepError: If ``uv sync``, the keyless import, or the
            dry run exits non-zero or times out. Carries every step
            that ran up to and including the failing one.
    """
    extras_result = check_pyproject_extras(project_dir, route=route, resolved_model=resolved_model)
    return run_keyless_gate(
        project_dir,
        import_snippet=KEYLESS_IMPORT_SNIPPET,
        extras_result=extras_result,
        uv_cache_dir=uv_cache_dir,
        uv_sync_timeout_s=uv_sync_timeout_s,
        keyless_import_timeout_s=keyless_import_timeout_s,
        dry_run_timeout_s=dry_run_timeout_s,
    )


def run_keyless_gate(
    project_dir: Path,
    *,
    import_snippet: str,
    extras_result: ExtrasCheckResult | None = None,
    uv_cache_dir: Path | None = None,
    uv_sync_timeout_s: float = UV_SYNC_TIMEOUT_S,
    keyless_import_timeout_s: float = KEYLESS_IMPORT_TIMEOUT_S,
    dry_run_timeout_s: float = DRY_RUN_TIMEOUT_S,
) -> ValidationResult:
    """Run the three-step keyless gate (``uv sync`` -> import -> dry run).

    The framework-agnostic core of :func:`validate_project`: it knows
    nothing about pydantic-ai extras, only that a project must sync,
    import with every credential stripped, and pass its own
    ``validate/dry_run.py``. The LangGraph target (``compile/langgraph``)
    calls it with its own import snippet.

    Args:
        project_dir: The project to validate; never modified.
        import_snippet: The ``python -c`` snippet proving a keyless import.
        extras_result: A prior extras check to carry in the result, or
            ``None`` for a project with no extras contract.
        uv_cache_dir: Explicit ``UV_CACHE_DIR``; defaults to the env.
        uv_sync_timeout_s: Wall-clock timeout for ``uv sync``.
        keyless_import_timeout_s: Wall-clock timeout for the import step.
        dry_run_timeout_s: Wall-clock timeout for the dry-run step.

    Returns:
        Every step's outcome. Only returned when all three succeeded.

    Raises:
        ValidationStepError: If a step exits non-zero or times out.
    """
    resolved_extras = (
        extras_result
        if extras_result is not None
        else ExtrasCheckResult(required_extras=(), declared_extras=())
    )
    resolved_cache_dir = uv_cache_dir if uv_cache_dir is not None else get_uv_cache_dir()
    env = _build_subprocess_env(resolved_cache_dir)

    steps: list[StepOutcome] = []

    def run_step(step_name: str, command: Sequence[str], timeout_s: float) -> None:
        raw = _run_bounded(command, cwd=project_dir, env=env, timeout_s=timeout_s)
        outcome = StepOutcome(
            step=step_name,
            command=tuple(command),
            returncode=raw.returncode,
            stdout=raw.stdout,
            stderr=raw.stderr,
        )
        steps.append(outcome)
        if not outcome.ok:
            partial_result = ValidationResult(
                project_dir=project_dir, extras=resolved_extras, steps=tuple(steps)
            )
            raise ValidationStepError(partial_result, outcome)

    run_step(STEP_UV_SYNC, ["uv", "sync"], uv_sync_timeout_s)
    run_step(
        STEP_KEYLESS_IMPORT,
        ["uv", "run", "python", "-c", import_snippet],
        keyless_import_timeout_s,
    )
    run_step(STEP_DRY_RUN, ["uv", "run", "python", "validate/dry_run.py"], dry_run_timeout_s)

    return ValidationResult(project_dir=project_dir, extras=resolved_extras, steps=tuple(steps))


__all__ = [
    "CREDENTIAL_ENV_VARS_TO_STRIP",
    "DRY_RUN_TIMEOUT_S",
    "ExtrasCheckResult",
    "KEYLESS_IMPORT_SNIPPET",
    "KEYLESS_IMPORT_TIMEOUT_S",
    "MissingExtraError",
    "STDERR_TAIL_LINE_COUNT",
    "STEP_DRY_RUN",
    "STEP_KEYLESS_IMPORT",
    "STEP_UV_SYNC",
    "StepOutcome",
    "TIMEOUT_RETURNCODE",
    "UV_SYNC_TIMEOUT_S",
    "ValidationError",
    "ValidationResult",
    "ValidationStepError",
    "check_pyproject_extras",
    "run_keyless_gate",
    "validate_project",
]
