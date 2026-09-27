"""Unit tests for the graph-document schema (PLAN.md "Tests" -> Unit ->
models.py, and the JSON round-trip acceptance criterion)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from swarm_builder.known_models import (
    THINKING_MODEL_MAX_OUTPUT_TOKENS,
    default_max_output_tokens,
    resolve_max_output_tokens,
)
from swarm_builder.models import (
    PORT_TYPE_ANNOTATIONS,
    PORT_TYPE_IMPORTS,
    REDUCER_FUNCTIONS,
    AgentSpec,
    DecisionBranch,
    DecisionSpec,
    JoinSpec,
    ModelSelection,
    NodeIo,
    NodeKind,
    NosqlSpec,
    PortType,
    Position,
    ProgrammaticSpec,
    SeqEdge,
    SqlSpec,
    StateField,
    SwarmGraph,
    SwarmNode,
    VectorDocument,
    VectorSpec,
)

UPDATED_AT = datetime(2024, 1, 1, tzinfo=UTC)


def _three_node_fixture() -> dict:
    """websearch -> agent summarize -> programmatic format, per PLAN.md's
    canonical example (acceptance criterion 3, Tests -> Unit)."""
    return {
        "version": 1,
        "id": "graph-1",
        "name": "Research and format",
        "entryNodeId": "search",
        "exitNodeId": "format",
        "stateFields": [
            {"name": "topic", "type": "str", "default": '""'},
        ],
        "nodes": [
            {
                "id": "search",
                "kind": "agent",
                "title": "Web search",
                "intent": "Search the web for the given topic.",
                "position": {"x": 0, "y": 0},
                "template": "websearch",
                "io": {"inputType": "str", "outputType": "str"},
                "reads": [],
                "writes": ["topic"],
                "agent": {
                    "instructions": "Search the web and report findings.",
                    "tools": [],
                    "delegatesTo": [],
                },
            },
            {
                "id": "summarize",
                "kind": "agent",
                "title": "Summarize",
                "intent": "Summarize the search findings.",
                "position": {"x": 200, "y": 0},
                "template": "chat",
                "io": {"inputType": "str", "outputType": "str"},
                "reads": ["topic"],
                "writes": [],
                "agent": {
                    "instructions": "Summarize the input in two sentences.",
                },
            },
            {
                "id": "format",
                "kind": "programmatic",
                "title": "Format",
                "intent": "Format the summary as JSON.",
                "position": {"x": 400, "y": 0},
                "io": {"inputType": "str", "outputType": "json"},
                "reads": [],
                "writes": [],
                "programmatic": {
                    "needs": [],
                    "signatureHint": "def run(summary: str) -> dict[str, Any]",
                },
            },
        ],
        "edges": [
            {
                "kind": "seq",
                "id": "e1",
                "source": "search",
                "target": "summarize",
            },
            {
                "kind": "seq",
                "id": "e2",
                "source": "summarize",
                "target": "format",
            },
        ],
        "updatedAt": "2024-01-01T00:00:00Z",
    }


class TestValidDocument:
    def test_accepts_the_three_node_fixture(self) -> None:
        graph = SwarmGraph.model_validate(_three_node_fixture())
        assert graph.version == 1
        assert [n.id for n in graph.nodes] == ["search", "summarize", "format"]
        assert graph.nodes[0].template == "websearch"
        assert graph.nodes[2].io.output_type == "json"

    def test_populate_by_name_accepts_snake_case_too(self) -> None:
        payload = _three_node_fixture()
        payload["entry_node_id"] = payload.pop("entryNodeId")
        payload["exit_node_id"] = payload.pop("exitNodeId")
        graph = SwarmGraph.model_validate(payload)
        assert graph.entry_node_id == "search"
        assert graph.exit_node_id == "format"

    def test_serializes_with_camel_case_aliases(self) -> None:
        graph = SwarmGraph.model_validate(_three_node_fixture())
        dumped = graph.model_dump(mode="json", by_alias=True)
        assert "entryNodeId" in dumped
        assert "entry_node_id" not in dumped
        assert dumped["nodes"][2]["io"]["outputType"] == "json"


class TestRejections:
    def test_rejects_unknown_node_kind(self) -> None:
        payload = _three_node_fixture()
        payload["nodes"][0]["kind"] = "wizard"
        with pytest.raises(ValidationError):
            SwarmGraph.model_validate(payload)

    def test_rejects_bad_port_type(self) -> None:
        payload = _three_node_fixture()
        payload["nodes"][0]["io"]["outputType"] = "int"
        with pytest.raises(ValidationError):
            SwarmGraph.model_validate(payload)

    def test_rejects_dangling_edge_source(self) -> None:
        payload = _three_node_fixture()
        payload["edges"][0]["source"] = "does-not-exist"
        with pytest.raises(ValidationError, match="unknown source"):
            SwarmGraph.model_validate(payload)

    def test_rejects_dangling_edge_target(self) -> None:
        payload = _three_node_fixture()
        payload["edges"][1]["target"] = "does-not-exist"
        with pytest.raises(ValidationError, match="unknown target"):
            SwarmGraph.model_validate(payload)

    def test_rejects_fanout_edge_with_no_join_node_id(self) -> None:
        payload = _three_node_fixture()
        payload["edges"].append(
            {
                "kind": "fanout",
                "id": "e3",
                "source": "search",
                "target": "summarize",
                # joinNodeId deliberately omitted.
            }
        )
        with pytest.raises(ValidationError, match="joinNodeId|join_node_id"):
            SwarmGraph.model_validate(payload)

    def test_rejects_unknown_extra_field(self) -> None:
        payload = _three_node_fixture()
        payload["somethingUnexpected"] = True
        with pytest.raises(ValidationError):
            SwarmGraph.model_validate(payload)

    def test_rejects_duplicate_node_ids(self) -> None:
        payload = _three_node_fixture()
        payload["nodes"][1]["id"] = payload["nodes"][0]["id"]
        with pytest.raises(ValidationError, match="duplicate node id"):
            SwarmGraph.model_validate(payload)

    def test_rejects_unknown_entry_node_id(self) -> None:
        payload = _three_node_fixture()
        payload["entryNodeId"] = "does-not-exist"
        with pytest.raises(ValidationError, match="entry_node_id"):
            SwarmGraph.model_validate(payload)

    def test_rejects_unknown_exit_node_id(self) -> None:
        payload = _three_node_fixture()
        payload["exitNodeId"] = "does-not-exist"
        with pytest.raises(ValidationError, match="exit_node_id"):
            SwarmGraph.model_validate(payload)

    def test_rejects_unsupported_version(self) -> None:
        payload = _three_node_fixture()
        payload["version"] = 2
        with pytest.raises(ValidationError):
            SwarmGraph.model_validate(payload)


def _full_document_fixture() -> SwarmGraph:
    """A richer document exercising every node/edge kind and optional
    field, used for the round-trip test so the byte-exactness guarantee
    is not accidentally only proven for the simple fixture."""
    return SwarmGraph(
        id="graph-2",
        name="Branch and join",
        entry_node_id="search",
        exit_node_id="join1",
        state_fields=[
            StateField(name="bucket", type="str", default='""', description="d"),
        ],
        nodes=[
            SwarmNode(
                id="search",
                kind="agent",
                title="Web search",
                intent="Search the web.",
                position=Position(x=0, y=0),
                template="websearch",
                io=NodeIo(input_type="str", output_type="str"),
                writes=["bucket"],
                agent=AgentSpec(
                    instructions="Search.",
                    tools=["web_search"],
                    delegates_to=["child"],
                    output_schema={"type": "object"},
                ),
            ),
            SwarmNode(
                id="classify",
                kind="decision",
                title="Classify",
                intent="Classify the result.",
                position=Position(x=100, y=0),
                io=NodeIo(input_type="str", output_type="str"),
                decision=DecisionSpec(
                    branches=[
                        DecisionBranch(match="big", target_node_id="big"),
                        DecisionBranch(match="small", target_node_id="small"),
                    ],
                    note="classify by length",
                ),
            ),
            SwarmNode(
                id="big",
                kind="programmatic",
                title="Big",
                intent="Handle big.",
                position=Position(x=200, y=-50),
                io=NodeIo(input_type="str", output_type="str"),
                programmatic=ProgrammaticSpec(
                    needs=["httpx"], signature_hint="def run(x: str) -> str"
                ),
            ),
            SwarmNode(
                id="small",
                kind="programmatic",
                title="Small",
                intent="Handle small.",
                position=Position(x=200, y=50),
                io=NodeIo(input_type="str", output_type="str"),
            ),
            SwarmNode(
                id="join1",
                kind="join",
                title="Join",
                intent="Join branches.",
                position=Position(x=300, y=0),
                io=NodeIo(input_type="str", output_type="list[str]"),
                join=JoinSpec(reducer="list_append"),
            ),
            SwarmNode(
                id="child",
                kind="agent",
                title="Child",
                intent="Delegate target.",
                position=Position(x=0, y=100),
                template="chat",
                io=NodeIo(input_type="str", output_type="str"),
                agent=AgentSpec(instructions="Help the orchestrator."),
            ),
        ],
        edges=[
            {"kind": "seq", "id": "e1", "source": "search", "target": "classify"},
            {
                "kind": "branch",
                "id": "e2",
                "source": "classify",
                "target": "big",
                "match": "big",
            },
            {
                "kind": "branch",
                "id": "e3",
                "source": "classify",
                "target": "small",
                "match": "small",
            },
            {
                "kind": "fanout",
                "id": "e4",
                "source": "big",
                "target": "join1",
                "join_node_id": "join1",
            },
            {"kind": "join", "id": "e5", "source": "small", "target": "join1"},
            {"kind": "delegate", "id": "e6", "source": "search", "target": "child"},
        ],
        model=ModelSelection(provider="amazon-bedrock", model="us.anthropic.claude-opus-5"),
        updated_at=UPDATED_AT,
    )


class TestJsonRoundTrip:
    def test_full_document_round_trips_byte_for_byte(self) -> None:
        graph = _full_document_fixture()
        first_json = graph.model_dump_json(by_alias=True)

        reloaded = SwarmGraph.model_validate(json.loads(first_json))
        second_json = reloaded.model_dump_json(by_alias=True)

        assert first_json == second_json

    def test_three_node_fixture_round_trips_byte_for_byte(self) -> None:
        payload = _three_node_fixture()
        graph = SwarmGraph.model_validate(payload)
        first_json = graph.model_dump_json(by_alias=True)

        reloaded = SwarmGraph.model_validate(json.loads(first_json))
        second_json = reloaded.model_dump_json(by_alias=True)

        assert first_json == second_json

    def test_reducer_lookup_table_has_exactly_four_entries(self) -> None:
        assert len(REDUCER_FUNCTIONS) == 4
        assert set(REDUCER_FUNCTIONS) == {"list_append", "list_extend", "dict_update", "sum"}

    def test_port_type_annotation_table_is_exact(self) -> None:
        # fact 17: 'json' must never be interpolated verbatim into an
        # annotation -- it must map to a real Python type.
        assert PORT_TYPE_ANNOTATIONS == {
            "str": "str",
            "list[str]": "list[str]",
            "json": "dict[str, Any]",
            "list[json]": "list[dict[str, Any]]",
        }

    def test_port_type_import_table_matches_annotation_table(self) -> None:
        assert set(PORT_TYPE_IMPORTS) == set(PORT_TYPE_ANNOTATIONS)
        assert PORT_TYPE_IMPORTS["json"] == "from typing import Any"
        assert PORT_TYPE_IMPORTS["list[json]"] == "from typing import Any"
        assert PORT_TYPE_IMPORTS["str"] is None
        assert PORT_TYPE_IMPORTS["list[str]"] is None

    def test_the_port_tables_cover_every_port_type_member(self) -> None:
        """Neither table may lag the ``PortType`` literal.

        A missing row is not a type error -- it is a ``KeyError`` (or a silently
        wrong annotation) inside the emitter, at compile time, for a document
        that validated. Deriving the expected keys from the literal is what makes
        this check keep working when a fifth port type is added.
        """
        members = set(get_args(PortType))
        assert members == {"str", "json", "list[str]", "list[json]"}
        assert set(PORT_TYPE_ANNOTATIONS) == members
        assert set(PORT_TYPE_IMPORTS) == members

    def test_node_kind_covers_the_three_database_kinds(self) -> None:
        assert set(get_args(NodeKind)) == {
            "agent",
            "programmatic",
            "decision",
            "join",
            "sql",
            "nosql",
            "vector",
        }


# ---------------------------------------------------------------------------
# Database node specs (PLAN-DB-NODES.md §4.1)
# ---------------------------------------------------------------------------


def _sql_node(**overrides: object) -> SwarmNode:
    """One ``kind="sql"`` node, with the starter's shape."""
    payload: dict[str, object] = {
        "id": "orders_db",
        "kind": "sql",
        "title": "Orders",
        "intent": "Read the orders of the named customer.",
        "position": Position(x=0, y=0),
        "io": NodeIo(input_type="str", output_type="list[json]"),
        "sql": SqlSpec(
            query="SELECT id FROM orders WHERE customer = :input",
            seed_sql="CREATE TABLE orders (id INTEGER);\n",
            write=False,
            note="the example",
        ),
    }
    payload.update(overrides)
    return SwarmNode(**payload)  # type: ignore[arg-type]


