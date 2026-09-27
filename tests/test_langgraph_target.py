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
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import ModelRetry

from fixtures.graphs import (
    database_agent_tool_graph,
    db_tool_unknown_node_graph,
    db_write_as_tool_graph,
    decision_branching_graph,
    fanout_join_graph,
    json_ports_graph,
    linear_chat_graph,
    mixed_programmatic_graph,
    nosql_query_graph,
    orchestrator_graph,
    sql_lookup_graph,
    two_sql_nodes_same_seed_graph,
    vector_search_graph,
    websearch_graph,
)
from swarm_builder.compile import imports_marker_end
from swarm_builder.compile.agent import FillSession
from swarm_builder.compile.boundary import capture_baseline, check_boundary
from swarm_builder.compile.fake_fill import FILLABLE_NODE_KINDS
from swarm_builder.compile.graph_ir import analyze
from swarm_builder.compile.jobs import JobRegistry
from swarm_builder.compile.langgraph import (
    FORBIDDEN_DIRECTORIES,
    FORBIDDEN_FILES,
    LANGGRAPH_PHASE_NAMES,
    NODES_DIR_PARTS,
    PACKAGE_NAME,
    PINNED_LANGGRAPH_VERSION,
    REPOSITORIES_DIR,
    TARGET_LANGGRAPH,
)
from swarm_builder.compile.langgraph.convert import (
    _convert_targets,
    build_convert_instructions,
)
from swarm_builder.compile.langgraph.fake_convert import (
    _RETURN_EXPRESSIONS,
    apply_fake_convert,
    stub_langgraph_body,
)
from swarm_builder.compile.langgraph.models import to_langchain_model_source
from swarm_builder.compile.langgraph.scaffold import (
    UNCONVERTED_BODY_SENTINEL,
    _render_dry_run,
    _render_env_example,
    _render_pyproject,
    _render_readme,
    _render_state,
    database_tool_name,
    database_tool_targets,
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
    PortType,
    Position,
    ProgrammaticSpec,
    SeqEdge,
    StateField,
    SwarmGraph,
    SwarmNode,
)
from swarm_builder.routes import compile as compile_routes
from swarm_builder.templates.database import get_database_entry

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
        # The database kinds: their node bodies are complete as emitted, so
        # scaffolding plus the stub conversion of their programmatic consumers
        # is enough to run the whole project keylessly.
        sql_lookup_graph,
        nosql_query_graph,
        vector_search_graph,
        database_agent_tool_graph,
        two_sql_nodes_same_seed_graph,
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
# Database nodes (PLAN-DB-NODES.md §4.4b): the LangGraph half of the feature
#
# Everything here is the fast, deterministic half: node-module rendering per
# kind, the repository tree under this target's own package name, the extras /
# env / README gating, the agent tool binding, the conversion-target
# regression, and the `list[json]` ripple. `tests/test_db_codegen.py` (Task G)
# owns the slow `uv sync` gates.
# ---------------------------------------------------------------------------

#: fixture -> (database node id, repository getter, seed suffix)
_DATABASE_FIXTURES = [
    (sql_lookup_graph, "orders_db", "get_sql_repository", "sql"),
    (nosql_query_graph, "tickets", "get_document_repository", "json"),
    (vector_search_graph, "product_docs", "get_vector_repository", "json"),
]


def _database_node(graph: SwarmGraph, node_id: str) -> SwarmNode:
    return next(node for node in graph.nodes if node.id == node_id)


def _module_for(graph: SwarmGraph, node_id: str) -> str:
    """The rendered node module for ``node_id``, parsed (so syntax fails loudly)."""
    source = render_node_module(graph, analyze(graph), _database_node(graph, node_id))
    compile(source, f"{node_id}.py", "exec")
    return source


def _body_region(source: str, node_id: str) -> str:
    """The lines between ``node_id``'s body markers (what lg_boundary protects)."""
    begin = f"# --- swarm:begin {node_id} ---"
    end = f"# --- swarm:end {node_id} ---"
    return source.split(begin, 1)[1].split(end, 1)[0].strip("\n")


