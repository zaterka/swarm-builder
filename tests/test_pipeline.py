"""Tests for the five-phase compile orchestrator (``compile/pipeline.py``).

Everything here runs with no model and no real ``$DSH_HOME``: the fill
seam is either exercised through ``SWARM_FAKE_FILL=1`` (which wraps
``compile/fake_fill.py``) or through an injected fake :class:`Filler`
defined in this module. Every test points ``DSH_HOME``,
``SWARM_WORKSPACE``, and ``UV_CACHE_DIR`` at ``tmp_path``, so no test
can read or write the developer's real ``~/.dsh`` or this repository's
``workspace/``.

The gates that touch a real subprocess (``uv sync``) are marked ``slow``
(registered in ``tests/conftest.py``) exactly like the rest of this
project's keyless-validation tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fixtures.graphs import (
    POSITIVE_FIXTURES,
    cycle_graph,
    linear_chat_graph,
)
from swarm_builder.compile import ResolvedModel
from swarm_builder.compile.fake_fill import _splice_region, apply_fake_fill
from swarm_builder.compile.jobs import Job, JobRegistry
from swarm_builder.compile.pipeline import (
    ERROR_CODE_PHASE_FAILED,
    PHASE_BOUNDARY,
    PHASE_FILL,
    PHASE_NAMES,
    PHASE_REVIEW,
    PHASE_SCAFFOLD,
    PHASE_VALIDATE,
    CompileOutcome,
    Filler,
    FillResult,
    FillUnavailableError,
    PhaseFailureError,
    _select_filler,
    fake_filler,
    run_compile,
)
from swarm_builder.compile.review import review
from swarm_builder.models import ModelSelection, SwarmGraph

REPO_ROOT = Path(__file__).resolve().parent.parent
UV_CACHE_DIR = REPO_ROOT / ".uv-cache"

#: The provider whose extras this server's own environment already
#: satisfies (``pydantic-ai-slim[openai]``) and whose key is in
#: ``inherit/routes.py``'s known-name table, so a graph-override test can
#: use it without depending on a provider this repo does not install.
_OVERRIDE_PROVIDER = "openai"
_OVERRIDE_MODEL = "gpt-4o-mini"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Pin the anyio backend to asyncio -- trio is not installed."""
    return "asyncio"


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _register(registry: JobRegistry, graph: SwarmGraph) -> str:
    """Register a job for ``graph`` and hand back its compile id."""
    compile_id = f"compile-{graph.id}"
    registry.register(compile_id, graph.id)
    return compile_id


def _events(job: Job) -> list[tuple[str, dict[str, object]]]:
    """Every emitted event as ``(event_type, payload)``, in order."""
    return [(event.event_type, event.payload) for event in job.events_after(None)]


def _phases(job: Job) -> list[tuple[str, str]]:
    """Every ``phase`` event as ``(phase_name, status)``, in order."""
    return [
        (str(payload["name"]), str(payload["status"]))
        for event_type, payload in _events(job)
        if event_type == "phase"
    ]


def _terminal_event(job: Job) -> tuple[str, dict[str, object]]:
    """The last ``done``/``error`` event, asserted to exist."""
    terminal = [
        (event_type, payload)
        for event_type, payload in _events(job)
        if event_type in ("done", "error")
    ]
    assert terminal, "no terminal done/error event was emitted"
    return terminal[-1]


async def _run(
    graph: SwarmGraph,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    project_name: str = "project",
    filler: Filler | None = None,
    dsh_home: Path | None = None,
) -> tuple[Job, CompileOutcome]:
    """Run one compile against temp-dir-isolated paths.

    Args:
        graph: The document to compile.
        tmp_path: pytest's per-test temp directory.
        monkeypatch: Used to point every env var at ``tmp_path``.
        project_name: Subdirectory of ``tmp_path`` to scaffold into.
        filler: The Phase-3 filler to inject, or ``None`` for the real
            (unavailable) path.
        dsh_home: Override for the harness settings home; defaults to a
            nonexistent temp path, i.e. "no harness installed".

    Returns:
        The finished job and the compile outcome.
    """
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(dsh_home or tmp_path / "dsh_home"))
    monkeypatch.delenv("SWARM_MODEL", raising=False)
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)
    monkeypatch.delenv("SWARM_API_KEY_ENV", raising=False)

    registry = JobRegistry()
    compile_id = _register(registry, graph)
    outcome = await run_compile(
        graph,
        registry=registry,
        compile_id=compile_id,
        project_dir=tmp_path / project_name,
        dsh_home=dsh_home or tmp_path / "dsh_home",
        filler=filler,
        uv_cache_dir=UV_CACHE_DIR,
    )
    return registry.get(compile_id), outcome


