"""Unit tests for emit_graph.py against the codegen contract rules
(PLAN.md "Codegen contract" 1-12) that are checkable from generated
source text alone, without a real uv sync (the full end-to-end proof
lives in test_codegen_fixtures.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fixtures.graphs import (
    decision_branching_graph,
    fanout_join_graph,
    linear_chat_graph,
    nosql_query_graph,
    orchestrator_graph,
    sql_lookup_graph,
    vector_search_graph,
)
from swarm_builder.compile.emit_graph import emit_graph
from swarm_builder.models import AgentSpec, DelegateEdge, NodeIo, SeqEdge, SwarmGraph, SwarmNode

#: kind -> the fixture that builds a graph around one node of that kind.
DATABASE_FIXTURES = {
    "sql": sql_lookup_graph,
    "nosql": nosql_query_graph,
    "vector": vector_search_graph,
}


def _database_node_id(graph: SwarmGraph) -> str:
    """The id of the graph's one database node."""
    return next(node.id for node in graph.nodes if node.kind in DATABASE_FIXTURES)


def test_never_calls_graph_constructor_directly() -> None:
    """Contract rule 4: Graph(...) is never called directly (fact 6)."""
    source = emit_graph(linear_chat_graph())
    assert "Graph(" not in source
    assert "GraphBuilder(" in source
    assert "builder.build()" in source


def test_uses_plain_call_step_form_with_explicit_node_id() -> None:
    """Contract rule 6: every builder.step carries an explicit node_id=."""
    source = emit_graph(linear_chat_graph())
    assert 'builder.step(intake, node_id="intake")' in source
    assert 'builder.step(chat_step, node_id="chat_step")' in source
    assert 'builder.step(summarize, node_id="summarize")' in source


def test_future_annotations_is_the_first_import() -> None:
    """Contract rule 12: every generated Python file starts with
    from __future__ import annotations."""
    source = emit_graph(linear_chat_graph())
    lines = [line for line in source.splitlines() if line.strip()]
    import_lines = [line for line in lines if line.startswith(("from ", "import "))]
    assert import_lines[0] == "from __future__ import annotations"


def test_join_emits_explicit_builder_join_never_two_bare_add_edges() -> None:
    """Contract rule 7: every fan-in goes through an explicit
    builder.join(...); two bare add_edge calls are never used as a
    fan-out (fact 13)."""
    source = emit_graph(fanout_join_graph())
    assert "builder.join(reduce_list_append, initial_factory=list" in source
    assert "builder.add_edge(left_node, join)" in source
    assert "builder.add_edge(right_node, join)" in source


def test_decision_branch_chain_precedes_add_edge_targeting_it() -> None:
    """Contract rule 8: a decision's complete .branch(...) chain is
    emitted BEFORE the add_edge that targets it (fact 14)."""
    source = emit_graph(decision_branching_graph())
    branch_chain_end = source.rindex(".branch(")
    add_edge_to_decision = source.index("builder.add_edge(classify_node, decision)")
    assert add_edge_to_decision > branch_chain_end, (
        "add_edge targeting the decision must come after the full .branch() chain"
    )
    # And the decision variable is rebound each time, not a fresh name.
    assert source.count("decision = decision.branch(") == 2
    assert source.count("decision = builder.decision(") == 1


def test_literal_import_present_only_when_a_decision_node_exists() -> None:
    """B3: from typing import Literal is needed for builder.match(Literal[...])
    -- an expression, not deferred by __future__ import annotations."""
    with_decision = emit_graph(decision_branching_graph())
    assert "from typing import Literal" in with_decision

    without_decision = emit_graph(linear_chat_graph())
    assert "from typing import Literal" not in without_decision


def test_port_type_never_interpolated_verbatim() -> None:
    """Contract rule 9: PortType goes through the annotation table, never
    interpolated verbatim (fact 17) -- output_type=json would be a
    NameError; output_type=list[str] is the real annotation."""
    source = emit_graph(fanout_join_graph())
    assert "output_type=list[str]" in source
    assert "output_type=json" not in source


def test_delegate_edge_emits_no_add_edge_and_no_step_for_child() -> None:
    """I1: delegate edges emit no add_edge at all -- otherwise the child
    runs twice."""
    source = emit_graph(orchestrator_graph())
    assert "child_agent" not in source  # no step import, no add_edge reference
    assert "add_edge" in source  # but the parent still gets its own edges


def test_start_and_end_edges_are_always_emitted() -> None:
    source = emit_graph(linear_chat_graph())
    assert "builder.add_edge(builder.start_node, intake_node)" in source
    assert "builder.add_edge(summarize_node, builder.end_node)" in source


