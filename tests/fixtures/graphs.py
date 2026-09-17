"""Fixture graph builders for the Group-2 codegen test suite.

Six positive fixtures (linear chat, websearch, orchestrator with
delegation, fanout+join, decision branching, mixed graph with a
programmatic node) plus four negative fixtures (cycle, orphan,
branchless decision, joinless fan-out), each a plain Python function
returning a :class:`SwarmGraph`. Negative fixtures are derived by
mutating a positive fixture's edges/nodes rather than maintaining
parallel near-duplicate documents.
"""

from __future__ import annotations

from datetime import UTC, datetime

from swarm_builder.models import (
    AgentSpec,
    BranchEdge,
    DecisionBranch,
    DecisionSpec,
    DelegateEdge,
    FanoutEdge,
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

UPDATED_AT = datetime(2024, 1, 1, tzinfo=UTC)


def _pos(x: float = 0, y: float = 0) -> Position:
    return Position(x=x, y=y)


def _io(input_type: str = "str", output_type: str = "str") -> NodeIo:
    return NodeIo(input_type=input_type, output_type=output_type)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Positive fixture 1: linear chat
# ---------------------------------------------------------------------------


def linear_chat_graph() -> SwarmGraph:
    intake = SwarmNode(
        id="intake",
        kind="programmatic",
        title="Intake",
        intent="Normalize the raw input topic string.",
        position=_pos(0, 0),
        io=_io("str", "str"),
        writes=["topic"],
        programmatic=ProgrammaticSpec(needs=[], signature_hint="strip whitespace"),
    )
    chat_node = SwarmNode(
        id="chat_step",
        kind="agent",
        title="Chat",
        intent="Have a friendly conversation about the given topic.",
        position=_pos(1, 0),
        template="chat",
        io=_io("str", "str"),
        agent=AgentSpec(instructions="Chat helpfully about the given topic."),
    )
    summarize = SwarmNode(
        id="summarize",
        kind="programmatic",
        title="Summarize",
        intent="Prefix the chat output with SUMMARY:.",
        position=_pos(2, 0),
        io=_io("str", "str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="prefix with SUMMARY:"),
    )
    return SwarmGraph(
        id="linear-chat",
        name="Linear chat",
        entry_node_id="intake",
        exit_node_id="summarize",
        state_fields=[StateField(name="topic", type="str", default='""')],
        nodes=[intake, chat_node, summarize],
        edges=[
            SeqEdge(kind="seq", id="e1", source="intake", target="chat_step"),
            SeqEdge(kind="seq", id="e2", source="chat_step", target="summarize"),
        ],
        updated_at=UPDATED_AT,
    )


# ---------------------------------------------------------------------------
# Positive fixture 2: websearch
# ---------------------------------------------------------------------------


def websearch_graph() -> SwarmGraph:
    search = SwarmNode(
        id="search",
        kind="agent",
        title="Web search",
        intent="Search the web for the latest news on the given topic.",
        position=_pos(0, 0),
        template="websearch",
        io=_io("str", "str"),
        agent=AgentSpec(instructions="Search the web and report findings."),
    )
    format_step = SwarmNode(
        id="format_result",
        kind="programmatic",
        title="Format result",
        intent="Wrap the search result in a RESULT: prefix.",
        position=_pos(1, 0),
        io=_io("str", "str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="prefix with RESULT:"),
    )
    return SwarmGraph(
        id="websearch-graph",
        name="Websearch",
        entry_node_id="search",
        exit_node_id="format_result",
        state_fields=[],
        nodes=[search, format_step],
        edges=[SeqEdge(kind="seq", id="e1", source="search", target="format_result")],
        updated_at=UPDATED_AT,
    )


# ---------------------------------------------------------------------------
# Positive fixture 3: orchestrator with delegation
# ---------------------------------------------------------------------------


def orchestrator_graph() -> SwarmGraph:
    child = SwarmNode(
        id="child_agent",
        kind="agent",
        title="Child agent",
        intent="Answer a narrow sub-question.",
        position=_pos(0, 1),
        template="chat",
        io=_io("str", "str"),
        agent=AgentSpec(instructions="Answer the given sub-question concisely."),
    )
    orchestrator = SwarmNode(
        id="orchestrator",
        kind="agent",
        title="Orchestrator",
        intent="Delegate to a sub-agent and coordinate the final answer.",
        position=_pos(0, 0),
        template="orchestrator",
        io=_io("str", "str"),
        agent=AgentSpec(
            instructions="Coordinate with your delegate to answer the question.",
            delegates_to=["child_agent"],
        ),
    )
    return SwarmGraph(
        id="orchestrator-graph",
        name="Orchestrator with delegation",
        entry_node_id="orchestrator",
        exit_node_id="orchestrator",
        state_fields=[],
        nodes=[orchestrator, child],
        edges=[
            DelegateEdge(kind="delegate", id="e1", source="orchestrator", target="child_agent"),
        ],
        updated_at=UPDATED_AT,
    )


# ---------------------------------------------------------------------------
# Positive fixture 4: fanout + join
# ---------------------------------------------------------------------------


def fanout_join_graph() -> SwarmGraph:
    split = SwarmNode(
        id="split",
        kind="programmatic",
        title="Split",
        intent="Fan-out source: pass the input to both branches.",
        position=_pos(0, 0),
        io=_io("str", "str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="identity"),
    )
    left = SwarmNode(
        id="left",
        kind="programmatic",
        title="Left",
        intent="Left fan-out arm.",
        position=_pos(1, -1),
        io=_io("str", "str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="prefix with L:"),
    )
    right = SwarmNode(
        id="right",
        kind="programmatic",
        title="Right",
        intent="Right fan-out arm.",
        position=_pos(1, 1),
        io=_io("str", "str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="prefix with R:"),
    )
    join = SwarmNode(
        id="join",
        kind="join",
        title="Join",
        intent="Join both fan-out arms into a list.",
        position=_pos(2, 0),
        io=_io("str", "list[str]"),
        join=JoinSpec(reducer="list_append"),
    )
    return SwarmGraph(
        id="fanout-join-graph",
        name="Fanout and join",
        entry_node_id="split",
        exit_node_id="join",
        state_fields=[],
        nodes=[split, left, right, join],
        edges=[
            FanoutEdge(kind="fanout", id="e1", source="split", target="left", join_node_id="join"),
            FanoutEdge(kind="fanout", id="e2", source="split", target="right", join_node_id="join"),
            JoinEdge(kind="join", id="e3", source="left", target="join"),
            JoinEdge(kind="join", id="e4", source="right", target="join"),
        ],
        updated_at=UPDATED_AT,
    )


# ---------------------------------------------------------------------------
# Positive fixture 5: decision branching
# ---------------------------------------------------------------------------


def decision_branching_graph() -> SwarmGraph:
    classify = SwarmNode(
        id="classify",
        kind="programmatic",
        title="Classify",
        intent="Classify the input by length into big or small.",
        position=_pos(0, 0),
        io=_io("str", "str"),
        writes=["length_bucket"],
        programmatic=ProgrammaticSpec(needs=[], signature_hint="length > 3 -> big else small"),
    )
    decision = SwarmNode(
        id="decision",
        kind="decision",
        title="Decision",
        intent="Dispatch by length bucket.",
        position=_pos(1, 0),
        io=_io("str", "str"),
        decision=DecisionSpec(
            branches=[
                DecisionBranch(match="big", target_node_id="big"),
                DecisionBranch(match="small", target_node_id="small"),
            ],
            note="classify by length",
        ),
    )
    big = SwarmNode(
        id="big",
        kind="programmatic",
        title="Big",
        intent="Handle a big classification.",
        position=_pos(2, -1),
        io=_io("str", "str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="prefix with BIG:"),
    )
    small = SwarmNode(
        id="small",
        kind="programmatic",
        title="Small",
        intent="Handle a small classification.",
        position=_pos(2, 1),
        io=_io("str", "str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="prefix with small:"),
    )
    return SwarmGraph(
        id="decision-branching-graph",
        name="Decision branching",
        entry_node_id="classify",
        exit_node_id="big",
        state_fields=[StateField(name="length_bucket", type="str", default='""')],
        nodes=[classify, decision, big, small],
        edges=[
            SeqEdge(kind="seq", id="e1", source="classify", target="decision"),
            BranchEdge(kind="branch", id="e2", source="decision", target="big", match="big"),
            BranchEdge(kind="branch", id="e3", source="decision", target="small", match="small"),
        ],
        updated_at=UPDATED_AT,
    )


# ---------------------------------------------------------------------------
# Positive fixture 6: mixed graph with a programmatic node
# ---------------------------------------------------------------------------


def mixed_programmatic_graph() -> SwarmGraph:
    fetch = SwarmNode(
        id="fetch",
        kind="programmatic",
        title="Fetch",
        intent="Fetch raw data for the given key (unfilled -- fill in the test).",
        position=_pos(0, 0),
        io=_io("str", "str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="return f'DATA:{ctx.inputs}'"),
    )
    respond = SwarmNode(
        id="respond",
        kind="agent",
        title="Respond",
        intent="Summarize the fetched data for the user.",
        position=_pos(1, 0),
        template="chat",
        io=_io("str", "str"),
        agent=AgentSpec(instructions="Summarize the fetched data."),
    )
    return SwarmGraph(
        id="mixed-programmatic-graph",
        name="Mixed graph with a programmatic node",
        entry_node_id="fetch",
        exit_node_id="respond",
        state_fields=[],
        nodes=[fetch, respond],
        edges=[SeqEdge(kind="seq", id="e1", source="fetch", target="respond")],
        updated_at=UPDATED_AT,
    )


# ---------------------------------------------------------------------------
# Negative fixtures
# ---------------------------------------------------------------------------


def cycle_graph() -> SwarmGraph:
    """linear_chat_graph with an added edge back from summarize to intake."""
    graph = linear_chat_graph()
    edges = list(graph.edges) + [
        SeqEdge(kind="seq", id="e_cycle", source="summarize", target="intake")
    ]
    return graph.model_copy(update={"edges": edges})


def orphan_graph() -> SwarmGraph:
    """linear_chat_graph plus an extra node with no edges at all."""
    graph = linear_chat_graph()
    orphan_node = SwarmNode(
        id="orphan",
        kind="programmatic",
        title="Orphan",
        intent="Never wired to anything.",
        position=_pos(5, 5),
        io=_io("str", "str"),
        programmatic=ProgrammaticSpec(needs=[]),
    )
    nodes = list(graph.nodes) + [orphan_node]
    return graph.model_copy(update={"nodes": nodes})


def branchless_decision_graph() -> SwarmGraph:
    """decision_branching_graph with all branches removed."""
    graph = decision_branching_graph()
    nodes = []
    for node in graph.nodes:
        if node.kind == "decision":
            node = node.model_copy(update={"decision": DecisionSpec(branches=[])})
        nodes.append(node)
    edges = [e for e in graph.edges if not isinstance(e, BranchEdge)]
    return graph.model_copy(update={"nodes": nodes, "edges": edges})


def joinless_fanout_graph() -> SwarmGraph:
    """fanout_join_graph with the join node and its edges removed, replaced
    by plain seq edges straight from split to left/right, and a seq edge
    from left to exit (the classic silently-lossy shape, fact 13)."""
    graph = fanout_join_graph()
    nodes = [n for n in graph.nodes if n.kind != "join"]
    edges = [
        SeqEdge(kind="seq", id="e1", source="split", target="left"),
        SeqEdge(kind="seq", id="e2", source="split", target="right"),
    ]
    return graph.model_copy(update={"nodes": nodes, "edges": edges, "exit_node_id": "left"})


def json_ports_graph() -> SwarmGraph:
    """A graph whose entry/exit ports are ``json``.

    Regression fixture for a bug that shipped: the emitter decided whether
    ``graph.py`` needed ``from typing import Any`` by testing
    ``"Any" in (input_annotation, output_annotation)``, but the annotation
    for a ``json`` port is the string ``"dict[str, Any]"``, so the test
    never matched and the emitted module raised ``NameError`` at import.
    The agent templates separately hardcoded ``Agent[None, str]``, so a
    ``json``-output agent node returned a ``str`` and failed the Phase-5
    output-type assertion. ``json`` is one of three legal ``PortType``
    values and no other fixture used it, so nothing caught either defect.
    """
    graph = mixed_programmatic_graph()
    nodes = [
        node.model_copy(update={"io": NodeIo(input_type="json", output_type="json")})
        for node in graph.nodes
    ]
    return graph.model_copy(update={"id": "json-ports-graph", "nodes": nodes})


POSITIVE_FIXTURES = {
    "linear_chat": linear_chat_graph,
    "websearch": websearch_graph,
    "orchestrator": orchestrator_graph,
    "fanout_join": fanout_join_graph,
    "decision_branching": decision_branching_graph,
    "mixed_programmatic": mixed_programmatic_graph,
    "json_ports": json_ports_graph,
}

NEGATIVE_FIXTURES = {
    "cycle": cycle_graph,
    "orphan": orphan_graph,
    "branchless_decision": branchless_decision_graph,
    "joinless_fanout": joinless_fanout_graph,
}

__all__ = [
    "NEGATIVE_FIXTURES",
    "POSITIVE_FIXTURES",
    "branchless_decision_graph",
    "cycle_graph",
    "decision_branching_graph",
    "fanout_join_graph",
    "joinless_fanout_graph",
    "json_ports_graph",
    "linear_chat_graph",
    "mixed_programmatic_graph",
    "orchestrator_graph",
    "orphan_graph",
    "websearch_graph",
]