def _make_always_fail_filler() -> tuple[Filler, list[str | None]]:
    """Build a filler that always fails Phase 4 plus a recorder of the
    ``previous_failure`` each attempt received.

    It creates a file outside the scaffolded set, which is a genuine
    boundary violation and therefore exercises the real retry path
    rather than a simulated one.

    Returns:
        The filler and the list its calls append to.
    """
    calls: list[str | None] = []

    async def always_fail(
        *,
        project_dir: Path,
        graph: SwarmGraph,
        resolved_model: ResolvedModel,
        live_model: object | None,
        previous_failure: str | None,
    ) -> FillResult:
        del graph, resolved_model, live_model
        calls.append(previous_failure)
        (project_dir / "escaped.py").write_text("# outside the scaffolded set\n")
        return FillResult(filled_node_ids=("escape",))

    return always_fail, calls


# ---------------------------------------------------------------------------
# 1. The full five-phase run, through the real keyless validation gate
# ---------------------------------------------------------------------------


async def test_dry_run_switch_alone_selects_the_stub_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app's own dry-run switch is enough -- no ``SWARM_FAKE_FILL`` in the
    environment -- so a brand-new user can compile with no provider at all.

    The injected filler explodes, so the compile could only succeed by using
    the stub; the ``done`` payload then reports ``dryRun`` so a client can
    label the result honestly.
    """
    from swarm_builder.appconfig import AppConfig, save_config

    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    save_config(AppConfig(dry_run=True))

    def exploding_filler(**kwargs: object) -> object:
        raise AssertionError("the model-backed filler must not be used in dry run")

    graph = POSITIVE_FIXTURES["mixed_programmatic"]()
    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))

    outcome = await run_compile(
        graph,
        registry=registry,
        compile_id=compile_id,
        project_dir=tmp_path / "dry-run",
        dsh_home=tmp_path / "dsh_home",
        filler=exploding_filler,  # type: ignore[arg-type]
        uv_cache_dir=UV_CACHE_DIR,
    )

    assert registry.get(compile_id).status == "succeeded"
    assert outcome.dry_run is True
    payload = registry.get(compile_id).result.value
    assert payload is not None and payload["dryRun"] is True


async def test_an_explicit_live_request_cannot_defeat_an_exported_offline_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SWARM_FAKE_FILL=1`` exists so an offline or CI run cannot silently
    spend real credentials, so an explicit "run for real" must not override it
    -- checked once, at the freeze point."""
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")

    def exploding_filler(**kwargs: object) -> object:
        raise AssertionError("the model-backed filler must not be used")

    graph = POSITIVE_FIXTURES["mixed_programmatic"]()
    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))

    outcome = await run_compile(
        graph,
        registry=registry,
        compile_id=compile_id,
        project_dir=tmp_path / "forced",
        dsh_home=tmp_path / "dsh_home",
        filler=exploding_filler,  # type: ignore[arg-type]
        uv_cache_dir=UV_CACHE_DIR,
        dry_run=False,  # deliberately asking for a real compile
    )

    assert registry.get(compile_id).status == "succeeded"
    assert outcome.dry_run is True


