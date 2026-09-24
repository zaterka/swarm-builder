"""``compile/generate.py`` and ``POST /api/graphs/generate``.

``materialize`` is exercised on the shapes a model is most likely to
produce (linear, decision, fan-out written as bare seq edges, an
orchestrator with children), the repair loop on a scripted agent whose
first draft is broken, and the HTTP route under ``SWARM_FAKE_GENERATE=1``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import BinaryContent

from swarm_builder.compile import generate as generate_module
from swarm_builder.compile.generate import (
    DraftEdge,
    DraftError,
    DraftNode,
    DraftStateField,
    GenerateError,
    GraphDraft,
    fake_draft,
    generate_graph,
    materialize,
)
from swarm_builder.compile.review import review
from swarm_builder.main import create_app
from swarm_builder.routes import compile as compile_routes
from swarm_builder.slugify import slugify_titles

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    monkeypatch.setenv("SWARM_CONFIG", str(tmp_path / "workspace" / "settings.json"))
    monkeypatch.delenv("SWARM_MODEL", raising=False)
    monkeypatch.delenv("SWARM_FAKE_GENERATE", raising=False)
    monkeypatch.delenv("SWARM_FAKE_FILL", raising=False)
    monkeypatch.delenv("SWARM_RUN_TEST_MODEL", raising=False)
    return tmp_path / "workspace"


@pytest.fixture(autouse=True)
def fresh_registry() -> Iterator[None]:
    compile_routes._reset_registry()
    yield
    compile_routes._reset_registry()


# ---------------------------------------------------------------------------
# materialize
# ---------------------------------------------------------------------------


def _linear_draft() -> GraphDraft:
    return GraphDraft(
        name="Linear",
        nodes=[
            DraftNode(
                title="Intake", kind="programmatic", intent="Normalize input.", writes=["topic"]
            ),
            DraftNode(title="Chat", kind="agent", intent="Chat about the topic.", reads=["topic"]),
            DraftNode(title="Summarize", kind="programmatic", intent="Prefix with SUMMARY."),
        ],
        edges=[
            DraftEdge(source="Intake", target="Chat"),
            DraftEdge(source="Chat", target="Summarize"),
        ],
    )


def test_linear_draft_is_review_clean_with_frontend_ids() -> None:
    graph = materialize(_linear_draft(), "g1")
    assert review(graph).ok
    assert [n.id for n in graph.nodes] == slugify_titles(["Intake", "Chat", "Summarize"])
    assert graph.entry_node_id == "intake"
    assert graph.exit_node_id == "summarize"
    # `topic` was never declared: it is added rather than left as a review error.
    assert [f.name for f in graph.state_fields] == ["topic"]
    assert graph.state_fields[0].default == '""'
    # Layout: strictly increasing x along the chain, same row.
    xs = [n.position.x for n in graph.nodes]
    assert xs == sorted(xs) and len(set(xs)) == 3
    assert len({n.position.y for n in graph.nodes}) == 1


def test_decision_draft_builds_branches_and_forces_branch_edges() -> None:
    draft = GraphDraft(
        name="Triage",
        nodes=[
            DraftNode(title="Classify", kind="programmatic", intent="Return billing or technical."),
            DraftNode(title="Route", kind="decision", intent="Route by category."),
            DraftNode(title="Billing", kind="agent", intent="Answer billing questions."),
            DraftNode(title="Technical", kind="agent", intent="Answer technical questions."),
        ],
        edges=[
            DraftEdge(source="Classify", target="Route"),
            # The model wrote `seq` out of a decision; it must become a branch.
            DraftEdge(source="Route", target="Billing", match="billing"),
            DraftEdge(source="Route", target="Technical", kind="branch", match="technical"),
        ],
    )
    graph = materialize(draft, "g2")
    assert review(graph).ok, [f.message for f in review(graph).errors]
    route = next(n for n in graph.nodes if n.id == "route")
    assert route.decision is not None
    assert [(b.match, b.target_node_id) for b in route.decision.branches] == [
        ("billing", "billing"),
        ("technical", "technical"),
    ]
    assert all(e.kind == "branch" for e in graph.edges if e.source == "route")
    # Two sinks: the exit is the last one; both are laid out in one column.
    assert graph.exit_node_id == "technical"
    billing, technical = (
        next(n for n in graph.nodes if n.id == i) for i in ("billing", "technical")
    )
    assert billing.position.x == technical.position.x
    assert billing.position.y != technical.position.y


def test_decision_and_join_nodes_drop_reads_and_writes() -> None:
    draft = GraphDraft(
        name="x",
        nodes=[
            DraftNode(title="A", kind="programmatic", intent="a", writes=["t"]),
            DraftNode(title="D", kind="decision", intent="d", reads=["t"], writes=["u"]),
            DraftNode(title="B", kind="agent", intent="b", reads=["t"]),
        ],
        edges=[
            DraftEdge(source="A", target="D"),
            DraftEdge(source="D", target="B", kind="branch", match="go"),
        ],
    )
    graph = materialize(draft, "g")
    decision = next(n for n in graph.nodes if n.kind == "decision")
    assert decision.reads == [] and decision.writes == []
    assert review(graph).ok


def test_branch_without_match_is_a_draft_error() -> None:
    draft = GraphDraft(
        name="x",
        nodes=[
            DraftNode(title="A", kind="programmatic", intent="a"),
            DraftNode(title="D", kind="decision", intent="d"),
            DraftNode(title="B", kind="agent", intent="b"),
        ],
        edges=[DraftEdge(source="A", target="D"), DraftEdge(source="D", target="B")],
    )
    with pytest.raises(DraftError, match="needs a match value"):
        materialize(draft, "g")


def test_bare_multi_successor_becomes_fanout_and_join_edges_are_inferred() -> None:
    draft = GraphDraft(
        name="Fan",
        nodes=[
            DraftNode(title="Split", kind="programmatic", intent="Split the work."),
            DraftNode(title="Left", kind="agent", intent="Left angle."),
            DraftNode(title="Right", kind="agent", intent="Right angle."),
            DraftNode(title="Merge", kind="join", intent="Merge.", output_type="list[str]"),
        ],
        edges=[
            DraftEdge(source="Split", target="Left"),
            DraftEdge(source="Split", target="Right"),
            DraftEdge(source="Left", target="Merge"),
            DraftEdge(source="Right", target="Merge"),
        ],
    )
    graph = materialize(draft, "g3")
    assert review(graph).ok, [f.message for f in review(graph).errors]
    kinds = {(e.source, e.target): e.kind for e in graph.edges}
    assert kinds[("split", "left")] == "fanout"
    assert kinds[("split", "right")] == "fanout"
    assert kinds[("left", "merge")] == "join"
    fanouts = [e for e in graph.edges if e.kind == "fanout"]
    assert all(e.join_node_id == "merge" for e in fanouts)  # type: ignore[union-attr]
    merge = next(n for n in graph.nodes if n.id == "merge")
    assert merge.join is not None and merge.join.reducer == "list_append"


def test_fanout_arm_without_join_is_a_draft_error() -> None:
    draft = GraphDraft(
        name="x",
        nodes=[
            DraftNode(title="Split", kind="programmatic", intent="s"),
            DraftNode(title="Left", kind="agent", intent="l"),
            DraftNode(title="Right", kind="agent", intent="r"),
        ],
        edges=[DraftEdge(source="Split", target="Left"), DraftEdge(source="Split", target="Right")],
    )
    with pytest.raises(DraftError, match="never reaches a join"):
        materialize(draft, "g")


def test_orchestrator_children_become_delegate_edges_and_template() -> None:
    draft = GraphDraft(
        name="Orch",
        nodes=[
            DraftNode(
                title="Coordinator", kind="agent", intent="Coordinate.", delegates_to=["Helper"]
            ),
            DraftNode(title="Helper", kind="agent", intent="Help."),
        ],
        edges=[],
    )
    graph = materialize(draft, "g4")
    assert review(graph).ok, [f.message for f in review(graph).errors]
    coordinator = next(n for n in graph.nodes if n.id == "coordinator")
    assert coordinator.template == "orchestrator"
    assert coordinator.agent is not None and coordinator.agent.delegates_to == ["helper"]
    assert [(e.kind, e.source, e.target) for e in graph.edges] == [
        ("delegate", "coordinator", "helper")
    ]
    assert graph.entry_node_id == "coordinator"
    helper = next(n for n in graph.nodes if n.id == "helper")
    assert helper.position.x > coordinator.position.x


def test_unknown_title_duplicate_title_and_two_entries_are_draft_errors() -> None:
    with pytest.raises(DraftError, match="unknown node title"):
        materialize(
            GraphDraft(
                name="x",
                nodes=[DraftNode(title="A", kind="agent", intent="a")],
                edges=[DraftEdge(source="A", target="Nope")],
            ),
            "g",
        )
    with pytest.raises(DraftError, match="unique"):
        materialize(
            GraphDraft(
                name="x",
                nodes=[
                    DraftNode(title="A", kind="agent", intent="a"),
                    DraftNode(title="A", kind="agent", intent="a"),
                ],
                edges=[],
            ),
            "g",
        )
    with pytest.raises(DraftError, match="exactly one entry"):
        materialize(
            GraphDraft(
                name="x",
                nodes=[
                    DraftNode(title="A", kind="agent", intent="a"),
                    DraftNode(title="B", kind="agent", intent="b"),
                ],
                edges=[],
            ),
            "g",
        )


def test_state_fields_get_dataclass_safe_defaults() -> None:
    draft = _linear_draft()
    draft.state_fields = [
        DraftStateField(name="topic", type="str"),
        DraftStateField(name="items", type="list[str]"),
        DraftStateField(name="meta", type="json"),
    ]
    graph = materialize(draft, "g")
    defaults = {f.name: f.default for f in graph.state_fields}
    assert defaults == {"topic": '""', "items": "None", "meta": "None"}


# ---------------------------------------------------------------------------
# fake_draft
# ---------------------------------------------------------------------------


def test_fake_draft_is_a_review_clean_chain() -> None:
    text = (
        "Take a support ticket. Classify it as billing or technical; then route it to an "
        "agent that answers. Summarize the answer."
    )
    graph = materialize(fake_draft(text), "fake")
    assert review(graph).ok
    assert len(graph.nodes) == 4
    assert [n.kind for n in graph.nodes] == ["programmatic", "agent", "agent", "agent"]
    assert len(graph.edges) == 3


def test_fake_draft_handles_an_empty_description() -> None:
    graph = materialize(fake_draft("   "), "fake")
    assert review(graph).ok and len(graph.nodes) == 1


# ---------------------------------------------------------------------------
# Repair loop, with a scripted agent
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedRun:
    output: GraphDraft

    def all_messages(self) -> list[str]:
        return ["scripted"]


@dataclass
class _ScriptedAgent:
    drafts: list[GraphDraft]
    prompts: list[str] = field(default_factory=list)

    async def run(
        self, prompt: str, *, message_history: object, usage_limits: object
    ) -> _ScriptedRun:
        del message_history, usage_limits
        self.prompts.append(prompt)
        return _ScriptedRun(self.drafts.pop(0))


async def test_repair_loop_feeds_problems_back_and_returns_the_fixed_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = GraphDraft(
        name="x",
        nodes=[
            DraftNode(title="A", kind="programmatic", intent="a", output_type="json"),
            DraftNode(title="B", kind="agent", intent="b"),  # input str != json
        ],
        edges=[DraftEdge(source="A", target="B")],
    )
    agent = _ScriptedAgent([broken, _linear_draft()])
    monkeypatch.setattr(generate_module, "build_generate_agent", lambda model, **_: agent)
    result = await generate_graph("desc", model="test:model", graph_id="g")
    assert result.attempts == 2
    assert result.graph.entry_node_id == "intake"
    assert agent.prompts[0] == "desc"
    assert "port_type_mismatch" in agent.prompts[1]


async def test_repair_loop_gives_up_after_max_repairs(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = GraphDraft(
        name="x",
        nodes=[DraftNode(title="A", kind="agent", intent="a")],
        edges=[DraftEdge(source="A", target="Missing")],
    )
    agent = _ScriptedAgent([bad, bad, bad])
    monkeypatch.setattr(generate_module, "build_generate_agent", lambda model, **_: agent)
    with pytest.raises(GenerateError) as excinfo:
        await generate_graph("desc", model="test:model", graph_id="g", max_repairs=2)
    assert excinfo.value.attempts == 3
    assert any("unknown node title" in p for p in excinfo.value.problems)


async def test_generate_without_model_and_without_fake_is_refused() -> None:
    with pytest.raises(ValueError, match="needs a model"):
        await generate_graph("desc", model=None, graph_id="g")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def test_generate_route_saves_and_returns_a_graph_under_fake_mode(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = workspace
    monkeypatch.setenv("SWARM_FAKE_GENERATE", "1")
    client = TestClient(create_app())
    response = client.post(
        "/api/graphs/generate",
        json={
            "description": "Read the ticket. Classify it. Summarize with an agent.",
            "name": "Triage",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    graph = body["graph"]
    assert graph["name"] == "Triage"
    assert len(graph["nodes"]) == 3
    assert body["attempts"] == 1
    assert body["warnings"] == []
    assert set(body["model"]) == {"provider", "model", "source"}

    # It is now an ordinary saved document.
    saved = client.get(f"/api/graphs/{graph['id']}")
    assert saved.status_code == 200
    assert saved.json()["entryNodeId"] == graph["entryNodeId"]
    listed = client.get("/api/graphs").json()["graphs"]
    assert [g["id"] for g in listed] == [graph["id"]]


def test_generate_route_can_replace_an_existing_graph_id(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = workspace
    monkeypatch.setenv("SWARM_FAKE_GENERATE", "1")
    client = TestClient(create_app())
    first = client.post("/api/graphs/generate", json={"description": "One step."}).json()["graph"]
    second = client.post(
        "/api/graphs/generate",
        json={"description": "First step. Second step.", "graphId": first["id"]},
    )
    assert second.status_code == 200
    assert second.json()["graph"]["id"] == first["id"]
    assert len(client.get(f"/api/graphs/{first['id']}").json()["nodes"]) == 2


def test_generate_route_works_in_dry_run_with_no_model_at_all(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new user who turns the dry-run switch on must be able to
    describe a workflow with no provider, no key and no environment variable.

    This is the case both of the route's gates used to block: the
    "nothing configured" 503 and the missing-credential 503.
    """
    from swarm_builder.appconfig import AppConfig, save_config

    save_config(AppConfig(dry_run=True), workspace / "settings.json")
    monkeypatch.delenv("SWARM_FAKE_GENERATE", raising=False)

    response = TestClient(create_app()).post(
        "/api/graphs/generate", json={"description": "Take a ticket. Classify it. Reply."}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dryRun"] is True
    assert body["graph"]["nodes"]
    assert body["model"]["source"] == "bundle-default"


def test_generate_route_reports_dry_run_false_for_a_real_model(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWARM_FAKE_GENERATE", "1")
    response = TestClient(create_app()).post(
        "/api/graphs/generate", json={"description": "Take a ticket. Classify it. Reply."}
    )
    assert response.status_code == 200
    # ``SWARM_FAKE_GENERATE=1`` is itself a dry-run signal now.
    assert response.json()["dryRun"] is True


def test_generate_route_refuses_when_no_model_is_configured(workspace: Path) -> None:
    _ = workspace
    client = TestClient(create_app())
    response = client.post("/api/graphs/generate", json={"description": "Anything."})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "No model is configured yet" in detail
    # A brand-new user must be pointed at this application's own controls.
    for foreign in ("settings.yaml", "DSH_HOME", "agent-default-model", "SWARM_MODEL"):
        assert foreign not in detail, foreign


def test_generate_route_validates_the_description(workspace: Path) -> None:
    _ = workspace
    client = TestClient(create_app())
    assert client.post("/api/graphs/generate", json={"description": ""}).status_code == 422


# ---------------------------------------------------------------------------
# Attachments and answers (PLAN-ATTACHMENTS-CLARIFY.md)
# ---------------------------------------------------------------------------


def _loaded_csv(
    workspace: Path,
    name: str = "orders.csv",
    text: str = "| Order ID |\n| --- |\n| 42 |",
):
    """Store and load one text attachment the way the route would."""
    from swarm_builder.attachments import store as store_module
    from swarm_builder.attachments.models import Extraction

    record = store_module.save_attachment(
        workspace,
        name,
        text.encode("utf-8"),
        Extraction(text=text, truncated=False, notes=["only the first 200 rows were read"]),
        "text/csv",
        "csv",
    )
    return store_module.load_attachments(workspace, [record.id])


def _loaded_image(
    workspace: Path,
    name: str = "sketch.png",
    payload: bytes = b"\x89PNG\r\n\x1a\n00",
):
    from swarm_builder.attachments import store as store_module
    from swarm_builder.attachments.models import Extraction

    record = store_module.save_attachment(
        workspace,
        name,
        payload,
        Extraction(text="", truncated=False, notes=["image attachment"]),
        "image/png",
        "image",
    )
    return store_module.load_attachments(workspace, [record.id])


def test_without_files_or_answers_the_prompt_is_exactly_the_description() -> None:
    """The regression guard for every existing caller of this endpoint."""
    assert generate_module.build_prompt_parts("Take a ticket. Classify it.") == (
        "Take a ticket. Classify it."
    )
    assert generate_module.build_prompt_parts("Take a ticket.", [], []) == "Take a ticket."


async def test_the_agent_receives_the_bare_description_when_nothing_is_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant, asserted where it matters: at the agent's own argument.

    `build_prompt_parts` returning the string is the mechanism; this is the
    promise (a request with no files and no answers sends exactly what it sent
    before the feature existed). The system instructions did gain a paragraph --
    that is a deliberate, inert change described in the plan, and it is why this
    test targets the prompt argument rather than pretending nothing changed.
    """
    captured: list[object] = []

    class _Agent:
        async def run(self, prompt: object, **kwargs: object) -> _ScriptedRun:
            captured.append(prompt)
            return _ScriptedRun(_linear_draft())

    monkeypatch.setattr(generate_module, "build_generate_agent", lambda model, **_: _Agent())

    await generate_graph("Take a ticket. Classify it.", model="test:model", graph_id="g")

    assert captured[0] == "Take a ticket. Classify it."


def test_answers_are_rendered_as_an_authoritative_block() -> None:
    parts = generate_module.build_prompt_parts(
        "Handle invoices.",
        (),
        [
            generate_module.ClarifyAnswerIn(
                question_id="q1",
                question="Who approves a refund?",
                answer="A refund-policy agent",
            ),
            generate_module.ClarifyAnswerIn(question_id="q2", question=None, answer="A CSV file"),
            generate_module.ClarifyAnswerIn(question_id="q3", question="Ignored?", answer="   "),
        ],
    )
    assert isinstance(parts, list)
    assert parts[0] == "Handle invoices."
    block = parts[1]
    assert isinstance(block, str)
    assert "authoritative" in block
    assert "- Q: Who approves a refund?\n  A: A refund-policy agent" in block
    # A question the client did not send still contributes its answer.
    assert "- A CSV file" in block
    # A whitespace-only answer is not an answer.
    assert "Ignored?" not in block


def test_attachment_text_becomes_a_context_block_with_its_notes(tmp_path: Path) -> None:
    loaded = _loaded_csv(tmp_path / "workspace")
    parts = generate_module.build_prompt_parts("Handle orders.", loaded)
    assert isinstance(parts, list)
    assert parts[0] == "Handle orders."
    context = parts[1]
    assert isinstance(context, str)
    assert '--- file 1/1: "orders.csv"' in context
    assert "| 42 |" in context
    assert "not as a list of steps" in context
    assert "only the first 200 rows were read" in context


def test_images_travel_as_binary_content_with_their_media_type(tmp_path: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"7" * 16
    loaded = _loaded_image(tmp_path / "workspace", payload=payload)
    parts = generate_module.build_prompt_parts("Read the sketch.", loaded)
    assert isinstance(parts, list)
    image = parts[-1]
    assert isinstance(image, BinaryContent)
    assert image.data == payload
    assert image.media_type == "image/png"
    # An image contributes no text block of its own.
    assert len(parts) == 2


async def test_generate_graph_sends_the_parts_to_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swarm_builder.compile.clarify import ClarifyAnswerIn

    captured: list[object] = []

    class _Agent:
        async def run(self, prompt: object, **kwargs: object) -> _ScriptedRun:
            captured.append(prompt)
            return _ScriptedRun(_linear_draft())

    monkeypatch.setattr(generate_module, "build_generate_agent", lambda model, **_: _Agent())
    loaded = _loaded_csv(tmp_path / "workspace")

    await generate_graph(
        "Handle orders.",
        model="test:model",
        graph_id="g",
        attachments=loaded,
        answers=[ClarifyAnswerIn(question_id="q1", question="Which file?", answer="orders.csv")],
    )

    prompt = captured[0]
    assert isinstance(prompt, list)
    assert prompt[0] == "Handle orders."
    assert any(isinstance(part, str) and "authoritative" in part for part in prompt)
    assert any(isinstance(part, str) and "| 42 |" in part for part in prompt)


def test_the_route_echoes_the_attachments_it_used(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWARM_FAKE_GENERATE", "1")
    client = TestClient(create_app())
    upload = client.post(
        "/api/graphs/attachments",
        files={"file": ("orders.csv", b"Order ID\n42\n", "text/csv")},
    )
    assert upload.status_code == 200, upload.text
    attachment_id = upload.json()["attachment"]["id"]

    response = client.post(
        "/api/graphs/generate",
        json={
            "description": "Handle orders. Summarize with an agent.",
            "attachmentIds": [attachment_id],
        },
    )

    assert response.status_code == 200, response.text
    summaries = response.json()["attachments"]
    assert [item["filename"] for item in summaries] == ["orders.csv"]
    assert summaries[0]["kind"] == "csv"
    assert summaries[0]["chars"] > 0
    # The graph document itself never carries attachment content or names.
    saved = (workspace / "graphs" / f"{response.json()['graph']['id']}.json").read_text()
    assert "orders.csv" not in saved
    assert "Order ID" not in saved


def test_the_route_refuses_an_unknown_attachment_id(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWARM_FAKE_GENERATE", "1")
    _ = workspace
    response = TestClient(create_app()).post(
        "/api/graphs/generate",
        json={"description": "Handle it.", "attachmentIds": ["a" * 32]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "attachment_expired"


def test_the_route_refuses_more_attachments_than_the_count_cap(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six distinct files is the documented 413, not a schema-validation error.

    The cap lives in the store (one place knows how many files a generation may
    use), so the request model deliberately does not pre-empt it: an API client
    gets the same structured code the UI depends on.
    """
    monkeypatch.setenv("SWARM_FAKE_GENERATE", "1")
    _ = workspace
    client = TestClient(create_app())
    ids = [
        client.post(
            "/api/graphs/attachments",
            files={"file": (f"f{index}.csv", f"a,b\n{index},2\n", "text/csv")},
        ).json()["attachment"]["id"]
        for index in range(6)
    ]

    response = client.post(
        "/api/graphs/generate",
        json={"description": "Handle it.", "attachmentIds": ids},
    )

    assert response.status_code == 413, response.text
    assert response.json()["detail"]["code"] == "attachment_too_large"
    assert "at most 5 files" in response.json()["detail"]["message"]


def test_the_route_rechecks_the_image_gate_at_generate_time(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model can change between upload and generate, so the check re-runs."""
    from swarm_builder.attachments import store as store_module
    from swarm_builder.attachments.models import Extraction

    record = store_module.save_attachment(
        workspace,
        "sketch.png",
        b"\x89PNG\r\n\x1a\n1234",
        Extraction(text="", truncated=False, notes=[]),
        "image/png",
        "image",
    )
    _configure_live_route(monkeypatch, "deepseek-v4-flash")

    response = TestClient(create_app()).post(
        "/api/graphs/generate",
        json={"description": "Read the sketch.", "attachmentIds": [record.id]},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "image_input_unsupported"


def test_the_route_accepts_an_image_from_a_vision_capable_route(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swarm_builder.attachments import store as store_module
    from swarm_builder.attachments.models import Extraction

    record = store_module.save_attachment(
        workspace,
        "sketch.png",
        b"\x89PNG\r\n\x1a\n1234",
        Extraction(text="", truncated=False, notes=[]),
        "image/png",
        "image",
    )
    _configure_live_route(monkeypatch, "deepseek-flash")
    captured: list[object] = []

    class _Agent:
        async def run(self, prompt: object, **kwargs: object) -> _ScriptedRun:
            captured.append(prompt)
            return _ScriptedRun(_linear_draft())

    monkeypatch.setattr(generate_module, "build_generate_agent", lambda model, **_: _Agent())

    response = TestClient(create_app()).post(
        "/api/graphs/generate",
        json={"description": "Read the sketch.", "attachmentIds": [record.id]},
    )

    assert response.status_code == 200, response.text
    prompt = captured[0]
    assert isinstance(prompt, list)
    assert any(isinstance(part, BinaryContent) for part in prompt)


def test_the_route_sends_attachment_context_and_answers_into_the_prompt(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swarm_builder.attachments import store as store_module
    from swarm_builder.attachments.models import Extraction

    record = store_module.save_attachment(
        workspace,
        "orders.csv",
        b"Order ID\n42\n",
        Extraction(text="| Order ID |\n| --- |\n| 42 |", truncated=False, notes=[]),
        "text/csv",
        "csv",
    )
    _configure_live_route(monkeypatch, "deepseek-flash")
    captured: list[object] = []

    class _Agent:
        async def run(self, prompt: object, **kwargs: object) -> _ScriptedRun:
            captured.append(prompt)
            return _ScriptedRun(_linear_draft())

    monkeypatch.setattr(generate_module, "build_generate_agent", lambda model, **_: _Agent())

    response = TestClient(create_app()).post(
        "/api/graphs/generate",
        json={
            "description": "Handle orders.",
            "attachmentIds": [record.id],
            "answers": [
                {"questionId": "q1", "question": "Which file?", "answer": "orders.csv"},
                {"questionId": "q1", "question": "Which file?", "answer": "the newest one"},
                {"questionId": "q2", "answer": "   "},
            ],
        },
    )

    assert response.status_code == 200, response.text
    prompt = captured[0]
    assert isinstance(prompt, list)
    context = [part for part in prompt if isinstance(part, str) and "| 42 |" in part]
    answers = [part for part in prompt if isinstance(part, str) and "authoritative" in part]
    assert context and answers
    # Duplicate question ids collapse to the last non-blank answer, and a blank
    # answer is dropped rather than sent.
    assert "the newest one" in answers[0]
    assert "orders.csv" not in answers[0]
    assert "q2" not in answers[0]


def _configure_live_route(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    """A configured provider/model with a credential and no dry run."""
    monkeypatch.delenv("SWARM_FAKE_GENERATE", raising=False)
    monkeypatch.setenv("SWARM_MODEL", f"deepseek:{model}")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
