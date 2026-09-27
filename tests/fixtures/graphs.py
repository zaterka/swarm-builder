"""Fixture graph builders for the Group-2 codegen test suite.

Thirteen positive fixtures (linear chat, websearch, orchestrator with
delegation, fanout+join, decision branching, mixed graph with a
programmatic node, the json-ports regression graph, and six database
graphs covering the three database kinds) plus twelve negative fixtures
(cycle, orphan, branchless decision, joinless fan-out, and eight
database-node violations that Phase 1 must reject), each a plain Python
function returning a :class:`SwarmGraph`. Negative fixtures are derived by
mutating a positive fixture's edges/nodes rather than maintaining
parallel near-duplicate documents.

Every database fixture is built from the *starter catalog*
(``swarm_builder.templates.database``) rather than from a hand-written
literal, so the fixtures exercise the same default operation, seed and I/O
pair a user gets when they drop a node on the canvas -- and so a fixture
cannot silently drift from the starter, which Phase 1 reports as
``db_starter_drift``.
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
    NosqlSpec,
    Position,
    ProgrammaticSpec,
    SeqEdge,
    SqlSpec,
    StateField,
    SwarmGraph,
    SwarmNode,
    VectorSpec,
)
from swarm_builder.templates.database import get_database_entry

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
# Positive fixtures 8-13: the three database kinds
#
# A database node's declared I/O pair is mandatory (`str -> list[json]`) and its
# input may never be `list[json]`, so a database node is always followed by
# something that turns rows back into the value the *next* step binds -- which
# is why every fixture below pairs its database node with a consumer node.
# ---------------------------------------------------------------------------


def _starter_spec(kind: str) -> SqlSpec | NosqlSpec | VectorSpec:
    """A fresh copy of one kind's starter spec.

    A copy, not the catalog's own object: every node gets its own spec, exactly
    as the Inspector copies one in when the node is created.
    """
    return get_database_entry(kind).starter_spec.model_copy()


def _starter_io(kind: str) -> NodeIo:
    """One kind's mandatory I/O pair (``str -> list[json]``)."""
    return get_database_entry(kind).starter_io.model_copy()


def _database_node(
    kind: str, node_id: str, title: str, intent: str, x: float = 0
) -> SwarmNode:
    """One database node carrying its kind's starter spec and I/O pair."""
    return SwarmNode(
        id=node_id,
        kind=kind,  # type: ignore[arg-type]
        title=title,
        intent=intent,
        position=_pos(x, 0),
        io=_starter_io(kind),
        **{kind: _starter_spec(kind)},
    )