async def test_an_explicit_dry_run_argument_beats_a_flipped_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run freezes the decision at job start and passes it down. Flipping
    the persisted switch afterwards must not change which filler the job that
    is already running selected."""

    def exploding_filler(**kwargs: object) -> object:
        raise AssertionError("the model-backed filler must not be used in dry run")

    graph = POSITIVE_FIXTURES["mixed_programmatic"]()
    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)

    # The switch is OFF in the persisted configuration; the explicit argument
    # is what decides.
    outcome = await run_compile(
        graph,
        registry=registry,
        compile_id=compile_id,
        project_dir=tmp_path / "frozen",
        dsh_home=tmp_path / "dsh_home",
        filler=exploding_filler,  # type: ignore[arg-type]
        uv_cache_dir=UV_CACHE_DIR,
        dry_run=True,
    )

    assert registry.get(compile_id).status == "succeeded"
    assert outcome.dry_run is True


def test_unknown_model_warning_is_suppressed_for_the_offline_default() -> None:
    """The internal stand-in model id is not a decision the user made, so it
    must never be the subject of a "check your model id" warning."""
    from swarm_builder.compile import ResolvedModel
    from swarm_builder.compile.pipeline import _warn_if_model_name_unknown
    from swarm_builder.inherit.settings import EffectiveModel

    registry = JobRegistry()
    job = registry.register("warn", "g1", kind="compile")

    effective = EffectiveModel(
        provider="deepseek-official",
        model="not-a-real-model",
        reasoning_effort=None,
        base_url=None,
        api_key_env=None,
        source="bundle-default",
        route=None,
    )
    resolved = ResolvedModel(
        helper_source="DEFAULT_MODEL = 'deepseek:not-a-real-model'",
        default_factory_name="_resolve_default_model",
        extra_imports=(),
        pyproject_extras=("openai",),
        env_lines=("SWARM_MODEL=deepseek:not-a-real-model",),
        readme_model_note="",
    )

    _warn_if_model_name_unknown(job, effective, resolved)

    assert [payload for _, payload in _events(job)] == []


def test_unknown_model_warning_still_fires_for_a_chosen_model() -> None:
    """The warning keeps its purpose: it catches a model id the user picked
    that the installed pydantic-ai does not know, before the export is run
    for real."""
    from swarm_builder.compile import ResolvedModel
    from swarm_builder.compile.pipeline import _warn_if_model_name_unknown
    from swarm_builder.inherit.settings import EffectiveModel

    registry = JobRegistry()
    job = registry.register("warn", "g1", kind="compile")

    effective = EffectiveModel(
        provider="openai",
        model="not-a-real-model",
        reasoning_effort=None,
        base_url=None,
        api_key_env="OPENAI_API_KEY",
        source="app-config",
        route=None,
    )
    resolved = ResolvedModel(
        helper_source="DEFAULT_MODEL = 'openai:not-a-real-model'",
        default_factory_name="_resolve_default_model",
        extra_imports=(),
        pyproject_extras=("openai",),
        env_lines=("SWARM_MODEL=openai:not-a-real-model",),
        readme_model_note="",
    )

    _warn_if_model_name_unknown(job, effective, resolved)

    warnings = [payload for event_type, payload in _events(job) if event_type == "warning"]
    assert warnings and warnings[0]["code"] == "unknown_model_name"
    assert "Model settings" in warnings[0]["message"]
    for foreign in ("harness", "settings.yaml"):
        assert foreign not in warnings[0]["message"]


@pytest.mark.slow
async def test_fake_fill_run_succeeds_and_generated_project_passes_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SWARM_FAKE_FILL=1`` compiles a real fixture graph end to end:
    every phase runs, the job ends ``succeeded``, the project is on disk,
    and the generated project passes ``uv sync`` + keyless import +
    ``dry_run.py`` (run by Phase 5 itself, whose steps are asserted)."""
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    graph = POSITIVE_FIXTURES["mixed_programmatic"]()
    project_dir = tmp_path / "mixed"

    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    monkeypatch.delenv("SWARM_MODEL", raising=False)

    outcome = await run_compile(
        graph,
        registry=registry,
        compile_id=compile_id,
        project_dir=project_dir,
        dsh_home=tmp_path / "dsh_home",
        uv_cache_dir=UV_CACHE_DIR,
    )
    job = registry.get(compile_id)

    assert job.status == "succeeded"
    assert outcome.project_dir == project_dir
    assert project_dir.is_dir()

    # The stub fill actually filled the one genuinely unfilled body, and
    # the sentinel scaffold.py emits for it is gone.
    assert outcome.filled_node_ids == ("fetch",)
    step_text = (project_dir / "src/swarm_workflow/steps/fetch.py").read_text()
    assert "unfilled step body" not in step_text

    # Every phase ran, in order, exactly once, and succeeded.
    assert _phases(job) == [
        (PHASE_REVIEW, "started"),
        (PHASE_REVIEW, "succeeded"),
        (PHASE_SCAFFOLD, "started"),
        (PHASE_SCAFFOLD, "succeeded"),
        (PHASE_FILL, "started"),
        (PHASE_FILL, "succeeded"),
        (PHASE_BOUNDARY, "started"),
        (PHASE_BOUNDARY, "succeeded"),
        (PHASE_VALIDATE, "started"),
        (PHASE_VALIDATE, "succeeded"),
    ]

    event_type, payload = _terminal_event(job)
    assert event_type == "done"
    assert payload["validationSteps"] == ["uv_sync", "keyless_import", "dry_run"]
    assert payload["attempts"] == 1
    # Proof the keyless gate really ran, rather than being reported: the
    # `uv sync` step created a real venv in the project.
    assert (project_dir / ".venv").is_dir()
    assert payload["runCommand"] == (
        f"cd {project_dir} && UV_CACHE_DIR={UV_CACHE_DIR} uv sync && "
        f"UV_CACHE_DIR={UV_CACHE_DIR} uv run python validate/dry_run.py"
    )
    # The rendered diagram travels with the result so the compile panel
    # can show it without reading the project directory back.
    assert payload["diagram"].strip().startswith("stateDiagram-v2")

    # The success value the registry retained for a reconnecting client
    # is the same shape the done event carried.
    assert job.result.value == {
        key: value for key, value in payload.items() if key != "validationSteps"
    }