def test_multiple_sinks_each_get_an_end_edge() -> None:
    """B1: exit_node_id is not the only sink -- spike/branching wires
    BOTH big and small to end_node."""
    source = emit_graph(decision_branching_graph())
    assert "builder.add_edge(big_node, builder.end_node)" in source
    assert "builder.add_edge(small_node, builder.end_node)" in source


def test_broadcast_fork_ids_predicted_for_fanout_source() -> None:
    """fact 32: the synthetic fork id is predictable and computed, not
    discovered."""
    source = emit_graph(fanout_join_graph())
    assert 'BROADCAST_FORK_NODE_IDS: dict[str, str] = {"split": "split_broadcast_fork"}' in source


def test_no_broadcast_fork_constant_when_no_fanout() -> None:
    source = emit_graph(linear_chat_graph())
    assert "BROADCAST_FORK_NODE_IDS" not in source


def test_graph_run_return_value_used_directly_in_dry_run_template() -> None:
    """Contract rule 10: graph.run()'s return value is used directly --
    no .output access, no End assertion. Checked on the emitted
    dry_run.py, not graph.py, since that's where graph.run() is called."""
    import tempfile
    from pathlib import Path

    from swarm_builder.compile import default_scaffold_model
    from swarm_builder.compile.scaffold import scaffold

    with tempfile.TemporaryDirectory() as tmp:
        scaffold(linear_chat_graph(), Path(tmp), default_scaffold_model())
        dry_run_text = (Path(tmp) / "validate" / "dry_run.py").read_text()
    assert "result.output" not in dry_run_text
    assert "out.output" not in dry_run_text
    assert "isinstance(out, End)" not in dry_run_text
    assert "from pydantic_graph import End" not in dry_run_text
    assert "await graph.run(" in dry_run_text


def test_every_emitted_py_file_starts_with_future_annotations() -> None:
    """Contract rule 12, whole-scaffold invariant (B16): every generated
    Python file starts with from __future__ import annotations, not just
    graph.py."""
    import tempfile
    from pathlib import Path

    from swarm_builder.compile import default_scaffold_model
    from swarm_builder.compile.scaffold import scaffold

    with tempfile.TemporaryDirectory() as tmp:
        result = scaffold(orchestrator_graph(), Path(tmp), default_scaffold_model())
        py_files = [p for p in result.written_paths if p.suffix == ".py"]
        assert py_files, "expected at least one emitted .py file"
        for py_file in py_files:
            text = py_file.read_text()
            lines = [line for line in text.splitlines() if line.strip()]
            import_lines = [line for line in lines if line.startswith(("from ", "import "))]
            assert import_lines and import_lines[0] == "from __future__ import annotations", (
                f"{py_file} does not start its imports with "
                "'from __future__ import annotations'"
            )


def test_scaffolding_the_same_fixture_twice_is_byte_identical() -> None:
    """Determinism check (nice-to-have from planReview): scaffolding the
    same fixture twice must produce byte-identical output for every
    emitted file -- this is also the cheapest guard against the kind of
    process-global corruption B5 found."""
    import tempfile
    from pathlib import Path

    from swarm_builder.compile import default_scaffold_model
    from swarm_builder.compile.scaffold import scaffold

    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        result_a = scaffold(fanout_join_graph(), Path(tmp_a), default_scaffold_model())
        result_b = scaffold(fanout_join_graph(), Path(tmp_b), default_scaffold_model())

        rel_a = sorted(p.relative_to(tmp_a) for p in result_a.written_paths)
        rel_b = sorted(p.relative_to(tmp_b) for p in result_b.written_paths)
        assert rel_a == rel_b

        for rel_path in rel_a:
            text_a = (Path(tmp_a) / rel_path).read_text()
            text_b = (Path(tmp_b) / rel_path).read_text()
            assert text_a == text_b, f"{rel_path} differs between two scaffolds of the same fixture"


# ---------------------------------------------------------------------------
# Database nodes: real steps, never bare variables, never delegate-only children
# (PLAN-DB-NODES.md §4.0 items 6-9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(DATABASE_FIXTURES))
def test_database_kinds_are_emitted_as_builder_steps(kind: str) -> None:
    """§4.0 item 6: a database node is a ``builder.step(...)`` with an explicit
    ``node_id``, imported from its own ``steps/<id>`` module -- leaving it out of
    ``STEP_KINDS`` emitted a ``graph.py`` that never ran the node at all."""
    graph = DATABASE_FIXTURES[kind]()
    node_id = _database_node_id(graph)
    source = emit_graph(graph)
    assert f'builder.step({node_id}, node_id="{node_id}")' in source
    assert f"from swarm_workflow.steps.{node_id} import {node_id}" in source