def _database_spec_of(graph: SwarmGraph, node_id: str):
    node = _database_node(graph, node_id)
    return {"sql": node.sql, "nosql": node.nosql, "vector": node.vector}[node.kind]


def _rewrite_spec(graph: SwarmGraph, node_id: str, **changes: object) -> SwarmGraph:
    """A copy of ``graph`` whose ``node_id`` spec carries ``changes``."""
    nodes = []
    for node in graph.nodes:
        if node.id != node_id:
            nodes.append(node)
            continue
        spec = _database_spec_of(graph, node_id)
        assert spec is not None
        nodes.append(node.model_copy(update={node.kind: spec.model_copy(update=changes)}))
    return graph.model_copy(update={"nodes": nodes})


def _rewrite_io(graph: SwarmGraph, node_id: str, input_type: str, output_type: str) -> SwarmGraph:
    """A copy of ``graph`` whose ``node_id`` declares a different I/O pair."""
    nodes = [
        node.model_copy(
            update={"io": NodeIo(input_type=input_type, output_type=output_type)}  # type: ignore[arg-type]
        )
        if node.id == node_id
        else node
        for node in graph.nodes
    ]
    return graph.model_copy(update={"nodes": nodes})


def _rewrite_node(graph: SwarmGraph, node_id: str, **updates: object) -> SwarmGraph:
    """A copy of ``graph`` with ``updates`` applied to ``node_id``."""
    nodes = [
        node.model_copy(update=updates) if node.id == node_id else node for node in graph.nodes
    ]
    return graph.model_copy(update={"nodes": nodes})


@pytest.mark.parametrize(("make_graph", "node_id", "getter", "suffix"), _DATABASE_FIXTURES)
def test_database_node_module_is_complete_and_repository_backed(
    make_graph, node_id: str, getter: str, suffix: str
) -> None:
    graph = make_graph()
    source = _module_for(graph, node_id)
    body = _body_region(source, node_id)
    # Complete, never the sentinel: no model call authored this module.
    assert UNCONVERTED_BODY_SENTINEL not in source
    assert body, "check_boundary refuses an empty body region"
    # The declared operation and its seed path are module constants, outside
    # the marker regions.
    assert (
        'SEED_PATH = Path(__file__).resolve().parent.parent / "repositories" / "seed" / '
        f'"{node_id}.{suffix}"' in source
    )
    assert "from pathlib import Path" in source
    # The body runs the declared operation through the factory's getter, and
    # coerces what it read into the port the document declares.
    assert f"{getter}(SEED_PATH" in body
    assert 'as_port(rows, "list[json]")' in body.rstrip()
    assert body.rstrip().endswith("return result")
    assert ") -> list[dict[str, Any]]:" in source
    # The getter is imported in the *generated* region, after the imports
    # marker: lg_convert owns the marker regions, and a database node is never
    # a conversion target, so an agent's write_region must not be able to
    # replace the import its own body needs.
    assert source.index(imports_marker_end(node_id)) < source.index(
        f"from {PACKAGE_NAME}.repositories import {getter}"
    )
    assert f"from {PACKAGE_NAME}.repositories.portshape import as_port" in source


def test_a_database_node_publishes_what_it_read_into_its_declared_state_fields() -> None:
    graph = _rewrite_node(sql_lookup_graph(), "orders_db", writes=["orders", "last_query"])
    body = _body_region(_module_for(graph, "orders_db"), "orders_db")
    assert 'writes["orders"] = result' in body
    assert 'writes["last_query"] = result' in body
    assert body.index('writes["orders"] = result') < body.index("return result")