@pytest.mark.slow
async def test_done_event_reports_the_projects_own_run_command_and_diagram(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A linear run reports a runnable command, the golden diagram, and
    the resolved model's provenance (I5: a compile never silently spends
    credentials on an unexpected route)."""
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    graph = POSITIVE_FIXTURES["linear_chat"]()

    job, outcome = await _run(graph, tmp_path, monkeypatch, project_name="linear")

    assert job.status == "succeeded"
    event_type, payload = _terminal_event(job)
    assert event_type == "done"

    model = payload["model"]
    assert isinstance(model, dict)
    # No harness and no SWARM_MODEL: the documented bundle default.
    assert model["source"] == "bundle-default"
    assert model["provider"] == "deepseek-official"
    assert model["model"] == "deepseek-v4-flash"
    assert model["source"] == outcome.model.source

    # Both of linear_chat's steps are programmatic, so both bodies are
    # filled; agent nodes need no filling.
    assert outcome.filled_node_ids == ("intake", "summarize")
    golden_on_disk = (outcome.project_dir / "validate/golden_render.txt").read_text()
    assert golden_on_disk == outcome.diagram


# ---------------------------------------------------------------------------
# 2. Phase ordering as observed through emitted events
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_phase_events_are_ordered_and_indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each phase's ``started`` precedes its ``succeeded``, the five
    phases appear in pipeline order, and each carries its 1-based index
    and the phase total."""
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    graph = POSITIVE_FIXTURES["linear_chat"]()

    job, _ = await _run(graph, tmp_path, monkeypatch, project_name="ordering")

    phase_events = [
        payload for event_type, payload in _events(job) if event_type == "phase"
    ]
    names_in_order = [payload["name"] for payload in phase_events]
    started = [payload["name"] for payload in phase_events if payload["status"] == "started"]

    assert started == list(PHASE_NAMES)
    for payload in phase_events:
        assert payload["total"] == len(PHASE_NAMES)
        assert payload["index"] == PHASE_NAMES.index(str(payload["name"])) + 1
    # start/succeed pairs, never a phase terminal before its own start.
    assert names_in_order.index(PHASE_SCAFFOLD) > names_in_order.index(PHASE_REVIEW)
    assert names_in_order.index(PHASE_FILL) > names_in_order.index(PHASE_SCAFFOLD)


async def test_phase_one_refuses_before_creating_any_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Phase-1 review error stops the compile *without scaffolding*: no
    project directory is created (not even empty), no scaffold event is
    emitted, and the graph's own findings are reported."""
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    graph = cycle_graph()
    project_dir = tmp_path / "should-never-exist"

    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))

    with pytest.raises(PhaseFailureError) as excinfo:
        await run_compile(
            graph,
            registry=registry,
            compile_id=compile_id,
            project_dir=project_dir,
            dsh_home=tmp_path / "dsh_home",
            uv_cache_dir=UV_CACHE_DIR,
        )

    job = registry.get(compile_id)
    assert job.status == "failed"
    assert excinfo.value.failure.phase == PHASE_REVIEW
    assert not project_dir.exists()

    assert _phases(job) == [(PHASE_REVIEW, "started"), (PHASE_REVIEW, "failed")]
    event_type, payload = _terminal_event(job)
    assert event_type == "error"
    assert payload["code"] == "phase_failed"
    assert "review found" in str(payload["message"])

    # The finding's own code reaches the client, and the phase event
    # carries the structured list.
    log_codes = [
        event_payload.get("code")
        for event_type, event_payload in _events(job)
        if event_type == "log"
    ]
    assert "cycle" in log_codes
    phase_failed = [
        event_payload
        for event_type, event_payload in _events(job)
        if event_type == "phase" and event_payload["status"] == "failed"
    ][0]
    errors = phase_failed["details"]["errors"]
    assert any(error["code"] == "cycle" for error in errors)


