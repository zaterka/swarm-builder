"""Unit tests for review.py (PLAN.md "Tests" -> Unit -> review.py, and
the codegen suite's negative-fixture requirement)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pytest

from fixtures.graphs import (
    NEGATIVE_FIXTURES,
    POSITIVE_FIXTURES,
    branchless_decision_graph,
    cycle_graph,
    database_agent_tool_graph,
    decision_branching_graph,
    joinless_fanout_graph,
    linear_chat_graph,
    nosql_query_graph,
    orphan_graph,
    sql_lookup_graph,
    two_sql_nodes_different_seed_graph,
    two_sql_nodes_same_seed_graph,
    vector_search_graph,
)
from swarm_builder.compile.review import ReviewResult, review
from swarm_builder.models import (
    AgentSpec,
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
    SwarmGraph,
    SwarmNode,
    VectorDocument,
    VectorSpec,
)
from swarm_builder.templates.database import get_database_entry

UPDATED_AT = datetime(2024, 1, 1, tzinfo=UTC)


def _pos() -> Position:
    return Position(x=0, y=0)


def _io(input_type: str = "str", output_type: str = "str") -> NodeIo:
    return NodeIo(input_type=input_type, output_type=output_type)  # type: ignore[arg-type]


def _codes(result: ReviewResult) -> set[str]:
    """Every finding code in one result, errors and warnings alike.

    The database tests assert on codes rather than on prose, so a reworded
    message cannot make a rule look enforced when it is not.
    """
    return {finding.code for finding in result.errors + result.warnings}


#: Every code review.py's database section can report. Lets "a graph with no
#: database node reports none of them" be one assertion instead of eleven.
_DB_CODES = frozenset(
    {
        "db_empty_operation",
        "db_placeholder_mismatch",
        "db_input_type_unsupported",
        "db_op_io_mismatch",
        "db_write_as_tool",
        "db_tool_unknown_node",
        "db_seed_invalid",
        "db_template_set",
        "db_limit_unset",
        "db_starter_drift",
        "db_separate_mock_instances",
    }
)

#: The one positive fixture that is *expected* to warn, with the codes it
#: warns: two SQL nodes over different seeds get independent mock instances
#: (``db_separate_mock_instances``), and the second node's seed therefore also
#: drifts from the starter it was copied from (``db_starter_drift``). Both are
#: warnings by design -- the graph builds and runs.
_EXPECTED_WARNING_CODES = {
    "two_sql_nodes_different_seed": {"db_separate_mock_instances", "db_starter_drift"},
}


def test_every_positive_fixture_passes_review_with_no_unexpected_findings() -> None:
    for name, build in POSITIVE_FIXTURES.items():
        result = review(build())
        assert result.ok, f"{name} unexpectedly failed review: {result.errors}"
        expected = _EXPECTED_WARNING_CODES.get(name, set())
        assert {f.code for f in result.warnings} == expected, (
            f"{name} had unexpected warnings: {result.warnings}"
        )


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


# ---------------------------------------------------------------------------
# Database nodes (kinds sql / nosql / vector)
#
# Every case starts from a shared fixture in fixtures/graphs.py -- which is built
# from the starter catalog, so it cannot silently drift from a starter -- and
# mutates it with the helpers below when a case needs a shape no fixture has: a
# JSON-parameterised SQL node, a write-mode node no agent references, a
# malformed tool entry. The helpers live here rather than in the shared module
# because those shapes are only observable through review.py.
# ---------------------------------------------------------------------------


def _starter_spec(kind: str) -> SqlSpec | NosqlSpec | VectorSpec:
    """A fresh copy of one kind's starter spec, as a new node receives it."""
    return get_database_entry(kind).starter_spec.model_copy()


def _starter_io(kind: str) -> NodeIo:
    """One kind's mandatory I/O pair, copied the way the Inspector copies it."""
    return get_database_entry(kind).starter_io.model_copy()


def _sql_spec(**updates: object) -> SqlSpec:
    """The SQL starter spec with the named fields replaced."""
    spec = _starter_spec("sql")
    assert isinstance(spec, SqlSpec)
    return spec.model_copy(update=updates)