def _database_document_fixture() -> SwarmGraph:
    """A document holding all three new specs, for the round-trip tests.

    Deliberately a real document rather than three isolated models: the
    acceptance criterion is that a *document* with database nodes round-trips,
    which is what the frontend and the store do on every save and load.
    """
    nosql_node = SwarmNode(
        id="tickets",
        kind="nosql",
        title="Tickets",
        intent="Find the tickets whose status is the input.",
        position=Position(x=200, y=0),
        io=NodeIo(input_type="str", output_type="list[json]"),
        nosql=NosqlSpec(
            collection="tickets",
            operation="find",
            filter={"status": "$input", "priority": {"$gte": 2}},
            limit=20,
            seed=[{"_id": "t-1", "status": "open", "tags": ["auth"]}],
            note="the example",
        ),
    )
    vector_node = SwarmNode(
        id="product_docs",
        kind="vector",
        title="Product docs",
        intent="Search the product documentation for the input text.",
        position=Position(x=400, y=0),
        io=NodeIo(input_type="str", output_type="list[json]"),
        vector=VectorSpec(
            collection="product_docs",
            top_k=4,
            min_score=0.25,
            seed=[
                VectorDocument(
                    id="doc-1", text="A blurb.", metadata={"product": "Nimbus Router X1"}
                )
            ],
            note="the example",
        ),
    )
    return SwarmGraph(
        id="database-graph",
        name="Database nodes",
        entry_node_id="orders_db",
        exit_node_id="product_docs",
        state_fields=[StateField(name="rows", type="list[json]", default="None")],
        nodes=[_sql_node(), nosql_node, vector_node],
        edges=[
            SeqEdge(kind="seq", id="e1", source="orders_db", target="tickets"),
            SeqEdge(kind="seq", id="e2", source="tickets", target="product_docs"),
        ],
        updated_at=UPDATED_AT,
    )