@pytest.mark.parametrize(
    ("input_type", "expected"),
    [
        ("str", 'repo.query(SQL_QUERY, {"input": inputs})'),
        ("json", 'repo.query(SQL_QUERY, {**values, "input": values})'),
        ("list[str]", "sql, params = expand_list_param("),
    ],
)
def test_a_sql_node_binds_the_declared_input_type(input_type: str, expected: str) -> None:
    graph = _rewrite_io(sql_lookup_graph(), "orders_db", input_type, "list[json]")
    source = _module_for(graph, "orders_db")
    body = _body_region(source, "orders_db")
    assert expected in body
    if input_type == "list[str]":
        # One `:input` placeholder becomes one placeholder per value, and the
        # rewritten query text is what the repository is asked to run.
        expansion = f"expand_list_param(SQL_QUERY, as_port(inputs, {json.dumps(input_type)}))"
        assert expansion in body
        assert "rows = repo.query(sql, params)" in body
        # The one binding that rewrites the query text needs its helper.
        assert (
            f"from {PACKAGE_NAME}.repositories.portshape import as_port, expand_list_param"
            in source
        )
    else:
        assert "expand_list_param" not in source


def test_a_sql_write_node_calls_execute_and_reports_the_rows_it_affected() -> None:
    graph = _rewrite_spec(sql_lookup_graph(), "orders_db", write=True)
    body = _body_region(_module_for(graph, "orders_db"), "orders_db")
    assert 'affected = repo.execute(SQL_QUERY, {"input": inputs})' in body
    assert 'result = as_port([{"rows_affected": affected}], "list[json]")' in body
    assert "repo.query(" not in body


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("find", "rows = repo.find(bind_input_filter(FILTER, inputs), limit=LIMIT)"),
        ("find_one", "row = repo.find_one(bind_input_filter(FILTER, inputs))"),
        ("count", "count = repo.count(bind_input_filter(FILTER, inputs))"),
        (
            "insert_one",
            'document = inputs if isinstance(inputs, dict) else {"value": inputs}',
        ),
    ],
)
def test_a_nosql_node_runs_its_declared_operation(operation: str, expected: str) -> None:
    graph = _rewrite_spec(nosql_query_graph(), "tickets", operation=operation)
    source = _module_for(graph, "tickets")
    body = _body_region(source, "tickets")
    assert expected in body
    if operation == "insert_one":
        # A write takes its document from the input: no filter, so no sentinel.
        assert "bind_input_filter" not in source
        assert 'repo.insert_one(document)' in body
        assert 'result = as_port([{"id": inserted_id}], "list[json]")' in body
    else:
        # The binding rule is *shared*, not emitted locally: both targets call the
        # same function in the generated `repositories/portshape.py`, which is what
        # makes "the same document reads the same rows in either export" checkable.
        assert "bind_input_filter" in source
        assert f"from {PACKAGE_NAME}.repositories.portshape import" in source
        assert "_bind_input" not in source


def test_a_nosql_json_input_merges_over_the_filters_top_level() -> None:
    graph = _rewrite_io(nosql_query_graph(), "tickets", "json", "list[json]")
    body = _body_region(_module_for(graph, "tickets"), "tickets")
    assert 'values = as_port(inputs, "json")' in body
    assert "bind_input_filter({**FILTER, **values}, values)" in body


def test_a_vector_node_searches_the_collection_and_top_k_it_declares() -> None:
    graph = vector_search_graph()
    spec = _database_spec_of(graph, "product_docs")
    source = _module_for(graph, "product_docs")
    body = _body_region(source, "product_docs")
    assert "rows = repo.search(inputs, top_k=TOP_K, min_score=MIN_SCORE)" in body
    assert f"COLLECTION = {spec.collection!r}" in source
    assert f"TOP_K = {spec.top_k!r}" in source
    assert f"MIN_SCORE = {spec.min_score!r}" in source


def test_a_vector_json_input_takes_its_query_and_top_k_from_the_mapping() -> None:
    graph = _rewrite_io(vector_search_graph(), "product_docs", "json", "list[json]")
    body = _body_region(_module_for(graph, "product_docs"), "product_docs")
    assert 'as_port(options["query"], "str")' in body
    assert 'top_k=int(options.get("top_k", TOP_K))' in body