def _nosql_spec(**updates: object) -> NosqlSpec:
    """The NoSQL starter spec with the named fields replaced."""
    spec = _starter_spec("nosql")
    assert isinstance(spec, NosqlSpec)
    return spec.model_copy(update=updates)


def _vector_spec(**updates: object) -> VectorSpec:
    """The vector starter spec with the named fields replaced."""
    spec = _starter_spec("vector")
    assert isinstance(spec, VectorSpec)
    return spec.model_copy(update=updates)


def _rewrite_node(graph: SwarmGraph, node_id: str, **updates: object) -> SwarmGraph:
    """Replace one node's fields, leaving every other node and edge alone."""
    nodes = [
        node.model_copy(update=updates) if node.id == node_id else node for node in graph.nodes
    ]
    return graph.model_copy(update={"nodes": nodes})


def _exit_node(graph: SwarmGraph) -> SwarmNode:
    """The node the graph's output comes from."""
    return next(node for node in graph.nodes if node.id == graph.exit_node_id)


def _append_node(graph: SwarmGraph, node: SwarmNode) -> SwarmGraph:
    """``graph`` with ``node`` appended after its exit, as the new exit.

    One ``seq`` edge from the old exit is the whole change, so a case can bolt
    one more node onto a shared fixture instead of rebuilding a near-duplicate
    document -- and the appended node becomes the sole sink, which keeps the
    sink/exit type rule satisfied by construction.
    """
    previous_exit = _exit_node(graph)
    return graph.model_copy(
        update={
            "exit_node_id": node.id,
            "nodes": [*graph.nodes, node],
            "edges": [
                *graph.edges,
                SeqEdge(kind="seq", id=f"e_{node.id}", source=previous_exit.id, target=node.id),
            ],
        }
    )


def _appended_agent(graph: SwarmGraph, node_id: str, tools: list[str]) -> SwarmGraph:
    """``graph`` plus a ``chat`` agent node listing ``tools``, wired after its exit.

    The agent's input type is taken from the old exit, so the new edge pairs
    matching ports -- otherwise the graph would fail review for a reason the test
    is not about.
    """
    agent = SwarmNode(
        id=node_id,
        kind="agent",
        title="Use the database",
        intent="Call the database tool this node is given.",
        position=_pos(),
        template="chat",
        io=_io(_exit_node(graph).io.output_type, "str"),
        agent=AgentSpec(instructions="Use the database tool.", tools=tools),
    )
    return _append_node(graph, agent)


def test_a_graph_without_a_database_node_reports_no_database_findings() -> None:
    """The new rules are inert for a document that has no database node."""
    checked = 0
    for name, build in POSITIVE_FIXTURES.items():
        graph = build()
        if any(node.kind in ("sql", "nosql", "vector") for node in graph.nodes):
            continue
        result = review(graph)
        assert _codes(result) & _DB_CODES == set(), f"{name}: {result}"
        checked += 1
    assert checked > 0, "no database-free positive fixture was checked"


def test_a_database_node_must_carry_exactly_its_own_kinds_spec() -> None:
    """The "exactly one spec, the right one" rule now covers the three kinds."""
    assert "kind_spec_mismatch" not in _codes(review(sql_lookup_graph()))
    wrong_spec = _rewrite_node(sql_lookup_graph(), "orders_db", sql=None, vector=_vector_spec())
    assert "kind_spec_mismatch" in _codes(review(wrong_spec))
    both_specs = _rewrite_node(sql_lookup_graph(), "orders_db", vector=_vector_spec())
    assert "kind_spec_mismatch" in _codes(review(both_specs))