class TestDatabaseSpecs:
    def test_all_three_specs_round_trip_losslessly(self) -> None:
        graph = _database_document_fixture()
        first_json = graph.model_dump_json(by_alias=True)

        reloaded = SwarmGraph.model_validate(json.loads(first_json))
        second_json = reloaded.model_dump_json(by_alias=True)

        assert first_json == second_json
        assert [node.kind for node in reloaded.nodes] == ["sql", "nosql", "vector"]

    def test_a_database_documents_field_order_is_stable(self) -> None:
        """``dump -> validate -> dump`` must not reorder anything."""
        dumped = json.loads(_database_document_fixture().model_dump_json(by_alias=True))
        assert list(dumped) == [
            "version",
            "id",
            "name",
            "entryNodeId",
            "exitNodeId",
            "stateFields",
            "nodes",
            "edges",
            "model",
            "updatedAt",
        ]
        assert list(dumped["nodes"][0]) == [
            "id",
            "kind",
            "title",
            "intent",
            "position",
            "template",
            "io",
            "reads",
            "writes",
            "agent",
            "programmatic",
            "decision",
            "join",
            "sql",
            "nosql",
            "vector",
        ]
        assert list(dumped["nodes"][0]["sql"]) == ["query", "seedSql", "write", "note"]
        assert list(dumped["nodes"][1]["nosql"]) == [
            "collection",
            "operation",
            "filter",
            "limit",
            "seed",
            "note",
        ]
        assert list(dumped["nodes"][2]["vector"]) == [
            "collection",
            "topK",
            "minScore",
            "seed",
            "note",
        ]
        assert list(dumped["nodes"][2]["vector"]["seed"][0]) == ["id", "text", "metadata"]

    def test_camel_case_aliases_resolve_for_the_database_specs(self) -> None:
        camel = {
            "id": "orders_db",
            "kind": "sql",
            "title": "Orders",
            "intent": "Read orders.",
            "position": {"x": 0, "y": 0},
            "io": {"inputType": "str", "outputType": "list[json]"},
            "sql": {"query": "SELECT 1 AS one", "seedSql": "CREATE TABLE t (a);", "note": "n"},
        }
        snake = {
            "id": "orders_db",
            "kind": "sql",
            "title": "Orders",
            "intent": "Read orders.",
            "position": {"x": 0, "y": 0},
            "io": {"input_type": "str", "output_type": "list[json]"},
            "sql": {"query": "SELECT 1 AS one", "seed_sql": "CREATE TABLE t (a);", "note": "n"},
        }
        from_camel = SwarmNode.model_validate(camel)
        from_snake = SwarmNode.model_validate(snake)
        assert from_camel == from_snake
        assert from_camel.sql is not None
        assert from_camel.sql.seed_sql == "CREATE TABLE t (a);"
        assert from_camel.sql.write is False

        vector = SwarmNode.model_validate(
            {
                "id": "product_docs",
                "kind": "vector",
                "title": "Docs",
                "intent": "Search docs.",
                "position": {"x": 0, "y": 0},
                "io": {"inputType": "str", "outputType": "list[json]"},
                "vector": {
                    "collection": "product_docs",
                    "topK": 9,
                    "minScore": 0.5,
                    "seed": [{"id": "doc-1", "text": "blurb"}],
                },
            }
        )
        assert vector.vector is not None
        assert (vector.vector.top_k, vector.vector.min_score) == (9, 0.5)
        assert vector.vector.seed[0].metadata == {}

    @pytest.mark.parametrize(
        ("model", "payload"),
        [
            (SqlSpec, {"query": "SELECT 1", "seed_sql": "CREATE TABLE t (a);", "writes": True}),
            (SqlSpec, {"query": "SELECT 1", "seed_sql": "", "seedSql": "CREATE TABLE t (a);"}),
            (NosqlSpec, {"collection": "tickets", "operation": "find", "limit": 5, "limits": 5}),
            (NosqlSpec, {"collection": "tickets", "operation": "aggregate"}),
            (VectorSpec, {"collection": "docs", "top_k": 4, "topK": 4}),
            (VectorDocument, {"id": "doc-1", "text": "blurb", "payload": {}}),
        ],
    )
    def test_extra_or_unknown_keys_are_forbidden(
        self, model: type, payload: dict[str, object]
    ) -> None:
        """``extra="forbid"`` must hold inside the new specs too.

        A stray key is a typo or a stale frontend build, and the one place a
        database spec has no other guard is here: a misspelled ``topK`` would
        otherwise validate and silently keep the default.
        """
        with pytest.raises(ValidationError):
            model.model_validate(payload)

    def test_a_stray_key_on_the_node_itself_is_still_forbidden(self) -> None:
        payload = json.loads(_database_document_fixture().model_dump_json(by_alias=True))
        payload["nodes"][0]["database"] = {}
        with pytest.raises(ValidationError):
            SwarmGraph.model_validate(payload)

    def test_the_seed_is_required_to_be_explicit_on_a_sql_node(self) -> None:
        """A missing ``seed_sql`` must be a validation error, not a default.

        The document has to determine the mock: an absent seed would make the
        mock's contents a compile-time decision instead of the document's.
        """
        with pytest.raises(ValidationError, match="seed_sql|seedSql"):
            SqlSpec.model_validate({"query": "SELECT 1"})
        with pytest.raises(ValidationError, match="collection"):
            NosqlSpec.model_validate({})
        with pytest.raises(ValidationError, match="collection"):
            VectorSpec.model_validate({})

    def test_the_documented_defaults_are_the_documented_ones(self) -> None:
        assert SqlSpec(query="SELECT 1", seed_sql="").write is False
        assert SqlSpec(query="SELECT 1", seed_sql="").note is None
        nosql = NosqlSpec(collection="tickets")
        assert (nosql.operation, nosql.filter, nosql.limit, nosql.seed, nosql.note) == (
            "find",
            {},
            20,
            [],
            None,
        )
        vector = VectorSpec(collection="product_docs")
        assert (vector.top_k, vector.min_score, vector.seed, vector.note) == (4, 0.0, [], None)
        assert VectorDocument(id="doc-1", text="blurb").metadata == {}

    def test_a_database_node_needs_no_template_to_validate(self) -> None:
        """Templates are agent-only, and the schema does not require one here.

        Phase 1 rejects a database node that carries a template; the schema's job
        is only to make the field optional, which is what lets the document
        round-trip either way.
        """
        assert _sql_node().template is None
        assert _sql_node(template="chat").template == "chat"


