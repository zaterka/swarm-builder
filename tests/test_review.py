"""Unit tests for review.py (PLAN.md "Tests" -> Unit -> review.py, and
the codegen suite's negative-fixture requirement)."""

from __future__ import annotations

from datetime import UTC, datetime

from fixtures.graphs import (
    NEGATIVE_FIXTURES,
    POSITIVE_FIXTURES,
    branchless_decision_graph,
    cycle_graph,
    decision_branching_graph,
    joinless_fanout_graph,
    linear_chat_graph,
    orphan_graph,
)
from swarm_builder.compile.review import review
from swarm_builder.models import (
    AgentSpec,
    DelegateEdge,
    FanoutEdge,
    JoinEdge,
    JoinSpec,
    NodeIo,
    Position,
    ProgrammaticSpec,
    SeqEdge,
    SwarmGraph,
    SwarmNode,
)

UPDATED_AT = datetime(2024, 1, 1, tzinfo=UTC)


def _pos() -> Position:
    return Position(x=0, y=0)


def _io(input_type: str = "str", output_type: str = "str") -> NodeIo:
    return NodeIo(input_type=input_type, output_type=output_type)  # type: ignore[arg-type]


def test_all_six_positive_fixtures_pass_with_no_findings() -> None:
    for name, build in POSITIVE_FIXTURES.items():
        result = review(build())
        assert result.ok, f"{name} unexpectedly failed review: {result.errors}"
        assert result.warnings == [], f"{name} had unexpected warnings: {result.warnings}"


def test_cycle_is_a_hard_error() -> None:
    result = review(cycle_graph())
    assert not result.ok
    assert any(f.code == "cycle" for f in result.errors)


def test_orphan_is_a_hard_error() -> None:
    result = review(orphan_graph())
    assert not result.ok
    assert any(f.code == "unreachable_node" and "orphan" in f.node_ids for f in result.errors)


def test_branchless_decision_is_a_hard_error() -> None:
    result = review(branchless_decision_graph())
    assert not result.ok
    assert any(f.code == "branchless_decision" for f in result.errors)


def test_joinless_fanout_is_a_hard_error() -> None:
    result = review(joinless_fanout_graph())
    assert not result.ok
    assert any(f.code == "fanout_without_join" for f in result.errors)


def test_all_four_negative_fixtures_are_rejected() -> None:
    for name, build in NEGATIVE_FIXTURES.items():
        result = review(build())
        assert not result.ok, f"negative fixture {name} was not rejected"


def test_missing_intent_is_a_hard_error() -> None:
    graph = linear_chat_graph()
    nodes = [n.model_copy(update={"intent": ""}) if n.id == "intake" else n for n in graph.nodes]
    graph = graph.model_copy(update={"nodes": nodes})
    result = review(graph)
    assert not result.ok
    assert any(f.code == "missing_intent" for f in result.errors)


def test_undeclared_state_write_is_a_hard_error() -> None:
    graph = linear_chat_graph()
    nodes = [
        n.model_copy(update={"writes": ["not_declared"]}) if n.id == "intake" else n
        for n in graph.nodes
    ]
    graph = graph.model_copy(update={"nodes": nodes})
    result = review(graph)
    assert not result.ok
    assert any(f.code == "undeclared_state_write" for f in result.errors)


def test_duplicate_state_writer_is_a_hard_error() -> None:
    graph = linear_chat_graph()
    nodes = [
        n.model_copy(update={"writes": ["topic"]}) if n.id in ("intake", "summarize") else n
        for n in graph.nodes
    ]
    graph = graph.model_copy(update={"nodes": nodes})
    result = review(graph)
    assert not result.ok
    assert any(f.code == "duplicate_state_writer" for f in result.errors)


def test_read_of_unwritten_field_is_a_hard_error() -> None:
    graph = linear_chat_graph()
    nodes = [
        n.model_copy(update={"reads": ["never_written"]}) if n.id == "summarize" else n
        for n in graph.nodes
    ]
    graph = graph.model_copy(update={"nodes": nodes})
    result = review(graph)
    assert not result.ok
    assert any(f.code == "unwritten_state_read" for f in result.errors)


def test_port_type_mismatch_is_a_hard_error() -> None:
    graph = linear_chat_graph()
    nodes = [
        n.model_copy(update={"io": _io("str", "list[str]")}) if n.id == "intake" else n
        for n in graph.nodes
    ]
    graph = graph.model_copy(update={"nodes": nodes})
    result = review(graph)
    assert not result.ok
    assert any(f.code == "port_type_mismatch" for f in result.errors)