async def test_review_warnings_are_emitted_but_do_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A soft finding becomes a ``warning`` event and the compile still
    proceeds through every later phase."""
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    graph = POSITIVE_FIXTURES["websearch"]()
    # `delegates_to` naming a child with no matching DelegateEdge is a
    # soft finding (review.py's `delegate_without_edge` warning), not an
    # error -- exactly the shape this test needs.
    nodes = [
        node.model_copy(
            update={"agent": node.agent.model_copy(update={"delegates_to": ["not_a_child"]})}
        )
        if node.agent is not None
        else node
        for node in graph.nodes
    ]
    graph = graph.model_copy(update={"nodes": nodes})
    assert any(finding.code == "delegate_without_edge" for finding in review(graph).warnings)

    job, _ = await _run(graph, tmp_path, monkeypatch, project_name="warnings")

    assert job.status == "succeeded"
    warnings = [payload for event_type, payload in _events(job) if event_type == "warning"]
    assert any(warning["code"] == "delegate_without_edge" for warning in warnings)
    for warning in warnings:
        assert isinstance(warning["nodeIds"], list)


# ---------------------------------------------------------------------------
# 3. Route resolution, the production way
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_graph_model_override_wins_and_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A graph's own ``model`` overrides the default, is announced in a
    ``log`` event with its source, and reaches the emitted project's
    ``deps.py`` (I5)."""
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    graph = linear_chat_graph()
    graph = graph.model_copy(
        update={
            "model": ModelSelection(
                provider=_OVERRIDE_PROVIDER, model=_OVERRIDE_MODEL, reasoning_effort=None
            )
        }
    )

    job, outcome = await _run(graph, tmp_path, monkeypatch, project_name="override")

    assert job.status == "succeeded"
    log_messages = [
        str(payload["message"]) for event_type, payload in _events(job) if event_type == "log"
    ]
    assert any(
        f"model: {_OVERRIDE_PROVIDER}:{_OVERRIDE_MODEL} (source=graph-override)" in message
        for message in log_messages
    )

    assert outcome.model.source == "graph-override"
    deps_text = (outcome.project_dir / "src/swarm_workflow/deps.py").read_text()
    # inherit/routes.py renders the emitter's DEFAULT_MODEL literal with
    # repr(), so quote style follows the model id.
    assert f"DEFAULT_MODEL: str = {f'{_OVERRIDE_PROVIDER}:{_OVERRIDE_MODEL}'!r}" in deps_text


async def test_unknown_override_provider_is_refused_at_phase_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fact 23: a route with no PydanticAI counterpart is refused at
    Phase 1 naming the provider, before any scaffolding."""
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    graph = linear_chat_graph()
    graph = graph.model_copy(
        update={
            "model": ModelSelection(
                provider="no-such-provider", model="some-model", reasoning_effort=None
            )
        }
    )
    project_dir = tmp_path / "unmappable"

    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))

    with pytest.raises(PhaseFailureError) as excinfo:
        await run_compile(
            graph,
            registry=registry,
            compile_id=compile_id,
            project_dir=project_dir,
            dsh_home=tmp_path / "dsh_home",
            uv_cache_dir=UV_CACHE_DIR,
        )

    job = registry.get(compile_id)
    assert job.status == "failed"
    assert excinfo.value.failure.phase == PHASE_REVIEW
    assert "no-such-provider" in excinfo.value.failure.message
    assert not project_dir.exists()
    assert _phases(job) == [(PHASE_REVIEW, "started"), (PHASE_REVIEW, "failed")]


@pytest.mark.slow
async def test_missing_filler_without_fake_fill_fails_with_an_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Group 5 has not landed: with ``SWARM_FAKE_FILL`` unset and nothing
    injected, Phase 3 fails immediately, names the seam, and tells the
    caller how to proceed. Phase 2 has already run, so the project stays
    on disk."""
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    graph = linear_chat_graph()
    project_dir = tmp_path / "no-filler"

    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))

    with pytest.raises(PhaseFailureError) as excinfo:
        await run_compile(
            graph,
            registry=registry,
            compile_id=compile_id,
            project_dir=project_dir,
            dsh_home=tmp_path / "dsh_home",
            uv_cache_dir=UV_CACHE_DIR,
        )

    job = registry.get(compile_id)
    assert job.status == "failed"
    assert excinfo.value.failure.phase == PHASE_FILL
    assert "SWARM_FAKE_FILL=1" in excinfo.value.failure.message

    assert _phases(job) == [
        (PHASE_REVIEW, "started"),
        (PHASE_REVIEW, "succeeded"),
        (PHASE_SCAFFOLD, "started"),
        (PHASE_SCAFFOLD, "succeeded"),
        (PHASE_FILL, "started"),
        (PHASE_FILL, "failed"),
    ]
    event_type, payload = _terminal_event(job)
    assert event_type == "error"
    assert payload["code"] == "phase_failed"

    # The project is deliberately left for inspection.
    assert project_dir.is_dir()
    assert (project_dir / "pyproject.toml").exists()