@pytest.mark.parametrize(
    ("make_graph", "node_id", "input_type"),
    [
        (sql_lookup_graph, "orders_db", "list[json]"),
        (nosql_query_graph, "tickets", "list[json]"),
        (vector_search_graph, "product_docs", "list[str]"),
        (vector_search_graph, "product_docs", "list[json]"),
    ],
)
def test_an_input_type_with_no_binding_rule_is_refused(
    make_graph, node_id: str, input_type: str
) -> None:
    """The plan's rejected cells (`db_input_type_unsupported`) cannot be emitted."""
    graph = _rewrite_io(make_graph(), node_id, input_type, "list[json]")
    with pytest.raises(ValueError, match="has no binding rule"):
        _module_for(graph, node_id)


def test_a_database_node_without_a_spec_is_refused() -> None:
    graph = _rewrite_node(sql_lookup_graph(), "orders_db", sql=None)
    with pytest.raises(ValueError, match="carries no 'sql' spec"):
        _module_for(graph, "orders_db")


def test_a_database_node_keeps_its_note_as_a_comment_above_the_constants() -> None:
    graph = _rewrite_spec(sql_lookup_graph(), "orders_db", note="Read-only.\nEdit me.")
    source = _module_for(graph, "orders_db")
    assert "# Read-only.\n# Edit me.\nSEED_PATH = " in source


# -- the emitted repository tree, under this target's own package name -------


def test_scaffold_emits_the_repository_tree_under_the_langgraph_package(
    tmp_path: Path,
) -> None:
    graph = vector_search_graph()
    scaffold_langgraph(graph, tmp_path, to_langchain_model_source(_effective()))
    package = tmp_path / "src" / PACKAGE_NAME
    for relative in (
        "repositories/__init__.py",
        "repositories/portshape.py",
        "repositories/vector.py",
        "repositories/embedding.py",
        "repositories/seed/product_docs.json",
    ):
        assert (package / relative).is_file(), relative
    # Only the used kind's adapter: a SQL module here would be a driver this
    # document cannot reach.
    assert not (package / "repositories" / "sql.py").exists()
    factory = (package / "repositories" / "__init__.py").read_text()
    assert f"from {PACKAGE_NAME}.repositories.vector import QdrantRepository" in factory
    # One template set, two renders: the PydanticAI package name must not appear.
    assert "swarm_workflow." not in factory
    spec = _database_spec_of(graph, "product_docs")
    seeded = json.loads((package / "repositories" / "seed" / "product_docs.json").read_text())
    assert [document["id"] for document in seeded] == [document.id for document in spec.seed]


def test_the_emitted_sql_seed_is_the_nodes_declared_seed(tmp_path: Path) -> None:
    graph = sql_lookup_graph()
    scaffold_langgraph(graph, tmp_path, to_langchain_model_source(_effective()))
    spec = _database_spec_of(graph, "orders_db")
    seed = (tmp_path / "src" / PACKAGE_NAME / "repositories" / "seed" / "orders_db.sql").read_text()
    assert seed == spec.seed_sql.rstrip("\n") + "\n"


def test_a_graph_without_database_nodes_emits_none_of_the_database_artifacts(
    tmp_path: Path,
) -> None:
    graph = linear_chat_graph()
    model_source = to_langchain_model_source(_effective())
    scaffold_langgraph(graph, tmp_path, model_source)
    assert not (tmp_path / "src" / PACKAGE_NAME / "repositories").exists()
    assert "[project.optional-dependencies]" not in (tmp_path / "pyproject.toml").read_text()
    # Byte-identical to what this line was before database nodes existed.
    assert (tmp_path / ".env.example").read_text() == "\n".join(model_source.env_lines) + "\n"
    assert "## Database nodes" not in (tmp_path / "README.md").read_text()
    assert "db_mode" not in (tmp_path / "validate" / "dry_run.py").read_text()


# -- pyproject extras, .env.example and README -------------------------------