def test_db_empty_operation_is_a_hard_error() -> None:
    """One case per trigger the plan names, plus a database kind with no spec."""
    cases: dict[str, SwarmGraph] = {
        "sql-empty-query": NEGATIVE_FIXTURES["db_empty_operation"](),
        "sql-empty-seed": _rewrite_node(
            sql_lookup_graph(), "orders_db", sql=_sql_spec(seed_sql="")
        ),
        "sql-no-spec": _rewrite_node(sql_lookup_graph(), "orders_db", sql=None),
        "nosql-empty-collection": _rewrite_node(
            nosql_query_graph(), "tickets", nosql=_nosql_spec(collection="")
        ),
        "nosql-unset-operation": _rewrite_node(
            nosql_query_graph(),
            "tickets",
            # A validated document cannot carry an unset operation (the field
            # defaults to "find"), so this case builds the model directly: the
            # check has to report it rather than raise on the missing value.
            nosql=NosqlSpec.model_construct(
                collection="tickets", operation=None, filter={}, limit=20, seed=[], note=None
            ),
        ),
        "vector-empty-collection": _rewrite_node(
            vector_search_graph(), "product_docs", vector=_vector_spec(collection="")
        ),
    }
    for name, graph in cases.items():
        result = review(graph)
        assert not result.ok, name
        assert "db_empty_operation" in _codes(result), (name, result)


def test_db_empty_operation_is_not_reported_for_a_declared_operation() -> None:
    for kind, build in (
        ("sql", sql_lookup_graph),
        ("nosql", nosql_query_graph),
        ("vector", vector_search_graph),
    ):
        assert "db_empty_operation" not in _codes(review(build())), kind


def test_db_placeholder_mismatch_is_a_hard_error() -> None:
    """One case per binding rule §4.3 fixes for a SQL node."""
    cases: dict[str, SwarmGraph] = {
        # input: str binds the single parameter :input -- here it binds :customer.
        "str-binds-a-name-nothing-supplies": NEGATIVE_FIXTURES["db_placeholder_mismatch"](),
        "str-binds-no-placeholder": _rewrite_node(
            sql_lookup_graph(),
            "orders_db",
            sql=_sql_spec(query="SELECT id FROM customers ORDER BY id"),
        ),
        # input: json binds the dict's keys -- with no placeholder there is
        # nothing for a key to bind to.
        "json-binds-no-placeholder": _rewrite_node(
            sql_lookup_graph(),
            "orders_db",
            io=_io("json", "list[json]"),
            sql=_sql_spec(query="SELECT id FROM customers ORDER BY id"),
        ),
        # input: list[str] expands ONE :input placeholder -- two is undefined.
        "list[str]-binds-:input-twice": _rewrite_node(
            sql_lookup_graph(),
            "orders_db",
            io=_io("list[str]", "list[json]"),
            sql=_sql_spec(query="SELECT id FROM customers WHERE name = :input OR name = :input"),
        ),
    }
    for name, graph in cases.items():
        result = review(graph)
        assert not result.ok, name
        assert "db_placeholder_mismatch" in _codes(result), (name, result)


def test_db_placeholder_mismatch_is_not_reported_for_a_bindable_query() -> None:
    """One case per §4.3 binding rule, including a ``::`` cast.

    A cast's second colon is not a placeholder: reading ``total::numeric`` as a
    parameter named ``numeric`` would report a perfectly bindable query.
    """
    cases: dict[str, SwarmGraph] = {
        "str-binds-:input": sql_lookup_graph(),
        "str-with-a-cast-and-a-literal-%": _rewrite_node(
            sql_lookup_graph(),
            "orders_db",
            sql=_sql_spec(
                query=(
                    "SELECT o.id, o.total::numeric AS total FROM orders o "
                    "JOIN customers c ON c.id = o.customer_id "
                    "WHERE c.name = :input AND c.city LIKE '%acme%' ORDER BY o.id"
                )
            ),
        ),
        "json-binds-a-key": _rewrite_node(
            sql_lookup_graph(),
            "orders_db",
            io=_io("json", "list[json]"),
            sql=_sql_spec(
                query=(
                    "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id "
                    "WHERE c.name = :customer ORDER BY o.id"
                )
            ),
        ),
        "list[str]-binds-one-:input": _rewrite_node(
            sql_lookup_graph(),
            "orders_db",
            io=_io("list[str]", "list[json]"),
            sql=_sql_spec(query="SELECT id FROM customers WHERE name = :input ORDER BY id"),
        ),
    }
    for name, graph in cases.items():
        assert "db_placeholder_mismatch" not in _codes(review(graph)), name