# ---------------------------------------------------------------------------
# 4. The retry policy, with injected fillers
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_phase_five_failure_then_success_retries_fill_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-then-succeed retries exactly once and ends ``succeeded``,
    with the phase-5 failure text handed to the retry as
    ``previous_failure``."""
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    graph = POSITIVE_FIXTURES["linear_chat"]()
    seen: list[str | None] = []

    async def flaky_filler(
        *,
        project_dir: Path,
        graph: SwarmGraph,
        resolved_model: ResolvedModel,
        live_model: object | None,
        previous_failure: str | None,
    ) -> FillResult:
        del resolved_model, live_model
        seen.append(previous_failure)
        if previous_failure is None:
            # Leave a permitted file's body region empty: a genuine
            # Phase-4 violation the retry must be asked to fix.
            step_path = project_dir / "src/swarm_workflow/steps/intake.py"
            step_path.write_text(_splice_region(step_path.read_text(), "intake", ""))
            return FillResult(filled_node_ids=())
        return FillResult(filled_node_ids=apply_fake_fill(project_dir, graph))

    job, outcome = await _run(
        graph, tmp_path, monkeypatch, project_name="flaky", filler=flaky_filler
    )

    assert job.status == "succeeded"
    assert outcome.attempts == 2
    assert len(seen) == 2
    assert seen[0] is None
    assert seen[1] is not None
    assert "empty_body_region" in seen[1]

    assert _phases(job) == [
        (PHASE_REVIEW, "started"),
        (PHASE_REVIEW, "succeeded"),
        (PHASE_SCAFFOLD, "started"),
        (PHASE_SCAFFOLD, "succeeded"),
        (PHASE_FILL, "started"),
        (PHASE_FILL, "succeeded"),
        (PHASE_BOUNDARY, "started"),
        (PHASE_BOUNDARY, "failed"),
        (PHASE_FILL, "started"),
        (PHASE_FILL, "succeeded"),
        (PHASE_BOUNDARY, "started"),
        (PHASE_BOUNDARY, "succeeded"),
        (PHASE_VALIDATE, "started"),
        (PHASE_VALIDATE, "succeeded"),
    ]
    event_type, payload = _terminal_event(job)
    assert event_type == "done"
    assert payload["attempts"] == 2


@pytest.mark.slow
async def test_permanent_failure_gives_up_after_exactly_two_fill_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filler that always breaks the boundary produces exactly two fill
    attempts, then a reported failure -- not an endless retry."""
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    graph = POSITIVE_FIXTURES["linear_chat"]()
    filler, calls = _make_always_fail_filler()

    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))

    with pytest.raises(PhaseFailureError) as excinfo:
        await run_compile(
            graph,
            registry=registry,
            compile_id=compile_id,
            project_dir=tmp_path / "always-fail",
            dsh_home=tmp_path / "dsh_home",
            filler=filler,
            uv_cache_dir=UV_CACHE_DIR,
        )

    job = registry.get(compile_id)
    assert job.status == "failed"
    assert len(calls) == 2, f"expected exactly 2 fill attempts, got {len(calls)}"
    assert calls[0] is None
    assert calls[1] is not None
    assert excinfo.value.failure.phase == PHASE_BOUNDARY

    starts = [
        payload["name"]
        for event_type, payload in _events(job)
        if event_type == "phase" and payload["status"] == "started"
    ]
    assert starts.count(PHASE_FILL) == 2
    assert starts.count(PHASE_BOUNDARY) == 2
    assert PHASE_VALIDATE not in starts

    event_type, payload = _terminal_event(job)
    assert event_type == "error"
    assert payload["code"] == "phase_failed"
    assert "boundary check found" in str(payload["message"])


@pytest.mark.slow
async def test_raised_fill_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fill that raises aborts immediately: the retry policy reacts
    only to a phase-4 or phase-5 failure."""
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    graph = linear_chat_graph()
    attempts: list[int] = []

    async def exploding_filler(
        *,
        project_dir: Path,
        graph: SwarmGraph,
        resolved_model: ResolvedModel,
        live_model: object | None,
        previous_failure: str | None,
    ) -> FillResult:
        del project_dir, graph, resolved_model, live_model, previous_failure
        attempts.append(1)
        raise RuntimeError("model returned garbage")

    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))

    with pytest.raises(PhaseFailureError) as excinfo:
        await run_compile(
            graph,
            registry=registry,
            compile_id=compile_id,
            project_dir=tmp_path / "exploding",
            dsh_home=tmp_path / "dsh_home",
            filler=exploding_filler,
            uv_cache_dir=UV_CACHE_DIR,
        )

    job = registry.get(compile_id)
    assert job.status == "failed"
    assert len(attempts) == 1
    assert excinfo.value.failure.phase == PHASE_FILL
    assert "model returned garbage" in excinfo.value.failure.message

    starts = [
        payload["name"]
        for event_type, payload in _events(job)
        if event_type == "phase" and payload["status"] == "started"
    ]
    assert starts.count(PHASE_FILL) == 1
    assert PHASE_BOUNDARY not in starts


# ---------------------------------------------------------------------------
# 5. Cancellation
# ---------------------------------------------------------------------------


async def test_cancel_mid_run_ends_the_job_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``registry.cancel()`` cancels the pipeline's own task mid-fill; the
    job ends ``cancelled`` and the raised ``CancelledError`` is not
    swallowed into a failure."""
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    graph = linear_chat_graph()
    started = asyncio.Event()
    never = asyncio.Event()
    resumed = asyncio.Event()
    project_dir = tmp_path / "cancelled"

    async def wedged_filler(
        *,
        project_dir: Path,
        graph: SwarmGraph,
        resolved_model: ResolvedModel,
        live_model: object | None,
        previous_failure: str | None,
    ) -> FillResult:
        del project_dir, graph, resolved_model, live_model, previous_failure
        started.set()
        await never.wait()  # never set: holds the fill open
        # Only reached if cancellation did not actually interrupt the
        # await, which would make this test's premise false.
        resumed.set()
        return FillResult()

    registry = JobRegistry()
    compile_id = _register(registry, graph)
    job = registry.get(compile_id)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))

    task = asyncio.create_task(
        run_compile(
            graph,
            registry=registry,
            compile_id=compile_id,
            project_dir=project_dir,
            dsh_home=tmp_path / "dsh_home",
            filler=wedged_filler,
            uv_cache_dir=UV_CACHE_DIR,
        )
    )
    job.task = task
    await asyncio.wait_for(started.wait(), timeout=10)

    cancelled = registry.cancel(compile_id)

    assert cancelled is job
    assert job.status == "cancelled"
    with pytest.raises(asyncio.CancelledError):
        await task

    # The await really was interrupted, not merely reported as cancelled.
    assert not resumed.is_set()

    # Cancellation is not an error path and not a done path.
    terminal_types = [
        event_type for event_type, _ in _events(job) if event_type in ("done", "error")
    ]
    assert terminal_types == []
    assert _phases(job)[-1] == (PHASE_FILL, "started")
    # The project Phase 2 already scaffolded is left where it is.
    assert project_dir.is_dir()


