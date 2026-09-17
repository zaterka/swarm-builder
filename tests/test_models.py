"""Unit tests for the graph-document schema (PLAN.md "Tests" -> Unit ->
models.py, and the JSON round-trip acceptance criterion)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

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
    Position,
    ProgrammaticSpec,
    StateField,
    SwarmGraph,
    SwarmNode,
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
        }

    def test_port_type_import_table_matches_annotation_table(self) -> None:
        assert set(PORT_TYPE_IMPORTS) == set(PORT_TYPE_ANNOTATIONS)
        assert PORT_TYPE_IMPORTS["json"] == "from typing import Any"
        assert PORT_TYPE_IMPORTS["str"] is None
        assert PORT_TYPE_IMPORTS["list[str]"] is None