@pytest.mark.parametrize(
    ("make_graph", "used", "unused"),
    [
        (sql_lookup_graph, "live-sql", ("live-nosql", "live-vector")),
        (nosql_query_graph, "live-nosql", ("live-sql", "live-vector")),
        (vector_search_graph, "live-vector", ("live-sql", "live-nosql")),
    ],
)
def test_the_extras_table_covers_only_the_kinds_the_graph_uses(
    make_graph, used: str, unused: tuple[str, ...]
) -> None:
    text = _render_pyproject(make_graph(), to_langchain_model_source(_effective()))
    data = tomllib.loads(text)
    assert set(data["project"]["optional-dependencies"]) == {used}
    for name in unused:
        assert f"{name} =" not in text
    # The existing tables are untouched, and the base dependencies stay base:
    # a plain `uv sync` installs no live driver.
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        f"src/{PACKAGE_NAME}"
    ]
    assert f"langgraph=={PINNED_LANGGRAPH_VERSION}" in data["project"]["dependencies"]
    assert "psycopg" not in "\n".join(data["project"]["dependencies"])


def test_the_extras_and_env_cover_every_used_kind_of_a_mixed_graph() -> None:
    """Two kinds in one document: both engines, and neither of the others.

    The two table renderers read only the document's node kinds, so the SQL
    fixture and the vector fixture's node can simply be put in one document --
    nothing here depends on how they are wired.
    """
    graph = sql_lookup_graph()
    mixed = graph.model_copy(
        update={"nodes": [*graph.nodes, _database_node(vector_search_graph(), "product_docs")]}
    )
    model_source = to_langchain_model_source(_effective())
    extras = tomllib.loads(_render_pyproject(mixed, model_source))["project"][
        "optional-dependencies"
    ]
    assert set(extras) == {"live-sql", "live-vector"}
    env = _render_env_example(mixed, model_source)
    assert "SWARM_DB_MODE=mock" in env
    assert "SWARM_SQL_DSN=" in env and "SWARM_VECTOR_DSN=" in env
    assert "SWARM_NOSQL_DSN" not in env


def test_env_example_keeps_the_model_lines_and_appends_only_used_engines() -> None:
    model_source = to_langchain_model_source(_effective())
    env = _render_env_example(sql_lookup_graph(), model_source)
    assert env.startswith("\n".join(model_source.env_lines) + "\n")
    assert "# Database nodes: leave SWARM_DB_MODE unset" in env
    assert "SWARM_DB_MODE=mock" in env
    assert "SWARM_SQL_DSN=" in env
    assert "SWARM_NOSQL_DSN" not in env and "SWARM_VECTOR_DSN" not in env


def test_the_readme_gains_a_database_section_only_for_a_database_graph() -> None:
    model_source = to_langchain_model_source(_effective())
    with_db = _render_readme(sql_lookup_graph(), model_source)
    assert "\n## Database nodes\n" in with_db
    assert "`repositories/seed/orders_db.sql`" in with_db
    assert "uv sync --extra live-sql" in with_db
    without = _render_readme(linear_chat_graph(), model_source)
    assert "Database nodes" not in without
    assert "uv sync --extra" not in without


# -- agent -> database tools -------------------------------------------------


def test_an_agent_binds_one_read_only_tool_per_database_node_it_names() -> None:
    graph = database_agent_tool_graph()
    agent = _database_node(graph, "summarize_docs")
    targets = database_tool_targets(graph, agent)
    assert [node.id for node in targets] == ["product_docs"]
    assert database_tool_name(targets[0]) == "product_docs_search"
    source = _module_for(graph, "summarize_docs")
    assert "from langchain.tools import tool" in source
    assert f"from {PACKAGE_NAME}.nodes.product_docs import product_docs_body" in source
    # Typed by the node's own declared I/O, and named for the node.
    assert "async def product_docs_search(query_text: str) -> list[dict[str, Any]]:" in source
    # It runs the node's declared read operation -- the node's own body -- so
    # the project holds one copy of that operation, not one per caller.
    assert "return await product_docs_body(query_text, state, model, {})" in source
    assert "for t in [product_docs_search]" in source
    assert "model.bind_tools(list(tools_by_name.values()))" in source
    assert "MAX_TOOL_ROUNDS = 6" in source
    # Read-only by construction: no write path and no free-form statement.
    assert ".execute(" not in source and "insert_one" not in source
    assert "SELECT" not in source