async def test_direct_task_cancel_also_marks_the_job_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server shutdown cancels the raw task without going through
    ``registry.cancel``; the pipeline still leaves the job ``cancelled``
    rather than ``running``."""
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    graph = linear_chat_graph()
    started = asyncio.Event()

    async def wedged_filler(
        *,
        project_dir: Path,
        graph: SwarmGraph,
        resolved_model: ResolvedModel,
        live_model: object | None,
        previous_failure: str | None,
    ) -> FillResult:
        del project_dir, graph, resolved_model, live_model, previous_failure
        started.set()
        await asyncio.Event().wait()
        return FillResult()

    registry = JobRegistry()
    compile_id = _register(registry, graph)
    job = registry.get(compile_id)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))

    task = asyncio.create_task(
        run_compile(
            graph,
            registry=registry,
            compile_id=compile_id,
            project_dir=tmp_path / "shutdown",
            dsh_home=tmp_path / "dsh_home",
            filler=wedged_filler,
            uv_cache_dir=UV_CACHE_DIR,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=10)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert job.status == "cancelled"


# ---------------------------------------------------------------------------
# 6. Failure leaves the project on disk; recompile clears it
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_failed_compile_leaves_the_project_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Phase-5 validation failure keeps every scaffolded and filled
    file, so the user can inspect what the model produced."""
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    graph = POSITIVE_FIXTURES["linear_chat"]()
    project_dir = tmp_path / "failing"

    async def broken_filler(
        *,
        project_dir: Path,
        graph: SwarmGraph,
        resolved_model: ResolvedModel,
        live_model: object | None,
        previous_failure: str | None,
    ) -> FillResult:
        del resolved_model, live_model, previous_failure

        filled = apply_fake_fill(project_dir, graph)
        # Fill the exit agent's body through its own markers -- Phase 4
        # sees a legitimately filled project -- with a body that always
        # raises, so Phase 5's dry run fails and the compile is reported
        # failed with everything still on disk.
        step_path = project_dir / "src/swarm_workflow/steps/summarize.py"
        step_path.write_text(
            _splice_region(
                step_path.read_text(),
                "summarize",
                "    raise RuntimeError('broken on purpose')",
            )
        )
        return FillResult(filled_node_ids=filled)

    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))

    with pytest.raises(PhaseFailureError):
        await run_compile(
            graph,
            registry=registry,
            compile_id=compile_id,
            project_dir=project_dir,
            dsh_home=tmp_path / "dsh_home",
            filler=broken_filler,
            uv_cache_dir=UV_CACHE_DIR,
        )

    job = registry.get(compile_id)
    assert job.status == "failed"

    # Everything the compile produced is still there.
    assert project_dir.is_dir()
    for rel_path in (
        "pyproject.toml",
        "src/swarm_workflow/graph.py",
        "src/swarm_workflow/steps/summarize.py",
        "validate/dry_run.py",
        "validate/golden_render.txt",
    ):
        assert (project_dir / rel_path).exists(), rel_path

    event_type, payload = _terminal_event(job)
    assert event_type == "error"
    assert payload["code"] == "phase_failed"