def test_delegate_and_sequence_target_is_a_hard_error() -> None:
    """A node that is both a delegate target and a seq/branch target
    (PLAN.md I1) is a hard error."""
    a = SwarmNode(
        id="a",
        kind="agent",
        title="A",
        intent="Node A.",
        position=_pos(),
        template="chat",
        io=_io(),
        agent=AgentSpec(instructions="do a", delegates_to=["b"]),
    )
    b = SwarmNode(
        id="b",
        kind="agent",
        title="B",
        intent="Node B.",
        position=_pos(),
        template="chat",
        io=_io(),
        agent=AgentSpec(instructions="do b"),
    )
    graph = SwarmGraph(
        id="delegate-and-seq",
        name="delegate and seq",
        entry_node_id="a",
        exit_node_id="b",
        nodes=[a, b],
        edges=[
            DelegateEdge(kind="delegate", id="e1", source="a", target="b"),
            SeqEdge(kind="seq", id="e2", source="a", target="b"),
        ],
        updated_at=UPDATED_AT,
    )
    result = review(graph)
    assert not result.ok
    assert any(f.code == "delegate_and_sequence_target" for f in result.errors)


def test_decision_branch_type_mismatch_is_a_hard_error() -> None:
    """fact 27: a branch target's input_type must equal the decision's
    SOURCE STEP's output_type, not any node further upstream."""
    graph = decision_branching_graph()
    nodes = [
        n.model_copy(update={"io": _io("list[str]", n.io.output_type)}) if n.id == "big" else n
        for n in graph.nodes
    ]
    graph = graph.model_copy(update={"nodes": nodes})
    result = review(graph)
    assert not result.ok
    assert any(f.code == "decision_branch_type_mismatch" for f in result.errors)


def test_kind_spec_mismatch_is_a_hard_error() -> None:
    graph = linear_chat_graph()
    nodes = [
        n.model_copy(update={"programmatic": None}) if n.id == "intake" else n
        for n in graph.nodes
    ]
    graph = graph.model_copy(update={"nodes": nodes})
    result = review(graph)
    assert not result.ok
    assert any(f.code == "kind_spec_mismatch" for f in result.errors)


def test_fanout_arm_missing_join_edge_is_a_hard_error() -> None:
    split = SwarmNode(
        id="split",
        kind="programmatic",
        title="Split",
        intent="fan out",
        position=_pos(),
        io=_io(),
        programmatic=ProgrammaticSpec(),
    )
    left = SwarmNode(
        id="left",
        kind="programmatic",
        title="Left",
        intent="left arm",
        position=_pos(),
        io=_io(),
        programmatic=ProgrammaticSpec(),
    )
    right = SwarmNode(
        id="right",
        kind="programmatic",
        title="Right",
        intent="right arm",
        position=_pos(),
        io=_io(),
        programmatic=ProgrammaticSpec(),
    )
    join = SwarmNode(
        id="join",
        kind="join",
        title="Join",
        intent="join",
        position=_pos(),
        io=_io("str", "list[str]"),
        join=JoinSpec(reducer="list_append"),
    )
    graph = SwarmGraph(
        id="fanout-missing-join-edge",
        name="fanout missing join edge",
        entry_node_id="split",
        exit_node_id="join",
        nodes=[split, left, right, join],
        edges=[
            FanoutEdge(kind="fanout", id="e1", source="split", target="left", join_node_id="join"),
            FanoutEdge(kind="fanout", id="e2", source="split", target="right", join_node_id="join"),
            JoinEdge(kind="join", id="e3", source="left", target="join"),
            # right's JoinEdge is missing -- Phase 1 must catch this.
        ],
        updated_at=UPDATED_AT,
    )
    result = review(graph)
    assert not result.ok
    assert any(f.code == "fanout_arm_missing_join_edge" for f in result.errors)


def test_decision_branch_mismatch_is_a_hard_error() -> None:
    graph = decision_branching_graph()
    edges = [
        e.model_copy(update={"match": "medium"})
        if (e.kind == "branch" and e.match == "small")
        else e
        for e in graph.edges
    ]
    graph = graph.model_copy(update={"edges": edges})
    result = review(graph)
    assert not result.ok
    assert any(f.code == "decision_branch_mismatch" for f in result.errors)


def test_unknown_edge_endpoint_is_a_hard_error() -> None:
    graph = linear_chat_graph()
    edges = list(graph.edges) + [SeqEdge(kind="seq", id="bad", source="intake", target="ghost")]
    graph = graph.model_copy(update={"edges": edges})
    result = review(graph)
    assert not result.ok
    assert any(f.code == "unknown_edge_endpoint" for f in result.errors)