def test_an_orchestrator_binds_its_delegate_and_database_tools_together() -> None:
    """One tool loop, two kinds of tool: delegation and a database read."""
    graph = _rewrite_node(
        database_agent_tool_graph(),
        "summarize_docs",
        template="orchestrator",
        agent=AgentSpec(
            instructions="Summarize.",
            delegates_to=["helper"],
            tools=["vector:product_docs"],
        ),
    )
    source = _module_for(graph, "summarize_docs")
    body = _body_region(source, "summarize_docs")
    assert "async def helper(query: str) -> str:" in body
    assert "async def product_docs_search(query_text: str) -> list[dict[str, Any]]:" in body
    assert "for t in [helper, product_docs_search]" in body
    assert "model.bind_tools(list(tools_by_name.values()))" in body
    assert "MAX_TOOL_ROUNDS = 6" in source


def test_a_non_namespaced_tool_entry_stays_prompt_metadata() -> None:
    graph = _rewrite_node(
        database_agent_tool_graph(),
        "summarize_docs",
        agent=AgentSpec(instructions="Summarize.", tools=["web_search"]),
    )
    agent = _database_node(graph, "summarize_docs")
    assert database_tool_targets(graph, agent) == ()
    source = _module_for(graph, "summarize_docs")
    assert "@tool" not in source and "bind_tools" not in source
    assert "MAX_TOOL_ROUNDS" not in source


def test_a_write_or_unknown_database_tool_entry_is_not_acted_on() -> None:
    """Phase 1 refuses both (`db_write_as_tool` / `db_tool_unknown_node`, both
    errors); the emitter runs on an already-validated document and does not crash
    on a validation finding -- it simply does not build the tool."""
    write_graph = db_write_as_tool_graph()
    unknown_graph = db_tool_unknown_node_graph()
    for graph, node_id, entry in (
        (write_graph, "log_order", "sql:orders_db"),
        (unknown_graph, "summarize_docs", "vector:missing_db_node"),
    ):
        agent = _database_node(graph, node_id).agent
        assert agent is not None and entry in agent.tools
        assert database_tool_targets(graph, _database_node(graph, node_id)) == ()
        source = _module_for(graph, node_id)
        assert "@tool" not in source and "bind_tools" not in source
        # The safety property either way: no model-reachable write path.
        assert ".execute(" not in source and "insert_one" not in source
    # ...while the write-declared node's own step still runs its declared write.
    assert "repo.execute(" in _body_region(_module_for(write_graph, "orders_db"), "orders_db")


def test_an_agent_can_call_its_database_tool(tmp_path: Path) -> None:
    """The emitted tool really reaches the mock, keylessly.

    The plan's "agent tool with a repository tool" case: the tool is bound
    through the same ``model.bind_tools`` the orchestrator uses, and calling it
    reads the seeded mock. Run in the server's own interpreter (which has
    langchain installed) against the generated project, so no ``uv sync`` is
    needed -- the slow proof lives in ``tests/test_db_codegen.py``.
    """
    graph = database_agent_tool_graph()
    project = tmp_path / "project"
    scaffold_langgraph(graph, project, to_langchain_model_source(_effective()))
    probe = tmp_path / "probe_tool.py"
    probe.write_text(_DATABASE_TOOL_PROBE)
    result = _run_in_server_venv(project, str(probe))
    assert result.returncode == 0, result.stderr[-2500:]
    assert "TOOL PATH OK" in result.stdout