# ---------------------------------------------------------------------------
# default_max_output_tokens (PLAN-ATTACHMENTS-CLARIFY.md, provider testing)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # Thinking models whose provider default output budget is too small for a
        # large structured answer: measured at 5135 output tokens for one draft.
        ("claude-sonnet-5", THINKING_MODEL_MAX_OUTPUT_TOKENS),
        ("anthropic:claude-opus-5", THINKING_MODEL_MAX_OUTPUT_TOKENS),
        ("us.anthropic.claude-sonnet-4-6", THINKING_MODEL_MAX_OUTPUT_TOKENS),
        ("EU.ANTHROPIC.CLAUDE-HAIKU-4-5", THINKING_MODEL_MAX_OUTPUT_TOKENS),
        # Every other family now gets an explicit budget too -- leaving the
        # provider default in place was the too-small value this bug reported.
        ("deepseek-flash", 8192),
        ("deepseek-v4.1-flash", 8192),
        ("gpt-5.6-sol", 32768),
        ("gemini-2.5-flash", 32768),
        ("groq:llama-3.3-70b", 16384),
        # Unknown/custom endpoints get the conservative floor rather than None.
        ("", 16384),
        ("custom:some-proxy-model", 16384),
    ],
)
def test_default_max_output_tokens_only_overrides_where_it_must(
    model: str, expected: int | None
) -> None:
    assert default_max_output_tokens(model) == expected