def sql_lookup_graph() -> SwarmGraph:
    """The starter's customers/orders example: one SQL node, one consumer.

    The node carries the starter spec verbatim, so this fixture also proves the
    starter's default operation binds against the starter's seed with no user
    input (the ``:input`` placeholder is the upstream value).
    """
    orders_db = _database_node(
        "sql",
        "orders_db",
        "Orders",
        "Read the orders of the customer named by the input.",
    )
    format_orders = SwarmNode(
        id="format_orders",
        kind="programmatic",
        title="Format orders",
        intent="Render the returned order rows as one line each.",
        position=_pos(1, 0),
        io=_io("list[json]", "str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="one line per row"),
    )
    return SwarmGraph(
        id="sql-lookup-graph",
        name="SQL lookup",
        entry_node_id="orders_db",
        exit_node_id="format_orders",
        state_fields=[],
        nodes=[orders_db, format_orders],
        edges=[SeqEdge(kind="seq", id="e1", source="orders_db", target="format_orders")],
        updated_at=UPDATED_AT,
    )


def nosql_query_graph() -> SwarmGraph:
    """The starter's tickets example: one NoSQL node, one consumer."""
    tickets = _database_node(
        "nosql",
        "tickets",
        "Tickets",
        "Find the support tickets whose status is the input.",
    )
    format_tickets = SwarmNode(
        id="format_tickets",
        kind="programmatic",
        title="Format tickets",
        intent="Render the returned ticket documents as one line each.",
        position=_pos(1, 0),
        io=_io("list[json]", "str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="one line per ticket"),
    )
    return SwarmGraph(
        id="nosql-query-graph",
        name="NoSQL query",
        entry_node_id="tickets",
        exit_node_id="format_tickets",
        state_fields=[],
        nodes=[tickets, format_tickets],
        edges=[SeqEdge(kind="seq", id="e1", source="tickets", target="format_tickets")],
        updated_at=UPDATED_AT,
    )


def vector_search_graph() -> SwarmGraph:
    """The starter's product_docs example: one vector node, one consumer."""
    product_docs = _database_node(
        "vector",
        "product_docs",
        "Product docs",
        "Search the product documentation for the input text.",
    )
    format_matches = SwarmNode(
        id="format_matches",
        kind="programmatic",
        title="Format matches",
        intent="Render the search hits as one line each.",
        position=_pos(1, 0),
        io=_io("list[json]", "str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="one line per hit"),
    )
    return SwarmGraph(
        id="vector-search-graph",
        name="Vector search",
        entry_node_id="product_docs",
        exit_node_id="format_matches",
        state_fields=[],
        nodes=[product_docs, format_matches],
        edges=[SeqEdge(kind="seq", id="e1", source="product_docs", target="format_matches")],
        updated_at=UPDATED_AT,
    )


def database_agent_tool_graph() -> SwarmGraph:
    """A database node an agent may also call, read-only, as a tool.

    ``agent.tools`` carries the namespaced ``<kind>:<node_id>`` form for database
    nodes; the emitter turns each one into a read-only tool typed by that node's
    own declared I/O. The agent uses the ``chat`` template deliberately: tool
    rendering must not be tied to ``orchestrator``, which is where the
    delegate-tool code happens to live today.
    """
    product_docs = _database_node(
        "vector",
        "product_docs",
        "Product docs",
        "Search the product documentation for the input text.",
    )
    summarize = SwarmNode(
        id="summarize_docs",
        kind="agent",
        title="Summarize documents",
        intent="Summarize the product documents the search returned.",
        position=_pos(1, 0),
        template="chat",
        io=_io("list[json]", "str"),
        agent=AgentSpec(
            instructions="Summarize the matching product documents.",
            tools=[f"vector:{product_docs.id}"],
        ),
    )
    return SwarmGraph(
        id="database-agent-tool-graph",
        name="Database node as an agent tool",
        entry_node_id="product_docs",
        exit_node_id="summarize_docs",
        state_fields=[],
        nodes=[product_docs, summarize],
        edges=[SeqEdge(kind="seq", id="e1", source="product_docs", target="summarize_docs")],
        updated_at=UPDATED_AT,
    )


def nosql_agent_tool_graph() -> SwarmGraph:
    """A NoSQL node an agent may also call, read-only, as a tool.

    The sibling of :func:`database_agent_tool_graph` for the other kind, and the
    combination that hid a real defect: the PydanticAI tool path emitted
    ``filters = bind_input_filter(...)`` while its import line still listed only
    ``as_port``, so a NoSQL node attached as an agent tool raised ``NameError`` the
    first time the model called it. No fixture covered a NoSQL agent *tool* --
    ``database_agent_tool_graph`` covers the vector kind and the standalone NoSQL
    fixtures cover the step -- so nothing exercised it until it was found by
    inspection. This fixture closes that hole end to end.
    """
    tickets = _database_node(
        "nosql",
        "tickets",
        "Tickets",
        "Read the tickets whose status the input names.",
    )
    triage = SwarmNode(
        id="triage_tickets",
        kind="agent",
        title="Triage tickets",
        intent="Summarize the tickets the lookup returned.",
        position=_pos(1, 0),
        template="chat",
        io=_io("list[json]", "str"),
        agent=AgentSpec(
            instructions="Summarize the matching tickets.",
            tools=[f"nosql:{tickets.id}"],
        ),
    )
    return SwarmGraph(
        id="nosql-agent-tool-graph",
        name="NoSQL node as an agent tool",
        entry_node_id="tickets",
        exit_node_id="triage_tickets",
        state_fields=[],
        nodes=[tickets, triage],
        edges=[SeqEdge(kind="seq", id="e1", source="tickets", target="triage_tickets")],
        updated_at=UPDATED_AT,
    )


#: Appended to the starter's SQL seed to build a node whose seed genuinely
#: differs from its sibling's -- see :func:`two_sql_nodes_different_seed_graph`.
_EXTRA_CUSTOMER_SQL = (
    "\nINSERT INTO customers (id, name, city) VALUES (4, 'Umbrella', 'Oslo');\n"
)


def two_sql_nodes_same_seed_graph() -> SwarmGraph:
    """Two SQL nodes over the *same* seed: one shared mock instance.

    Both nodes carry the starter spec, so their rendered seed files are
    byte-identical and the factory's ``(kind, seed)`` key hands them a single
    in-memory database -- a write in the first step is visible to the second,
    which is the documented shared-mock behaviour.
    """
    orders_db = _database_node(
        "sql", "orders_db", "Orders", "Read the orders of the named customer."
    )
    pick_name = SwarmNode(
        id="pick_name",
        kind="programmatic",
        title="Pick a name",
        intent="Return one customer name from the rows, for the next query to bind.",
        position=_pos(1, 0),
        io=_io("list[json]", "str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="first row's name"),
    )
    orders_db_again = _database_node(
        "sql",
        "orders_db_again",
        "Orders again",
        "Read the same example database a second time.",
        x=2,
    )
    return SwarmGraph(
        id="two-sql-nodes-same-seed-graph",
        name="Two SQL nodes over one seed",
        entry_node_id="orders_db",
        exit_node_id="orders_db_again",
        state_fields=[],
        nodes=[orders_db, pick_name, orders_db_again],
        edges=[
            SeqEdge(kind="seq", id="e1", source="orders_db", target="pick_name"),
            SeqEdge(kind="seq", id="e2", source="pick_name", target="orders_db_again"),
        ],
        updated_at=UPDATED_AT,
    )


def two_sql_nodes_different_seed_graph() -> SwarmGraph:
    """Two SQL nodes over *different* seeds: two independent mock instances.

    The second node's seed adds a customer the starter's does not have, so the
    two nodes must not share one database: Phase 1 warns
    (``db_separate_mock_instances``) and the factory keys two instances, so
    neither node's declared seed is silently ignored.
    """
    graph = two_sql_nodes_same_seed_graph()
    nodes = []
    for node in graph.nodes:
        if node.id == "orders_db_again" and node.sql is not None:
            node = node.model_copy(
                update={
                    "sql": node.sql.model_copy(
                        update={"seed_sql": node.sql.seed_sql + _EXTRA_CUSTOMER_SQL}
                    )
                }
            )
        nodes.append(node)
    return graph.model_copy(
        update={"id": "two-sql-nodes-different-seed-graph", "nodes": nodes}
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


# ---------------------------------------------------------------------------
# Negative fixtures for the database rules (Phase 1's db_* codes)
#
# One per code Phase 1 can decide from the document alone, each a mutation of a
# positive database fixture: it therefore differs from a graph that must pass in
# exactly the way its name states.
# ---------------------------------------------------------------------------


def _rewrite_node(graph: SwarmGraph, node_id: str, **updates: object) -> SwarmGraph:
    """Replace one node's fields, leaving every other node and edge alone."""
    nodes = [
        node.model_copy(update=updates) if node.id == node_id else node
        for node in graph.nodes
    ]
    return graph.model_copy(update={"nodes": nodes})


def _sql_starter_spec() -> SqlSpec:
    """The SQL starter spec, typed (so a `.query`/`.seed_sql` edit type-checks)."""
    spec = _starter_spec("sql")
    assert isinstance(spec, SqlSpec)
    return spec


def db_empty_operation_graph() -> SwarmGraph:
    """sql_lookup_graph with the SQL node's query emptied (``db_empty_operation``)."""
    return _rewrite_node(
        sql_lookup_graph(), "orders_db", sql=_sql_starter_spec().model_copy(update={"query": ""})
    )


def db_placeholder_mismatch_graph() -> SwarmGraph:
    """A SQL node whose input is ``str`` but whose query binds ``:customer``.

    A ``str`` input binds under the name ``input``, so a query with only
    ``:customer`` has a placeholder the document cannot supply
    (``db_placeholder_mismatch``).
    """
    spec = _sql_starter_spec()
    return _rewrite_node(
        sql_lookup_graph(),
        "orders_db",
        sql=spec.model_copy(update={"query": spec.query.replace(":input", ":customer")}),
    )


def db_input_type_unsupported_graph() -> SwarmGraph:
    """A SQL node declaring ``list[json]`` as its input (``db_input_type_unsupported``)."""
    return _rewrite_node(
        sql_lookup_graph(),
        "orders_db",
        io=NodeIo(input_type="list[json]", output_type="list[json]"),
    )


def vector_list_str_input_graph() -> SwarmGraph:
    """A vector node declaring ``list[str]`` as its input (``db_input_type_unsupported``).

    A vector node's input is the query *text*; a list of strings has no meaning
    to a similarity search, which is why it is rejected rather than joined.
    """
    return _rewrite_node(
        vector_search_graph(),
        "product_docs",
        io=NodeIo(input_type="list[str]", output_type="list[json]"),
    )


def db_op_io_mismatch_graph() -> SwarmGraph:
    """A NoSQL node declaring ``str -> json`` (``db_op_io_mismatch``).

    A database node's I/O pair is not a choice. The consumer is re-typed too, so
    this fixture produces that one code rather than an unrelated port-type
    mismatch alongside it.
    """
    graph = _rewrite_node(
        nosql_query_graph(), "tickets", io=NodeIo(input_type="str", output_type="json")
    )
    return _rewrite_node(
        graph, "format_tickets", io=NodeIo(input_type="json", output_type="str")
    )


def db_write_as_tool_graph() -> SwarmGraph:
    """An agent listing a write-mode SQL node (``db_write_as_tool``).

    A write reachable from a model's tool call is the injection surface this
    design refuses to create, so the combination is an error -- not a silent
    downgrade to a read-only tool.
    """
    spec = _sql_starter_spec()
    graph = _rewrite_node(
        sql_lookup_graph(), "orders_db", sql=spec.model_copy(update={"write": True})
    )
    log_order = SwarmNode(
        id="log_order",
        kind="agent",
        title="Log order",
        intent="Record the order the returned rows describe.",
        position=_pos(1, 0),
        template="chat",
        io=_io("list[json]", "str"),
        agent=AgentSpec(instructions="Record the order.", tools=["sql:orders_db"]),
    )
    nodes = [node for node in graph.nodes if node.id != "format_orders"] + [log_order]
    return graph.model_copy(
        update={
            "id": "db-write-as-tool-graph",
            "exit_node_id": "log_order",
            "nodes": nodes,
            "edges": [SeqEdge(kind="seq", id="e1", source="orders_db", target="log_order")],
        }
    )


def db_tool_unknown_node_graph() -> SwarmGraph:
    """An agent naming a database node that does not exist (``db_tool_unknown_node``)."""
    graph = database_agent_tool_graph()
    nodes = []
    for node in graph.nodes:
        if node.agent is not None:
            node = node.model_copy(
                update={
                    "agent": node.agent.model_copy(update={"tools": ["vector:missing_db_node"]})
                }
            )
        nodes.append(node)
    return graph.model_copy(update={"id": "db-tool-unknown-node-graph", "nodes": nodes})


def db_seed_invalid_graph() -> SwarmGraph:
    """A SQL node whose seed SQL does not run (``db_seed_invalid``).

    Whether a seed is valid SQL cannot be read off the document, so Phase 1
    probes it in an in-memory SQLite database -- the stdlib-only probe that keeps
    a malformed seed from reaching a generated project's mock.
    """
    spec = _sql_starter_spec()
    return _rewrite_node(
        sql_lookup_graph(),
        "orders_db",
        sql=spec.model_copy(
            update={"seed_sql": "CREATE TABLE customers (id INTEGER PRIMARY KEY, name"}
        ),
    )


def db_template_set_graph() -> SwarmGraph:
    """A database node that also carries a ``template`` (``db_template_set``).

    Templates are agent-only, and the emitter would silently ignore one here, so
    it is rejected instead. The Inspector hides the field for database kinds,
    which means such a value can only come from a hand edit or an older document
    -- exactly the case a silent ignore would hide forever.
    """
    return _rewrite_node(sql_lookup_graph(), "orders_db", template="chat")


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
    "sql_lookup": sql_lookup_graph,
    "nosql_query": nosql_query_graph,
    "vector_search": vector_search_graph,
    "database_agent_tool": database_agent_tool_graph,
    "nosql_agent_tool": nosql_agent_tool_graph,
    "two_sql_nodes_same_seed": two_sql_nodes_same_seed_graph,
    "two_sql_nodes_different_seed": two_sql_nodes_different_seed_graph,
}

NEGATIVE_FIXTURES = {
    "cycle": cycle_graph,
    "orphan": orphan_graph,
    "branchless_decision": branchless_decision_graph,
    "joinless_fanout": joinless_fanout_graph,
    # One per database-node code Phase 1 decides from the document alone.
    "db_empty_operation": db_empty_operation_graph,
    "db_placeholder_mismatch": db_placeholder_mismatch_graph,
    "db_input_type_unsupported": db_input_type_unsupported_graph,
    "vector_list_str_input": vector_list_str_input_graph,
    "db_op_io_mismatch": db_op_io_mismatch_graph,
    "db_write_as_tool": db_write_as_tool_graph,
    "db_tool_unknown_node": db_tool_unknown_node_graph,
    "db_seed_invalid": db_seed_invalid_graph,
    "db_template_set": db_template_set_graph,
}

__all__ = [
    "NEGATIVE_FIXTURES",
    "POSITIVE_FIXTURES",
    "branchless_decision_graph",
    "cycle_graph",
    "database_agent_tool_graph",
    "db_empty_operation_graph",
    "db_input_type_unsupported_graph",
    "db_op_io_mismatch_graph",
    "db_placeholder_mismatch_graph",
    "db_seed_invalid_graph",
    "db_template_set_graph",
    "db_tool_unknown_node_graph",
    "db_write_as_tool_graph",
    "decision_branching_graph",
    "fanout_join_graph",
    "joinless_fanout_graph",
    "json_ports_graph",
    "linear_chat_graph",
    "mixed_programmatic_graph",
    "nosql_query_graph",
    "orchestrator_graph",
    "orphan_graph",
    "sql_lookup_graph",
    "two_sql_nodes_different_seed_graph",
    "two_sql_nodes_same_seed_graph",
    "vector_list_str_input_graph",
    "vector_search_graph",
    "websearch_graph",
]