def test_db_input_type_unsupported_is_a_hard_error_for_every_rejected_cell() -> None:
    """§4.3 rejects ``list[json]`` for all three kinds and ``list[str]`` for vector."""
    cases: dict[str, SwarmGraph] = {
        "sql-list[json]": NEGATIVE_FIXTURES["db_input_type_unsupported"](),
        "nosql-list[json]": _rewrite_node(
            nosql_query_graph(), "tickets", io=_io("list[json]", "list[json]")
        ),
        "vector-list[json]": _rewrite_node(
            vector_search_graph(), "product_docs", io=_io("list[json]", "list[json]")
        ),
        "vector-list[str]": NEGATIVE_FIXTURES["vector_list_str_input"](),
    }
    for name, graph in cases.items():
        result = review(graph)
        assert not result.ok, name
        assert "db_input_type_unsupported" in _codes(result), (name, result)


def test_db_input_type_unsupported_is_not_reported_for_a_bindable_input() -> None:
    cases: dict[str, SwarmGraph] = {
        "sql-str": sql_lookup_graph(),
        "nosql-str": nosql_query_graph(),
        "vector-str": vector_search_graph(),
        "sql-json": _rewrite_node(
            sql_lookup_graph(), "orders_db", io=_io("json", "list[json]")
        ),
        "nosql-json": _rewrite_node(nosql_query_graph(), "tickets", io=_io("json", "list[json]")),
        # §4.3's list binding, which `expand_list_param` implements in the emitted
        # portshape module: one `:input` placeholder, expanded per element.
        "sql-list[str]": _rewrite_node(
            sql_lookup_graph(), "orders_db", io=_io("list[str]", "list[json]")
        ),
        "nosql-list[str]": _rewrite_node(
            nosql_query_graph(), "tickets", io=_io("list[str]", "list[json]")
        ),
    }
    for name, graph in cases.items():
        assert "db_input_type_unsupported" not in _codes(review(graph)), name


def test_db_op_io_mismatch_is_a_hard_error_for_a_non_mandatory_pair() -> None:
    """A database node's ports are not a choice: ``str|json -> list[json]``."""
    cases: dict[str, SwarmGraph] = {
        "nosql-str->json": NEGATIVE_FIXTURES["db_op_io_mismatch"](),
        "sql-str->json": _rewrite_node(sql_lookup_graph(), "orders_db", io=_io("str", "json")),
        "sql-json->json": _rewrite_node(sql_lookup_graph(), "orders_db", io=_io("json", "json")),
        "vector-str->str": _rewrite_node(
            vector_search_graph(), "product_docs", io=_io("str", "str")
        ),
    }
    for name, graph in cases.items():
        result = review(graph)
        assert not result.ok, name
        assert "db_op_io_mismatch" in _codes(result), (name, result)


def test_db_op_io_mismatch_is_not_reported_for_the_mandatory_pair() -> None:
    for kind, build in (
        ("sql", sql_lookup_graph),
        ("nosql", nosql_query_graph),
        ("vector", vector_search_graph),
    ):
        assert "db_op_io_mismatch" not in _codes(review(build())), kind


def test_db_seed_invalid_is_a_hard_error() -> None:
    """The declared seed *is* the mock, so it has to parse for its kind."""
    cases: dict[str, SwarmGraph] = {
        "sql-unterminated-statement": NEGATIVE_FIXTURES["db_seed_invalid"](),
        "sql-not-sql-at-all": _rewrite_node(
            sql_lookup_graph(), "orders_db", sql=_sql_spec(seed_sql="this is not SQL at all")
        ),
        "sql-inserts-into-a-table-it-never-created": _rewrite_node(
            sql_lookup_graph(),
            "orders_db",
            sql=_sql_spec(seed_sql="INSERT INTO customers (id) VALUES (1);\n"),
        ),
        "sql-nul-character": _rewrite_node(
            sql_lookup_graph(),
            "orders_db",
            sql=_sql_spec(seed_sql="CREATE TABLE t (a TEXT);\n\x00\n"),
        ),
        "nosql-entry-is-not-an-object": _rewrite_node(
            nosql_query_graph(),
            "tickets",
            # The document layer types this field as list[dict], so only a model
            # built directly can carry a non-object entry: the check must report
            # it rather than raise on it.
            nosql=NosqlSpec.model_construct(
                collection="tickets",
                operation="find",
                filter={},
                limit=20,
                seed=["not a document"],
                note=None,
            ),
        ),
        "vector-entry-with-no-id": _rewrite_node(
            vector_search_graph(),
            "product_docs",
            vector=_vector_spec(seed=[VectorDocument(id="", text="A document without an id.")]),
        ),
        "vector-entry-with-no-text": _rewrite_node(
            vector_search_graph(),
            "product_docs",
            vector=_vector_spec(seed=[VectorDocument(id="doc-1", text="   ")]),
        ),
        "nosql-seed-is-not-a-list": _rewrite_node(
            nosql_query_graph(),
            "tickets",
            # Same reason as above, and this case also has to survive the seed
            # keys the shared-instance rule computes for every database node.
            nosql=NosqlSpec.model_construct(
                collection="tickets",
                operation="find",
                filter={},
                limit=20,
                seed=None,
                note=None,
            ),
        ),
    }
    for name, graph in cases.items():
        result = review(graph)
        assert not result.ok, name
        assert "db_seed_invalid" in _codes(result), (name, result)