def test_resolve_max_output_tokens_prefers_the_user_override() -> None:
    """A saved Max output tokens setting outranks the family default."""
    assert resolve_max_output_tokens("gpt-5", None) == 32768
    assert resolve_max_output_tokens("gpt-5", 5000) == 5000
    assert resolve_max_output_tokens("deepseek-v4-flash", 123456) == 123456


def test_the_agents_carry_the_budget_when_one_is_given() -> None:
    """The value has to reach ``Agent(model_settings=...)``, not just the route."""
    from swarm_builder.compile import clarify as clarify_module
    from swarm_builder.compile import generate as generate_module

    draft_agent = generate_module.build_generate_agent("test:model", max_output_tokens=16384)
    clarify_agent = clarify_module.build_clarify_agent("test:model", max_output_tokens=16384)
    assert draft_agent.model_settings["max_tokens"] == 16384
    assert clarify_agent.model_settings["max_tokens"] == 16384

    # And an unset budget leaves the provider default alone.
    default_draft = generate_module.build_generate_agent("test:model")
    default_clarify = clarify_module.build_clarify_agent("test:model")
    # `None` here means "no explicit settings", i.e. the provider default stands.
    assert (default_draft.model_settings or {}).get("max_tokens") is None
    assert (default_clarify.model_settings or {}).get("max_tokens") is None