@pytest.mark.parametrize("kind", sorted(DATABASE_FIXTURES))
def test_database_kinds_are_never_bare_builder_variables(kind: str) -> None:
    """A bare-variable kind is one whose builder call returns a *builder*
    (``decision``/``join``). A database node's call returns the step function, so
    binding the bare id would collide with the imported step name and the node
    would never be registered."""
    graph = DATABASE_FIXTURES[kind]()
    node_id = _database_node_id(graph)
    source = emit_graph(graph)
    assert not re.search(rf"^{node_id} = ", source, re.MULTILINE)
    assert f"{node_id} = builder.step(" not in source
    assert f"{node_id}_node = builder.step({node_id}" in source


@pytest.mark.parametrize("kind", sorted(DATABASE_FIXTURES))
def test_run_tracer_step_list_contains_database_node_ids(kind: str, tmp_path: Path) -> None:
    """§4.0 item 9: the Run tracer wraps exactly the step ids it is given, so a
    database step omitted from that list runs while the canvas shows nothing for
    it -- a silently invisible node."""
    import tempfile

    from swarm_builder.compile import default_scaffold_model
    from swarm_builder.compile.scaffold import scaffold

    graph = DATABASE_FIXTURES[kind]()
    with tempfile.TemporaryDirectory() as tmp:
        scaffold(graph, Path(tmp), default_scaffold_model())
        tracer = (Path(tmp) / "run" / "stream_run.py").read_text()
    expected = ", ".join(f'"{node.id}"' for node in sorted(graph.nodes, key=lambda n: n.id))
    assert f"STEP_NODE_IDS = [{expected}]" in tracer


def test_a_list_json_port_goes_through_the_annotation_table() -> None:
    """Contract rule 9 for the new port type: ``list[json]`` is a *label*, so it is
    never interpolated verbatim (``output_type=list[json]`` is a NameError) -- and its
    annotation needs ``typing.Any``, which ``PORT_TYPE_IMPORTS`` is asked for rather
    than guessed from the annotation text."""
    graph = sql_lookup_graph()
    nodes = [
        node.model_copy(update={"io": NodeIo(input_type="list[json]", output_type="list[json]")})
        if node.id == "format_orders"
        else node
        for node in graph.nodes
    ]
    source = emit_graph(graph.model_copy(update={"nodes": nodes}))
    assert "output_type=list[dict[str, Any]]" in source
    assert "output_type=list[json]" not in source
    assert "from typing import Any" in source


def _delegate_only_database_graph() -> SwarmGraph:
    """A graph whose SQL node is reachable *only* through a delegate edge.

    The same carve-out a delegate-only *agent* child gets: a node reached only by
    delegation is called as a tool by its orchestrator and never emitted as a
    graph step, so it must get no ``builder.step`` and no ``steps/<id>.py`` -- and
    nothing in the wiring may reference a builder variable for it.

    The orchestrator does not name ``orders_db`` in ``delegatesTo`` either: that
    field renders one tool per child, and a child with no agent module cannot be
    called that way. A database node is attached to an agent through
    ``agent.tools`` (``sql:<id>``), which is a different mechanism -- see
    ``test_agent.py``'s repository-tool tests.
    """
    graph = sql_lookup_graph()
    reader = SwarmNode(
        id="reader",
        kind="agent",
        title="Reader",
        intent="Ask the database node for the customer's orders.",
        position=graph.nodes[0].position,
        template="orchestrator",
        io=NodeIo(input_type="str", output_type="list[json]"),
        agent=AgentSpec(instructions="Read the orders."),
    )
    format_orders = next(node for node in graph.nodes if node.id == "format_orders")
    orders_db = next(node for node in graph.nodes if node.id == "orders_db")
    return graph.model_copy(
        update={
            "nodes": [orders_db, reader, format_orders],
            "edges": [
                DelegateEdge(kind="delegate", id="e1", source="reader", target="orders_db"),
                SeqEdge(kind="seq", id="e2", source="reader", target="format_orders"),
            ],
            "entry_node_id": "reader",
            "exit_node_id": "format_orders",
        }
    )


def test_a_delegate_only_database_node_is_not_a_step(tmp_path: Path) -> None:
    """The ``delegate_only_node_ids`` carve-out is kept for database nodes."""
    import tempfile

    from swarm_builder.compile import default_scaffold_model
    from swarm_builder.compile.graph_ir import analyze
    from swarm_builder.compile.scaffold import scaffold

    graph = _delegate_only_database_graph()
    structure = analyze(graph)
    assert "orders_db" in structure.delegate_only_node_ids
    source = emit_graph(graph)
    assert "builder.step(orders_db" not in source
    assert "from swarm_workflow.steps.orders_db import" not in source
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        scaffold(graph, project, default_scaffold_model())
        assert not (project / "src" / "swarm_workflow" / "steps" / "orders_db.py").exists()
        # The node still exists as a database node, so the repository layer it
        # needs is still emitted -- it is a tool's target, not a step.
        assert (project / "src" / "swarm_workflow" / "repositories" / "sql.py").is_file()