def test_db_seed_invalid_is_not_reported_for_a_parseable_seed() -> None:
    """Includes an *empty* document seed, which is legal rather than invalid."""
    cases: dict[str, SwarmGraph] = {
        "sql-starter": sql_lookup_graph(),
        "nosql-starter": nosql_query_graph(),
        "vector-starter": vector_search_graph(),
        "nosql-empty-seed": _rewrite_node(
            nosql_query_graph(), "tickets", nosql=_nosql_spec(seed=[])
        ),
        "vector-empty-seed": _rewrite_node(
            vector_search_graph(), "product_docs", vector=_vector_spec(seed=[])
        ),
    }
    for name, graph in cases.items():
        assert "db_seed_invalid" not in _codes(review(graph)), name


def test_db_template_set_is_a_hard_error() -> None:
    """Templates are agent-only; a database node carrying one is a stale field."""
    cases: dict[str, SwarmGraph] = {
        "sql": NEGATIVE_FIXTURES["db_template_set"](),
        "nosql": _rewrite_node(nosql_query_graph(), "tickets", template="chat"),
        "vector": _rewrite_node(vector_search_graph(), "product_docs", template="websearch"),
    }
    for name, graph in cases.items():
        result = review(graph)
        assert not result.ok, name
        assert "db_template_set" in _codes(result), (name, result)


def test_db_template_set_is_not_reported_for_an_agent_node() -> None:
    for name in ("linear_chat", "websearch", "orchestrator"):
        assert "db_template_set" not in _codes(review(POSITIVE_FIXTURES[name]())), name


def test_db_write_as_tool_is_a_hard_error() -> None:
    """A write reachable from a model's tool call is what this design refuses."""
    cases: dict[str, SwarmGraph] = {
        "sql-write": NEGATIVE_FIXTURES["db_write_as_tool"](),
        "nosql-insert_one": _appended_agent(
            _rewrite_node(
                nosql_query_graph(), "tickets", nosql=_nosql_spec(operation="insert_one")
            ),
            "log_ticket",
            ["nosql:tickets"],
        ),
    }
    for name, graph in cases.items():
        result = review(graph)
        assert not result.ok, name
        assert "db_write_as_tool" in _codes(result), (name, result)


def test_db_write_as_tool_is_not_reported_for_a_read_only_or_unreferenced_node() -> None:
    cases: dict[str, SwarmGraph] = {
        "vector-read-tool": database_agent_tool_graph(),
        "nosql-read-tool": _appended_agent(nosql_query_graph(), "read_ticket", ["nosql:tickets"]),
        "sql-write-without-a-tool": _rewrite_node(
            sql_lookup_graph(), "orders_db", sql=_sql_spec(write=True)
        ),
    }
    for name, graph in cases.items():
        assert "db_write_as_tool" not in _codes(review(graph)), name


