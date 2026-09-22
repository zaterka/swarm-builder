"""The LangGraph compile target (``compile/langgraph``).

Fast tests cover the deterministic emitters (wiring, node shells, state
reducers, model mapping, the conversion prompt, the stub converter) and
the request/route plumbing. The slow tests are the proof: a full
``target="langgraph"`` compile under ``SWARM_FAKE_FILL=1`` -- five
standard phases, then scaffold/convert/boundary/validate of the LangGraph
export -- for a fan-out graph and a decision graph, in-process and over
HTTP, ending with a passing keyless LangGraph dry run.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fixtures.graphs import (
    decision_branching_graph,
    fanout_join_graph,
    json_ports_graph,
    linear_chat_graph,
    mixed_programmatic_graph,
    orchestrator_graph,
    websearch_graph,
)
from swarm_builder.compile.boundary import capture_baseline, check_boundary
from swarm_builder.compile.graph_ir import analyze
from swarm_builder.compile.jobs import JobRegistry
from swarm_builder.compile.langgraph import (
    LANGGRAPH_PHASE_NAMES,
    NODES_DIR_PARTS,
    PACKAGE_NAME,
    TARGET_LANGGRAPH,
)
from swarm_builder.compile.langgraph.convert import build_convert_instructions
from swarm_builder.compile.langgraph.fake_convert import apply_fake_convert, stub_langgraph_body
from swarm_builder.compile.langgraph.models import to_langchain_model_source
from swarm_builder.compile.langgraph.scaffold import (
    _render_state,
    emit_langgraph,
    fake_reply_for,
    render_node_module,
    scaffold_langgraph,
)
from swarm_builder.compile.pipeline import (
    ALL_PHASE_NAMES,
    PHASE_NAMES,
    PHASE_STATUS_STARTED,
    PHASE_STATUS_SUCCEEDED,
    run_compile,
)
from swarm_builder.inherit.routes import UnmappableRouteError
from swarm_builder.inherit.settings import EffectiveModel, RouteConfig
from swarm_builder.main import create_app
from swarm_builder.models import (
    AgentSpec,
    BranchEdge,
    DecisionBranch,
    DecisionSpec,
    JoinEdge,
    JoinSpec,
    NodeIo,
    Position,
    ProgrammaticSpec,
    SeqEdge,
    StateField,
    SwarmGraph,
    SwarmNode,
)
from swarm_builder.routes import compile as compile_routes

REPO_ROOT = Path(__file__).resolve().parent.parent
UV_CACHE_DIR = REPO_ROOT / ".uv-cache"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    monkeypatch.setenv("UV_CACHE_DIR", str(UV_CACHE_DIR))
    monkeypatch.delenv("SWARM_MODEL", raising=False)
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)
    monkeypatch.delenv("SWARM_API_KEY_ENV", raising=False)
    return tmp_path / "workspace"


@pytest.fixture(autouse=True)
def fresh_registry() -> Iterator[None]:
    compile_routes._reset_registry()
    yield
    compile_routes._reset_registry()


def _effective(
    provider: str = "deepseek-official", model: str = "deepseek-flash"
) -> EffectiveModel:
    return EffectiveModel(
        provider=provider,
        model=model,
        reasoning_effort=None,
        base_url=None,
        api_key_env=None,
        source="settings-default",
        route=None,
    )


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------


def test_graph_wiring_for_a_decision_uses_conditional_edges() -> None:
    source = emit_langgraph(decision_branching_graph())
    assert "StateGraph(State, context_schema=Context)" in source
    assert 'builder.add_node("decision", decision)' in source
    assert re.search(
        r'add_conditional_edges\("decision", route_decision, \{"big": "big", "small": "small"\}\)',
        source,
    )
    assert 'builder.add_edge(START, "classify")' in source
    assert 'builder.add_edge("big", END)' in source and 'builder.add_edge("small", END)' in source
    # A decision never gets an END edge of its own.
    assert 'add_edge("decision", END)' not in source


def test_graph_wiring_for_a_fanout_has_one_edge_per_arm_and_a_list_fan_in() -> None:
    source = emit_langgraph(fanout_join_graph())
    assert 'builder.add_edge("split", "left")' in source
    assert 'builder.add_edge("split", "right")' in source
    assert 'builder.add_edge(["left", "right"], "join")' in source


def test_delegate_only_children_are_not_graph_nodes() -> None:
    graph = orchestrator_graph()
    source = emit_langgraph(graph)
    structure = analyze(graph)
    for child in structure.delegate_only_node_ids:
        assert f'add_node("{child}"' not in source
        # ...but the orchestrator imports the child's body for its tool.
    orchestrator = next(n for n in graph.nodes if n.agent and n.agent.delegates_to)
    module = render_node_module(graph, structure, orchestrator)
    for child in orchestrator.agent.delegates_to:  # type: ignore[union-attr]
        assert f"from {PACKAGE_NAME}.nodes.{child} import {child}_body" in module
        assert f"async def {child}(query: str) -> str:" in module
    assert "model.bind_tools(" in module
    assert "create_react_agent" not in module and "langchain.agents" not in module


def test_fanout_arms_write_the_join_inbox_not_payload() -> None:
    graph = fanout_join_graph()
    structure = analyze(graph)
    left = next(n for n in graph.nodes if n.id == "left")
    module = render_node_module(graph, structure, left)
    assert 'return {**writes, "join_inbox": [output]}' in module
    join = next(n for n in graph.nodes if n.id == "join")
    join_module = render_node_module(graph, structure, join)
    assert 'state.get("join_inbox")' in join_module
    state = _render_state(graph)
    assert "join_inbox: Annotated[list[Any], operator.add]" in state
    assert '"join_inbox": [],' in state


def test_state_declares_canvas_fields_with_their_defaults() -> None:
    state = _render_state(linear_chat_graph())
    assert "    topic: str" in state
    assert '"topic": "",' in state
    assert "payload: Any" in state


def test_programmatic_shell_carries_the_unconverted_sentinel() -> None:
    graph = mixed_programmatic_graph()
    structure = analyze(graph)
    fetch = next(n for n in graph.nodes if n.kind == "programmatic")
    module = render_node_module(graph, structure, fetch)
    assert "unconverted node body" in module
    assert f"async def {fetch.id}_body(" in module
    assert f"# --- swarm:begin {fetch.id} ---" in module


def test_fake_reply_takes_a_decision_branch_when_an_agent_feeds_it() -> None:
    assert fake_reply_for(linear_chat_graph()) == "fake reply"
    # The decision fixture's classifier is programmatic, so the default stays.
    assert fake_reply_for(decision_branching_graph()) == "fake reply"


def test_stub_body_honours_writes_and_decision_matches() -> None:
    graph = decision_branching_graph()
    classify = next(n for n in graph.nodes if n.id == "classify")
    body = stub_langgraph_body(graph, classify)
    assert body.endswith("return 'big'")
    intake = next(n for n in linear_chat_graph().nodes if n.id == "intake")
    body = stub_langgraph_body(linear_chat_graph(), intake)
    assert 'writes["topic"] = inputs' in body and "return str(inputs)" in body


def test_node_modules_survive_quotes_in_intents() -> None:
    """Found by a real-model compile: a generated intent with an apostrophe
    produced a docstring ending in four quotes and a SyntaxError."""
    graph = decision_branching_graph()
    structure = analyze(graph)
    for node in graph.nodes:
        for intent in (
            "Route the ticket based on the classifier's category.",
            "Say \"hello\" and 'bye' with a backslash \\ and triple ''' quotes.",
        ):
            source = render_node_module(
                graph, structure, node.model_copy(update={"intent": intent})
            )
            compile(source, f"{node.id}.py", "exec")


# ---------------------------------------------------------------------------
# Model mapping and prompt
# ---------------------------------------------------------------------------


def test_known_name_route_maps_to_init_chat_model() -> None:
    source = to_langchain_model_source(_effective())
    assert source.dependency == "langchain-deepseek==1.1.1"
    assert "DEFAULT_MODEL_PROVIDER: str = 'deepseek'" in source.helper_source
    assert "init_chat_model(" in source.helper_source
    assert source.env_lines == ("SWARM_MODEL=deepseek-flash", "SWARM_MODEL_PROVIDER=deepseek")


def test_base_url_route_maps_to_chat_openai() -> None:
    route = RouteConfig(
        key="kornerstone",
        api="openai-completions",
        base_url="http://localhost:8000/v1",
        api_key_env="KORNERSTONE_API_KEY",
        aws_profile=None,
        aws_region=None,
        models=(),
    )
    effective = EffectiveModel(
        provider="kornerstone",
        model="qwen38-27b-fp8",
        reasoning_effort=None,
        base_url="http://localhost:8000/v1",
        api_key_env="KORNERSTONE_API_KEY",
        source="settings-default",
        route=route,
    )
    source = to_langchain_model_source(effective)
    assert source.dependency == "langchain-openai==1.6.3"
    assert "ChatOpenAI(" in source.helper_source
    assert "SWARM_BASE_URL=http://localhost:8000/v1" in source.env_lines


def test_unknown_provider_is_refused_for_langgraph() -> None:
    with pytest.raises(UnmappableRouteError):
        to_langchain_model_source(_effective(provider="mystery-provider", model="x"))


def test_convert_prompt_states_the_contract_and_targets(tmp_path: Path) -> None:
    graph = decision_branching_graph()
    text = build_convert_instructions(
        tmp_path / "lg", tmp_path / "src", graph, model_description="on test model"
    )
    assert "read_source_file" in text
    assert "src/swarm_workflow/steps/classify.py" in text
    assert f"src/{PACKAGE_NAME}/nodes/classify.py" in text
    assert 'writes["<field>"] = value' in text
    assert "Do not import `pydantic_ai`" in text
    assert "create_react_agent" in text  # named as forbidden
    assert "MUST be one of 'big', 'small'" in text
    assert "1.2.12" in text and "1.4.2" in text


# ---------------------------------------------------------------------------
# Pipeline plumbing
# ---------------------------------------------------------------------------


async def test_run_compile_refuses_unknown_target(tmp_path: Path) -> None:
    registry = JobRegistry()
    registry.register("c1", "linear-chat")
    with pytest.raises(ValueError, match="unknown compile target"):
        await run_compile(
            linear_chat_graph(),
            registry=registry,
            compile_id="c1",
            project_dir=tmp_path / "p",
            dsh_home=tmp_path / "dsh",
            target="crewai",
        )
    registry.register("c2", "linear-chat-2")
    with pytest.raises(ValueError, match="requires langgraph_project_dir"):
        await run_compile(
            linear_chat_graph().model_copy(update={"id": "linear-chat-2"}),
            registry=registry,
            compile_id="c2",
            project_dir=tmp_path / "p2",
            dsh_home=tmp_path / "dsh",
            target=TARGET_LANGGRAPH,
        )


def test_all_phase_names_extend_the_standard_five() -> None:
    assert ALL_PHASE_NAMES == (*PHASE_NAMES, *LANGGRAPH_PHASE_NAMES)
    assert LANGGRAPH_PHASE_NAMES == ("lg_scaffold", "lg_convert", "lg_boundary", "lg_validate")


def test_export_route_knows_the_langgraph_target(workspace: Path) -> None:
    _ = workspace
    client = TestClient(create_app())
    graph = linear_chat_graph()
    client.put(f"/api/graphs/{graph.id}", json=json.loads(graph.model_dump_json(by_alias=True)))
    response = client.get(f"/api/graphs/{graph.id}/export?target=langgraph")
    assert response.status_code == 404
    assert "langgraph" in response.json()["detail"]
    assert "projects-langgraph" in response.json()["detail"]
    assert client.get(f"/api/graphs/{graph.id}/export?target=crewai").status_code == 422


def test_start_compile_validates_target(workspace: Path) -> None:
    _ = workspace
    client = TestClient(create_app())
    graph = linear_chat_graph()
    client.put(f"/api/graphs/{graph.id}", json=json.loads(graph.model_dump_json(by_alias=True)))
    response = client.post("/api/compile", json={"graphId": graph.id, "target": "crewai"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Fast keyless gate: scaffold + stub-convert + boundary + dry run, executed
# with the SERVER's interpreter (which has langgraph/langchain installed) via
# PYTHONPATH, so no `uv sync` is needed. Same assertions as Phase 9.
# ---------------------------------------------------------------------------


def _triage_graph() -> SwarmGraph:
    """The shape a real-model generate produced: branches whose match values
    differ from their target ids, converging on a join with no fan-out, then a
    final step. Both details broke the first emitter."""
    from datetime import UTC, datetime

    def io(i: str = "str", o: str = "str") -> NodeIo:
        return NodeIo(input_type=i, output_type=o)  # type: ignore[arg-type]

    nodes = [
        SwarmNode(
            id="classify",
            kind="programmatic",
            title="Classify",
            intent="Return 'billing' or 'technical'.",
            position=Position(x=0, y=0),
            io=io(),
            writes=["ticket_text"],
            programmatic=ProgrammaticSpec(),
        ),
        SwarmNode(
            id="route",
            kind="decision",
            title="Route",
            intent="Route.",
            position=Position(x=1, y=0),
            io=io(),
            decision=DecisionSpec(
                branches=[
                    DecisionBranch(match="billing", target_node_id="billing_agent"),
                    DecisionBranch(match="technical", target_node_id="technical_agent"),
                ]
            ),
        ),
        SwarmNode(
            id="billing_agent",
            kind="agent",
            title="Billing agent",
            intent="Explain refunds.",
            position=Position(x=2, y=0),
            io=io(),
            reads=["ticket_text"],
            agent=AgentSpec(instructions="Explain the refund policy."),
        ),
        SwarmNode(
            id="technical_agent",
            kind="agent",
            title="Technical agent",
            intent="Suggest a fix.",
            position=Position(x=2, y=1),
            io=io(),
            reads=["ticket_text"],
            agent=AgentSpec(instructions="Suggest a fix."),
        ),
        SwarmNode(
            id="merge",
            kind="join",
            title="Merge",
            intent="Merge drafts.",
            position=Position(x=3, y=0),
            io=io("str", "list[str]"),
            join=JoinSpec(reducer="list_append"),
        ),
        SwarmNode(
            id="final_reply",
            kind="agent",
            title="Final reply",
            intent="Write the reply.",
            position=Position(x=4, y=0),
            io=io("list[str]", "str"),
            agent=AgentSpec(instructions="Write a two-sentence reply."),
        ),
    ]
    edges = [
        SeqEdge(kind="seq", id="e1", source="classify", target="route"),
        BranchEdge(kind="branch", id="e2", source="route", target="billing_agent", match="billing"),
        BranchEdge(
            kind="branch", id="e3", source="route", target="technical_agent", match="technical"
        ),
        JoinEdge(kind="join", id="e4", source="billing_agent", target="merge"),
        JoinEdge(kind="join", id="e5", source="technical_agent", target="merge"),
        SeqEdge(kind="seq", id="e6", source="merge", target="final_reply"),
    ]
    return SwarmGraph(
        id="triage",
        name="Triage",
        entry_node_id="classify",
        exit_node_id="final_reply",
        state_fields=[StateField(name="ticket_text", type="str", default='""')],
        nodes=nodes,
        edges=edges,
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _run_in_server_venv(project_dir: Path, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, script],
        cwd=project_dir,
        env={"PYTHONPATH": str(project_dir / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "make_graph",
    [
        linear_chat_graph,
        websearch_graph,
        orchestrator_graph,
        fanout_join_graph,
        decision_branching_graph,
        mixed_programmatic_graph,
        json_ports_graph,
        _triage_graph,
    ],
)
def test_exported_project_passes_its_dry_run_in_the_server_venv(tmp_path: Path, make_graph) -> None:
    graph = make_graph()
    source = to_langchain_model_source(_effective())
    scaffold_langgraph(graph, tmp_path, source)
    baseline = capture_baseline(tmp_path, permitted_dirs=("/".join(NODES_DIR_PARTS),))
    apply_fake_convert(tmp_path, graph)
    check_boundary(tmp_path, baseline)
    result = _run_in_server_venv(tmp_path, "validate/dry_run.py")
    assert result.returncode == 0, result.stderr[-2500:]
    assert "ALL CHECKS PASSED" in result.stdout
    # The exit node really executed (the dry run asserts it; double-check here).
    assert graph.exit_node_id in result.stdout


def test_agent_nodes_see_the_state_fields_they_read() -> None:
    graph = _triage_graph()
    structure = analyze(graph)
    billing = next(n for n in graph.nodes if n.id == "billing_agent")
    module = render_node_module(graph, structure, billing)
    assert 'state.get(\'ticket_text\')' in module
    assert "Context from state" in module
    assert "HumanMessage(user_text)" in module
    final = next(n for n in graph.nodes if n.id == "final_reply")
    assert "user_text = str(inputs)" in render_node_module(graph, structure, final)


def test_decision_join_shape_reaches_the_final_node(tmp_path: Path) -> None:
    """Regression for the real-model compile: only one branch runs, so the
    join must fire on that branch alone and the final node must run."""
    graph = _triage_graph()
    wiring = emit_langgraph(graph)
    assert 'builder.add_edge("billing_agent", "merge")' in wiring
    assert 'builder.add_edge("technical_agent", "merge")' in wiring
    assert 'add_edge(["billing_agent", "technical_agent"], "merge")' not in wiring
    # Fan-outs still wait for every arm.
    assert 'builder.add_edge(["left", "right"], "join")' in emit_langgraph(fanout_join_graph())
    # The routing function returns the match value, never the node name.
    structure = analyze(graph)
    route = next(n for n in graph.nodes if n.id == "route")
    module = render_node_module(graph, structure, route)
    assert "return value" in module and "return ROUTES[" not in module


# ---------------------------------------------------------------------------
# End to end (slow): the whole nine-phase compile, keyless
# ---------------------------------------------------------------------------


def _phase_sequence(registry: JobRegistry, compile_id: str) -> list[tuple[str, str]]:
    return [
        (str(e.payload["name"]), str(e.payload["status"]))
        for e in registry.get(compile_id).events_after(None)
        if e.event_type == "phase"
    ]


@pytest.mark.slow
@pytest.mark.parametrize("make_graph", [fanout_join_graph, decision_branching_graph])
async def test_langgraph_target_compiles_and_validates_keyless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_graph
) -> None:
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    graph: SwarmGraph = make_graph()
    registry = JobRegistry()
    registry.register("c1", graph.id)
    outcome = await run_compile(
        graph,
        registry=registry,
        compile_id="c1",
        project_dir=tmp_path / "projects" / graph.id,
        dsh_home=tmp_path / "dsh_home",
        uv_cache_dir=UV_CACHE_DIR,
        target=TARGET_LANGGRAPH,
        langgraph_project_dir=tmp_path / "projects-langgraph" / graph.id,
    )
    assert outcome.langgraph is not None
    lg = outcome.langgraph
    assert lg.project_dir.is_dir()
    assert (lg.project_dir / "src" / PACKAGE_NAME / "graph.py").is_file()
    assert (lg.project_dir / ".venv").is_dir()  # the gate really ran uv sync
    assert lg.diagram.startswith("---\nconfig:")
    assert lg.attempts == 1
    programmatic = sorted(n.id for n in graph.nodes if n.kind == "programmatic")
    assert sorted(lg.converted_node_ids) == programmatic

    expected = [
        (name, status)
        for name in ALL_PHASE_NAMES
        for status in (PHASE_STATUS_STARTED, PHASE_STATUS_SUCCEEDED)
    ]
    assert _phase_sequence(registry, "c1") == expected
    job = registry.get("c1")
    assert job.status == "succeeded"
    assert job.result.value is not None
    payload = job.result.value["langgraph"]
    assert payload["projectPath"] == str(lg.project_dir)
    assert payload["convertedNodeIds"] == list(lg.converted_node_ids)
    # Every phase frame of this compile reports the nine-phase total.
    totals = {e.payload["total"] for e in job.events_after(None) if e.event_type == "phase"}
    assert totals == {len(ALL_PHASE_NAMES)}


@pytest.mark.slow
def test_langgraph_target_over_http_and_export(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = workspace
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    graph = mixed_programmatic_graph()
    with TestClient(create_app()) as client:
        client.put(f"/api/graphs/{graph.id}", json=json.loads(graph.model_dump_json(by_alias=True)))
        start = client.post("/api/compile", json={"graphId": graph.id, "target": "langgraph"})
        assert start.status_code == 200, start.text
        compile_id = start.json()["compileId"]
        status = client.get(f"/api/compile/{compile_id}").json()
        polls = 0
        while status["status"] in ("queued", "running") and polls < 4000:
            status = client.get(f"/api/compile/{compile_id}").json()
            polls += 1
        assert status["status"] == "succeeded", status
        assert "langgraph" in status["result"]

        export = client.get(f"/api/graphs/{graph.id}/export?target=langgraph")
        assert export.status_code == 200, export.text
        assert export.json()["target"] == "langgraph"
        assert export.json()["projectPath"] == status["result"]["langgraph"]["projectPath"]
        # The default export is still the pydantic-graph project.
        default = client.get(f"/api/graphs/{graph.id}/export").json()
        assert default["target"] == "pydantic-graph"
        assert default["projectPath"] != export.json()["projectPath"]