_DATABASE_TOOL_PROBE = '''
"""Call the agent's database tool and prove it read the seeded mock."""

import asyncio

from langchain_core.messages import AIMessage

from swarm_workflow_lg.context import Context
from swarm_workflow_lg.nodes.summarize_docs import summarize_docs_body
from swarm_workflow_lg.state import initial_state


class ToolCallingModel:
    """A model stand-in that calls every bound tool once, then answers."""

    def __init__(self) -> None:
        self.bound: dict = {}
        self.tool_results: list = []

    def bind_tools(self, tools, **kwargs):
        self.bound = {tool.name: tool for tool in tools}
        return self

    async def ainvoke(self, messages):
        tool = self.bound["product_docs_search"]
        result = await tool.ainvoke({"query_text": "aperture sensor"})
        self.tool_results.append(result)
        return AIMessage(content=f"read {len(result)} documents")


async def main() -> None:
    model = ToolCallingModel()
    out = await summarize_docs_body("aperture sensor", initial_state("aperture"), model, {})
    rows = model.tool_results[0]
    print("tool names:", sorted(model.bound))
    print("rows:", len(rows), rows[0]["id"] if rows else None)
    assert sorted(model.bound) == ["product_docs_search"]
    assert rows and isinstance(rows[0], dict)
    assert out == f"read {len(rows)} documents"
    print("TOOL PATH OK")


asyncio.run(main())
'''


# -- the repository tree is off limits, and DB nodes are not conversion targets


def test_the_repository_tree_is_named_as_off_limits(tmp_path: Path) -> None:
    assert REPOSITORIES_DIR in FORBIDDEN_DIRECTORIES
    prompt = build_convert_instructions(
        tmp_path / "lg",
        tmp_path / "src",
        database_agent_tool_graph(),
        model_description="on test model",
    )
    assert f"{REPOSITORIES_DIR}/" in prompt


def test_the_conversion_session_refuses_the_repository_tree(tmp_path: Path) -> None:
    """The prompt names it; the path guard is what enforces it."""
    session = FillSession(
        tmp_path,
        editable_dir_parts=NODES_DIR_PARTS,
        forbidden_files=FORBIDDEN_FILES,
        forbidden_directories=FORBIDDEN_DIRECTORIES,
    )
    with pytest.raises(ModelRetry, match="not editable"):
        session.write_region(
            f"{REPOSITORIES_DIR}/sql.py", "orders_db", "body", "    pass"
        )


def test_a_database_node_is_never_a_conversion_target(tmp_path: Path) -> None:
    graph = sql_lookup_graph()
    assert [node.id for node in _convert_targets(graph)] == ["format_orders"]
    prompt = build_convert_instructions(
        tmp_path / "lg", tmp_path / "src", graph, model_description="on test model"
    )
    targets = prompt.split("## Nodes to convert", 1)[1].split("## How to finish", 1)[0]
    assert f"## Nodes to convert ({len(_convert_targets(graph))})" in prompt
    assert "/".join((*("src", "swarm_workflow", "steps"), "format_orders.py")) in targets
    assert "orders_db" not in targets


def test_fake_convert_leaves_a_database_node_body_untouched(tmp_path: Path) -> None:
    graph = sql_lookup_graph()
    scaffold_langgraph(graph, tmp_path, to_langchain_model_source(_effective()))
    assert apply_fake_convert(tmp_path, graph) == ("format_orders",)
    module = (tmp_path / "src" / PACKAGE_NAME / "nodes" / "orders_db.py").read_text()
    body = _body_region(module, "orders_db")
    assert UNCONVERTED_BODY_SENTINEL not in module
    assert "get_sql_repository(SEED_PATH)" in body
    assert "str(inputs)" not in body


# -- the dry run, and the `list[json]` ripple ---------------------------------


def test_the_dry_run_asserts_the_mock_mode_only_for_a_database_graph() -> None:
    with_db = _render_dry_run(sql_lookup_graph(), analyze(sql_lookup_graph()))
    assert f"from {PACKAGE_NAME}.repositories import MOCK, db_mode" in with_db
    assert "def check_db_mode() -> None:" in with_db
    assert with_db.index("    check_db_mode()") < with_db.index("    check_render()")
    # The database node is a graph node like any other, so the gate's expected
    # node set (and the Mermaid golden) simply include it.
    assert "orders_db" in with_db
    without = _render_dry_run(linear_chat_graph(), analyze(linear_chat_graph()))
    assert "db_mode" not in without
    assert "MOCK" not in without and "check_db_mode" not in without