def test_db_tool_unknown_node_is_a_hard_error() -> None:
    cases: dict[str, SwarmGraph] = {
        "names-no-node": NEGATIVE_FIXTURES["db_tool_unknown_node"](),
        "malformed-entry": _appended_agent(vector_search_graph(), "search_docs", ["sql:"]),
        "names-a-node-of-another-kind": _appended_agent(
            vector_search_graph(), "search_docs", ["sql:product_docs"]
        ),
    }
    for name, graph in cases.items():
        result = review(graph)
        assert not result.ok, name
        assert "db_tool_unknown_node" in _codes(result), (name, result)


def test_db_tool_unknown_node_is_not_reported_for_a_catalog_tool_or_a_real_node() -> None:
    """``tools`` also carries catalog names; only the namespaced form is checked."""
    cases: dict[str, SwarmGraph] = {
        "catalog-tool-name": _appended_agent(vector_search_graph(), "search_docs", ["web_search"]),
        "matching-vector-entry": database_agent_tool_graph(),
        "matching-nosql-entry": _appended_agent(
            nosql_query_graph(), "read_ticket", ["nosql:tickets"]
        ),
    }
    for name, graph in cases.items():
        assert "db_tool_unknown_node" not in _codes(review(graph)), name


def test_db_limit_unset_is_a_warning() -> None:
    """``limit <= 0`` reads as "no limit", which is legal but rarely intended."""
    for limit in (0, -1):
        result = review(
            _rewrite_node(nosql_query_graph(), "tickets", nosql=_nosql_spec(limit=limit))
        )
        assert result.ok, result.errors
        assert "db_limit_unset" in _codes(result), limit


def test_db_limit_unset_is_not_reported_for_a_positive_limit() -> None:
    result = review(nosql_query_graph())
    assert "db_limit_unset" not in _codes(result)


def test_db_starter_drift_is_a_warning() -> None:
    """The starter is the example the node was created from; a difference warns."""
    cases: dict[str, SwarmGraph] = {
        "sql-seed": two_sql_nodes_different_seed_graph(),
        "sql-query": _rewrite_node(
            sql_lookup_graph(),
            "orders_db",
            sql=_sql_spec(
                query=(
                    "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id "
                    "WHERE c.name = :input ORDER BY o.id"
                )
            ),
        ),
        "nosql-limit": _rewrite_node(nosql_query_graph(), "tickets", nosql=_nosql_spec(limit=5)),
        "vector-top_k": _rewrite_node(
            vector_search_graph(), "product_docs", vector=_vector_spec(top_k=1)
        ),
    }
    for name, graph in cases.items():
        result = review(graph)
        assert result.ok, (name, result.errors)
        assert "db_starter_drift" in _codes(result), (name, result)


def test_db_starter_drift_is_not_reported_for_a_starter_node() -> None:
    for name in (
        "sql_lookup",
        "nosql_query",
        "vector_search",
        "database_agent_tool",
        "two_sql_nodes_same_seed",
    ):
        assert "db_starter_drift" not in _codes(review(POSITIVE_FIXTURES[name]())), name