@pytest.mark.slow
async def test_recompile_clears_the_previous_projects_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recompile rescaffolds from scratch: a stale file from the first
    compile must not make the second one fail its boundary check."""
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    graph = POSITIVE_FIXTURES["linear_chat"]()
    project_dir = tmp_path / "recompile"
    project_dir.mkdir()
    (project_dir / "stale_from_a_previous_compile.py").write_text("# stale\n")

    job, outcome = await _run(graph, tmp_path, monkeypatch, project_name="recompile")

    assert job.status == "succeeded"
    assert not (project_dir / "stale_from_a_previous_compile.py").exists()
    assert (outcome.project_dir / "pyproject.toml").exists()

    log_messages = [
        str(payload["message"]) for event_type, payload in _events(job) if event_type == "log"
    ]
    assert any("recompile: clearing existing project" in message for message in log_messages)


# ---------------------------------------------------------------------------
# 7. Fake-fill selection
# ---------------------------------------------------------------------------


async def test_fake_fill_env_var_overrides_an_injected_filler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SWARM_FAKE_FILL=1`` wins over an injected filler, so a test (or a
    developer) can always force the deterministic path."""
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    graph = POSITIVE_FIXTURES["linear_chat"]()

    async def exploding_filler(**kwargs: object) -> FillResult:
        raise AssertionError("the injected filler must not run when SWARM_FAKE_FILL=1")

    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))

    outcome = await run_compile(
        graph,
        registry=registry,
        compile_id=compile_id,
        project_dir=tmp_path / "fake-wins",
        dsh_home=tmp_path / "dsh_home",
        filler=exploding_filler,
        uv_cache_dir=UV_CACHE_DIR,
    )

    assert registry.get(compile_id).status == "succeeded"
    # Both of linear_chat's steps are programmatic, so both are filled.
    assert outcome.filled_node_ids == ("intake", "summarize")


def test_select_filler_reports_fill_unavailable_with_both_ways_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unavailability error is its own type and names both remedies."""
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    with pytest.raises(FillUnavailableError) as excinfo:
        _select_filler(None, dry_run=False)

    message = str(excinfo.value)
    assert "SWARM_FAKE_FILL=1" in message
    assert "compile/agent.py" in message
    assert "inject" in message


def test_select_filler_honours_the_frozen_decision_not_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filler is chosen from the decision ``run_compile`` froze at job
    start, never re-read here: a switch flipped mid-compile must not change
    which bodies the project gets."""

    async def injected_filler(**kwargs: object) -> FillResult:
        return FillResult()

    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    assert _select_filler(injected_filler, dry_run=False) is injected_filler

    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    assert _select_filler(injected_filler, dry_run=True) is fake_filler


async def test_fill_timeout_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged model cannot hang a compile forever: the fill is bounded
    by ``FILL_TIMEOUT_SECONDS``."""
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    monkeypatch.setattr("swarm_builder.compile.pipeline.FILL_TIMEOUT_SECONDS", 0.05)
    graph = linear_chat_graph()

    async def wedged_filler(**kwargs: object) -> FillResult:
        await asyncio.Event().wait()
        return FillResult()

    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))

    with pytest.raises(PhaseFailureError) as excinfo:
        await run_compile(
            graph,
            registry=registry,
            compile_id=compile_id,
            project_dir=tmp_path / "wedged",
            dsh_home=tmp_path / "dsh_home",
            filler=wedged_filler,
            uv_cache_dir=UV_CACHE_DIR,
        )

    assert excinfo.value.failure.phase == PHASE_FILL
    assert "did not finish within" in excinfo.value.failure.message
    assert registry.get(compile_id).status == "failed"


async def test_error_event_identifies_the_failing_phase_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``error`` event says why, and the preceding ``phase`` event
    says where: a client reads the phase list for the failing phase and
    the error event for the reason."""
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    graph = linear_chat_graph()
    registry = JobRegistry()
    compile_id = _register(registry, graph)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    monkeypatch.setenv("SWARM_FAKE_FILL", "0")  # any value other than "1" is off

    with pytest.raises(PhaseFailureError):
        await run_compile(
            graph,
            registry=registry,
            compile_id=compile_id,
            project_dir=tmp_path / "code",
            dsh_home=tmp_path / "dsh_home",
            uv_cache_dir=UV_CACHE_DIR,
        )

    job = registry.get(compile_id)
    event_type, payload = _terminal_event(job)
    assert event_type == "error"
    assert payload["code"] == ERROR_CODE_PHASE_FAILED
    assert payload["exception"] == "PhaseFailureError"
    assert "SWARM_FAKE_FILL=1" in str(payload["message"])

    failed_phases = [
        event_payload
        for event_type, event_payload in _events(job)
        if event_type == "phase" and event_payload["status"] == "failed"
    ]
    assert [event_payload["name"] for event_payload in failed_phases] == [PHASE_FILL]
    assert failed_phases[0]["details"]["hint"] == "SWARM_FAKE_FILL=1"