def test_the_stub_return_expressions_cover_every_port_type() -> None:
    assert set(get_args(PortType)) == set(_RETURN_EXPRESSIONS)
    assert _RETURN_EXPRESSIONS["list[json]"] == '[{"input": str(inputs)}]'


def test_a_list_json_stub_body_returns_rows() -> None:
    graph = _rewrite_io(linear_chat_graph(), "summarize", "str", "list[json]")
    summarize = _database_node(graph, "summarize")
    body = stub_langgraph_body(graph, summarize)
    assert "return [{\"input\": str(inputs)}]" in body


def test_an_agent_with_a_list_json_output_parses_rows() -> None:
    graph = _rewrite_io(linear_chat_graph(), "chat_step", "str", "list[json]")
    source = _module_for(graph, "chat_step")
    assert "output = _parse_json_rows_reply(reply.text)" in source
    assert "def _parse_json_rows_reply(text: str) -> list[dict[str, Any]]:" in source
    # Emitted only where it is used: every other node module keeps its bytes.
    plain = _module_for(linear_chat_graph(), "chat_step")
    assert "_parse_json_rows_reply" not in plain


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


# ---------------------------------------------------------------------------
# End to end (slow): a database graph through the whole nine-phase compile
#
# The fast half above runs the emitted LangGraph project's dry run on the
# server's interpreter, which proves the emitted code; this proves the *gate*:
# a real `uv sync`, the project's own keyless import and its own dry run, for
# each database kind. One fixture per kind rather than all six database
# fixtures, because the remaining three (the agent tool, and the same-seed /
# different-seed SQL pair) differ from these in the *pydantic* target's slow
# gate and in this file's fast half, not in what the LangGraph gate does.
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize(
    "make_graph", [sql_lookup_graph, nosql_query_graph, vector_search_graph]
)
async def test_langgraph_target_compiles_a_database_graph_keylessly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_graph
) -> None:
    """A document with a database node compiles and validates with no credentials.

    The claim the feature makes is that the *generated project* runs against its
    seeded mock, so the evidence has to be the export's own ``uv sync`` and dry
    run -- a phase that merely did not raise would not distinguish a project that
    read its seed from one whose step body was never emitted at all.
    """
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    graph: SwarmGraph = make_graph()
    database_kinds = {node.kind for node in graph.nodes} & {"sql", "nosql", "vector"}
    assert database_kinds, "this parametrization exists to cover a database kind"

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

    # Every phase ran to completion, including lg_validate -- which is where the
    # export's keyless import and dry run happen.
    expected = [
        (name, status)
        for name in ALL_PHASE_NAMES
        for status in (PHASE_STATUS_STARTED, PHASE_STATUS_SUCCEEDED)
    ]
    assert _phase_sequence(registry, "c1") == expected
    assert (lg.project_dir / ".venv").is_dir()  # the gate really ran uv sync

    # The repository layer landed under this target's own package, seeded from
    # each node's own spec: the dry run could not have passed without it.
    repositories = lg.project_dir / "src" / PACKAGE_NAME / "repositories"
    seed_files = sorted(path.name for path in (repositories / "seed").iterdir())
    assert seed_files, "the LangGraph export carries no seed file"
    for node in graph.nodes:
        if node.kind in database_kinds:
            module = get_database_entry(node.kind).package_module
            assert (repositories / f"{module}.py").is_file(), node.kind

    # Only the programmatic steps were converted; the database node's body was
    # emitted, not authored.
    assert sorted(lg.converted_node_ids) == sorted(
        node.id for node in graph.nodes if node.kind in FILLABLE_NODE_KINDS
    )