def test_db_starter_drift_is_skipped_when_the_catalog_cannot_be_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check degrades to nothing; it never becomes an import-time dependency.

    ``None`` in ``sys.modules`` makes ``import`` of that module raise, which is
    the catalog being unavailable at call time. The same graph is reviewed first
    unpatched, so the assertion is that the check was *skipped* rather than that
    it had nothing to say.
    """
    graph = two_sql_nodes_different_seed_graph()
    assert "db_starter_drift" in _codes(review(graph))

    monkeypatch.setitem(sys.modules, "swarm_builder.templates.database", None)
    result = review(graph)

    assert result.ok, result.errors
    assert "db_starter_drift" not in _codes(result)
    assert "db_separate_mock_instances" in _codes(result)


def test_db_separate_mock_instances_is_a_warning() -> None:
    """Different seeds mean independent mocks, so a write in one is not visible."""
    result = review(two_sql_nodes_different_seed_graph())
    assert result.ok, result.errors
    separate = [f for f in result.warnings if f.code == "db_separate_mock_instances"]
    assert len(separate) == 1
    assert set(separate[0].node_ids) == {"orders_db", "orders_db_again"}

    other_kind = _append_node(
        nosql_query_graph(),
        SwarmNode(
            id="tickets_archive",
            kind="nosql",
            title="Archived tickets",
            intent="Find archived support tickets.",
            position=_pos(),
            io=_starter_io("nosql"),
            nosql=_nosql_spec(
                seed=[*_nosql_spec().seed, {"_id": "t-4", "status": "archived"}]
            ),
        ),
    )
    other_result = review(other_kind)
    assert other_result.ok, other_result.errors
    assert any(f.code == "db_separate_mock_instances" for f in other_result.warnings)


def test_db_separate_mock_instances_is_not_reported_for_one_shared_seed() -> None:
    """Two nodes over the same seed share one instance, which is the documented default."""
    result = review(two_sql_nodes_same_seed_graph())
    assert "db_separate_mock_instances" not in _codes(result)


def test_every_database_finding_names_the_nodes_it_is_about() -> None:
    """A finding a caller cannot trace to a node is a finding nobody can act on."""
    checked = 0
    for build in {**POSITIVE_FIXTURES, **NEGATIVE_FIXTURES}.values():
        result = review(build())
        for finding in result.errors + result.warnings:
            if finding.code not in _DB_CODES:
                continue
            assert finding.node_ids, finding
            checked += 1
    assert checked > 0, "no database finding was produced by any fixture"


@pytest.mark.parametrize("code", sorted(code for code in NEGATIVE_FIXTURES if code in _DB_CODES))
def test_each_database_negative_fixture_reports_the_code_it_is_named_after(code: str) -> None:
    """A fixture named after its code is this suite's coverage claim for it."""
    assert code in _codes(review(NEGATIVE_FIXTURES[code]()))


def test_the_starter_catalog_is_imported_lazily_and_only_for_a_database_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """review.py reaches the catalog on demand, never at import time.

    Both graphs are built before the module is dropped from ``sys.modules``,
    because building a database fixture reads the catalog itself -- so what is
    observed here is review's own behaviour, not the fixtures'.
    """
    graph_without_db = linear_chat_graph()
    graph_with_db = sql_lookup_graph()

    monkeypatch.delitem(sys.modules, "swarm_builder.templates.database")

    assert review(graph_without_db).ok
    assert "swarm_builder.templates.database" not in sys.modules

    assert review(graph_with_db).ok
    assert "swarm_builder.templates.database" in sys.modules


def test_a_delegate_target_must_be_an_agent_node() -> None:
    """Delegation compiles to an *import*, so a non-agent target is unimportable.

    An orchestrator's factory emits ``from swarm_workflow.agents.<child> import
    build_agent`` for every ``delegates_to`` entry, and only ``agent`` nodes get an
    ``agents/`` module. Before this check existed, a hand-authored document (or the
    canvas, which happily draws a delegate edge to anything) could name a database
    node here and the generated project failed its keyless import with
    ``ModuleNotFoundError`` -- after the compile had already reported success as far
    as Phase 1 was concerned.
    """
    graph = database_agent_tool_graph()
    db_node = next(node for node in graph.nodes if node.kind == "vector")
    agent_node = next(node for node in graph.nodes if node.kind == "agent")
    broken = graph.model_copy(
        update={
            "nodes": [
                node.model_copy(
                    update={
                        "agent": node.agent.model_copy(
                            update={"delegates_to": [db_node.id]}
                        )
                    }
                )
                if node.id == agent_node.id
                else node
                for node in graph.nodes
            ]
        }
    )

    result = review(broken)

    assert not result.ok
    finding = next(f for f in result.errors if f.code == "delegate_target_not_agent")
    assert finding.node_ids == (agent_node.id, db_node.id)


def test_a_delegate_target_that_does_not_exist_is_the_same_error() -> None:
    graph = database_agent_tool_graph()
    agent_node = next(node for node in graph.nodes if node.kind == "agent")
    broken = graph.model_copy(
        update={
            "nodes": [
                node.model_copy(
                    update={"agent": node.agent.model_copy(update={"delegates_to": ["ghost"]})}
                )
                if node.id == agent_node.id
                else node
                for node in graph.nodes
            ]
        }
    )

    result = review(broken)

    assert not result.ok
    assert any(f.code == "delegate_target_not_agent" for f in result.errors)
