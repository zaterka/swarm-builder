"""Generate a whole graph document from a prose description.

Feature 2 of ``PLAN-V2-FEATURES.md``. The model's job is kept deliberately
small: it emits a :class:`GraphDraft` -- nodes named by title, edges that
reference titles, no ids, no positions, no ``DecisionSpec.branches``, no
``joinNodeId``. Everything the document's coupled invariants depend on is
then *derived* deterministically by :func:`materialize`:

* ids via :func:`~swarm_builder.slugify.slugify_titles`, the same function
  the frontend's ``slug.ts`` mirrors, so a generated node gets exactly the
  id the canvas would have assigned to a hand-typed title;
* ``DecisionSpec.branches`` from the ``branch`` edges leaving each decision;
* ``FanoutEdge.join_node_id`` by following each arm forward to the first
  ``join`` node;
* a plain multi-successor node is promoted to a fan-out, and any edge
  into a ``join`` node becomes a ``join`` edge, because those are the two
  shapes a model most often writes as bare ``seq`` edges;
* ``entryNodeId``/``exitNodeId`` from in-/out-degree;
* a state field that a node reads or writes but the draft forgot to declare
  is added (as a ``str``), rather than surfacing as a review error;
* positions from a layered left-to-right layout (:func:`layout_positions`).

The materialized document then goes through Phase 1's own
:func:`~swarm_builder.compile.review.review`. Any error -- or a draft
:func:`materialize` cannot make sense of -- is fed back to the model as
text and the draft is regenerated, at most :data:`MAX_REPAIRS` times. This
is the same "the model proposes, deterministic code checks and reports"
loop the fill agent runs with ``parse_check``.

``SWARM_FAKE_GENERATE=1`` swaps the model for :func:`fake_draft`, a
deterministic sentence-per-step draft, so the endpoint and the picker flow
are testable with no credentials -- the mirror of ``SWARM_FAKE_FILL``.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_ai import Agent, PromptedOutput, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model

from swarm_builder.compile.review import Finding, review
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
    NodeKind,
    PortType,
    Position,
    ProgrammaticSpec,
    ReducerId,
    SeqEdge,
    StateField,
    SwarmEdge,
    SwarmGraph,
    SwarmNode,
    TemplateId,
)
from swarm_builder.slugify import slugify_titles
from swarm_builder.templates.registry import infer_template

#: How many times a draft that fails materialization or review is sent back
#: to the model with the findings before giving up.
MAX_REPAIRS = 2

#: Model requests one generate call may spend, across the initial draft
#: and every repair. A draft is one request; pydantic-ai's own output
#: validation retries count too.
REQUEST_LIMIT = 12

#: ``Agent(retries=...)``: output-schema validation retries per request.
AGENT_RETRIES = 3

#: Env var and value that select :func:`fake_draft` instead of a model.
FAKE_GENERATE_ENV_VAR = "SWARM_FAKE_GENERATE"
FAKE_GENERATE_ENABLED_VALUE = "1"

#: Layout grid: rank (BFS depth from the entry) along x, order within a rank
#: along y. Matches the canvas node card size with room for edge labels.
LAYOUT_ORIGIN_X = 60.0
LAYOUT_ORIGIN_Y = 60.0
LAYOUT_COLUMN_WIDTH = 300.0
LAYOUT_ROW_HEIGHT = 170.0

#: Default literal source for a state field the draft did not give one.
#: Every field needs a default: the dry run and the tracer construct
#: ``State()`` with no arguments.
STATE_FIELD_DEFAULTS: dict[PortType, str] = {
    "str": '""',
    "list[str]": "None",
    "json": "None",
}

DraftEdgeKind = Literal["seq", "branch", "fanout", "join", "delegate"]


# ---------------------------------------------------------------------------
# The draft: what the model emits
# ---------------------------------------------------------------------------


class _DraftModel(BaseModel):
    """camelCase aliases like the document; extras forbidden so a stray
    key becomes a schema-validation retry rather than silently dropped."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class DraftNode(_DraftModel):
    """One node, referenced elsewhere in the draft by its ``title``."""

    title: str = Field(description="Short unique human title; the node id is derived from it.")
    kind: NodeKind
    intent: str = Field(description="Plain-English description of what this node does.")
    input_type: PortType = "str"
    output_type: PortType = "str"
    template: TemplateId | None = Field(
        default=None, description="Agent nodes only: chat, websearch, or orchestrator."
    )
    instructions: str | None = Field(
        default=None, description="Agent nodes only: the system instructions. Defaults to intent."
    )
    needs: list[str] = Field(
        default_factory=list, description="Programmatic nodes only: extra pip packages."
    )
    signature_hint: str | None = Field(
        default=None, description="Programmatic nodes only: guidance for the code writer."
    )
    reads: list[str] = Field(default_factory=list, description="State field names this node reads.")
    writes: list[str] = Field(
        default_factory=list, description="State field names this node writes."
    )
    reducer: ReducerId | None = Field(default=None, description="Join nodes only.")
    delegates_to: list[str] = Field(
        default_factory=list,
        description="Orchestrator agents only: titles of child agent nodes called as tools.",
    )


class DraftEdge(_DraftModel):
    """One edge between two titles."""

    source: str
    target: str
    kind: DraftEdgeKind = "seq"
    match: str | None = Field(
        default=None,
        description="branch edges only: the value the decision's source step returns for this arm.",
    )


class DraftStateField(_DraftModel):
    name: str
    type: PortType = "str"
    description: str | None = None


class GraphDraft(_DraftModel):
    """The model's whole answer."""

    name: str = Field(description="A short workflow name.")
    nodes: list[DraftNode]
    edges: list[DraftEdge]
    state_fields: list[DraftStateField] = Field(default_factory=list)


class DraftError(ValueError):
    """The draft cannot be turned into a document; the message is what
    gets fed back to the model."""


class GenerateError(RuntimeError):
    """Every attempt failed. ``problems`` is the last round's feedback."""

    def __init__(self, problems: Sequence[str], attempts: int) -> None:
        self.problems = list(problems)
        self.attempts = attempts
        super().__init__(
            f"could not produce a review-clean graph after {attempts} attempt(s): "
            + "; ".join(self.problems)
        )


@dataclass(frozen=True)
class GenerateResult:
    graph: SwarmGraph
    warnings: list[Finding]
    attempts: int


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------

_DISPATCH_KINDS: frozenset[str] = frozenset({"seq", "branch", "fanout", "join"})
_STEP_KINDS: frozenset[str] = frozenset({"agent", "programmatic"})


def _title_index(draft: GraphDraft) -> dict[str, int]:
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for index, node in enumerate(draft.nodes):
        key = node.title.strip()
        if not key:
            raise DraftError(f"node {index + 1} has an empty title")
        if key in seen:
            duplicates.append(key)
        seen[key] = index
    if duplicates:
        raise DraftError(
            "node titles must be unique; duplicated: " + ", ".join(sorted(set(duplicates)))
        )
    return seen


def _resolve_ref(title: str, index: dict[str, int], what: str) -> int:
    key = title.strip()
    if key in index:
        return index[key]
    lowered = {k.lower(): v for k, v in index.items()}
    if key.lower() in lowered:
        return lowered[key.lower()]
    raise DraftError(f"{what} refers to an unknown node title {title!r}")


def _normalize_edges(
    draft: GraphDraft, index: dict[str, int], kinds: list[NodeKind]
) -> list[tuple[int, int, DraftEdgeKind, str | None]]:
    """Resolve titles to indices and repair the two common shapes a model
    writes as bare ``seq``: several successors (a fan-out) and an edge into
    a join (a join edge). Delegations named on the orchestrator become
    ``delegate`` edges when the draft did not draw them."""
    edges: list[tuple[int, int, DraftEdgeKind, str | None]] = []
    for position, edge in enumerate(draft.edges):
        source = _resolve_ref(edge.source, index, f"edge {position + 1} source")
        target = _resolve_ref(edge.target, index, f"edge {position + 1} target")
        if source == target:
            raise DraftError(f"edge {position + 1} connects {edge.source!r} to itself")
        edges.append((source, target, edge.kind, edge.match))

    for source_index, node in enumerate(draft.nodes):
        for child_title in node.delegates_to:
            child = _resolve_ref(child_title, index, f"{node.title!r} delegatesTo")
            if not any(
                s == source_index and t == child and k == "delegate" for s, t, k, _ in edges
            ):
                edges.append((source_index, child, "delegate", None))

    # Edge into a join node -> join edge; out of a decision -> branch.
    repaired: list[tuple[int, int, DraftEdgeKind, str | None]] = []
    for source, target, kind, match in edges:
        if kind == "delegate":
            repaired.append((source, target, kind, match))
            continue
        if kinds[target] == "join" and kind != "fanout":
            kind = "join"
        if kinds[source] == "decision":
            kind = "branch"
        repaired.append((source, target, kind, match))

    # A non-decision node with 2+ seq/fanout successors is a fan-out.
    out_by_source: dict[int, list[int]] = defaultdict(list)
    for position, (source, _target, kind, _match) in enumerate(repaired):
        if kind in ("seq", "fanout"):
            out_by_source[source].append(position)
    for source, positions in out_by_source.items():
        if len(positions) >= 2 and kinds[source] != "decision":
            for position in positions:
                s, t, _k, m = repaired[position]
                repaired[position] = (s, t, "fanout", m)
    return repaired


def _find_join_ahead(
    start: int,
    successors: dict[int, list[int]],
    kinds: list[NodeKind],
) -> int | None:
    """First ``join`` reachable from ``start`` (inclusive) along dispatch edges."""
    queue: deque[int] = deque([start])
    visited: set[int] = set()
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        if kinds[current] == "join":
            return current
        queue.extend(successors.get(current, []))
    return None


def materialize(draft: GraphDraft, graph_id: str) -> SwarmGraph:
    """Turn a draft into a complete, positioned :class:`SwarmGraph`.

    Raises:
        DraftError: If titles are not unique, an edge names an unknown
            title, a branch has no match value, a fan-out arm never reaches
            a join, or the draft has no single entry node. The message is
            written for the model to act on.
    """
    if not draft.nodes:
        raise DraftError("the draft has no nodes")
    index = _title_index(draft)
    titles = [node.title.strip() for node in draft.nodes]
    ids = slugify_titles(titles)
    kinds: list[NodeKind] = [node.kind for node in draft.nodes]
    edges = _normalize_edges(draft, index, kinds)

    successors: dict[int, list[int]] = defaultdict(list)
    predecessors: dict[int, list[int]] = defaultdict(list)
    delegate_targets: set[int] = set()
    for source, target, kind, _match in edges:
        if kind == "delegate":
            delegate_targets.add(target)
            continue
        successors[source].append(target)
        predecessors[target].append(source)

    # Declared + implied state fields.
    declared = {f.name: f for f in draft.state_fields}
    for node in draft.nodes:
        for name in [*node.reads, *node.writes]:
            if name and name not in declared:
                declared[name] = DraftStateField(name=name, type="str")
    state_fields = [
        StateField(
            name=f.name,
            type=f.type,
            default=STATE_FIELD_DEFAULTS[f.type],
            description=f.description,
        )
        for f in declared.values()
    ]

    # Per-kind specs.
    swarm_edges: list[SwarmEdge] = []
    branches_by_decision: dict[int, list[DecisionBranch]] = defaultdict(list)
    for position, (source, target, kind, match) in enumerate(edges):
        edge_id = f"e{position + 1}"
        if kind == "seq":
            swarm_edges.append(
                SeqEdge(kind="seq", id=edge_id, source=ids[source], target=ids[target])
            )
        elif kind == "branch":
            if not match:
                raise DraftError(
                    f"branch edge from {titles[source]!r} to {titles[target]!r} needs a match value"
                )
            swarm_edges.append(
                BranchEdge(
                    kind="branch", id=edge_id, source=ids[source], target=ids[target], match=match
                )
            )
            branches_by_decision[source].append(
                DecisionBranch(match=match, target_node_id=ids[target])
            )
        elif kind == "fanout":
            join_index = _find_join_ahead(target, successors, kinds)
            if join_index is None:
                raise DraftError(
                    f"fan-out arm {titles[source]!r} -> {titles[target]!r} never reaches a join "
                    "node; add a join node that every arm flows into"
                )
            swarm_edges.append(
                FanoutEdge(
                    kind="fanout",
                    id=edge_id,
                    source=ids[source],
                    target=ids[target],
                    join_node_id=ids[join_index],
                )
            )
        elif kind == "join":
            swarm_edges.append(
                JoinEdge(kind="join", id=edge_id, source=ids[source], target=ids[target])
            )
        else:
            swarm_edges.append(
                DelegateEdge(kind="delegate", id=edge_id, source=ids[source], target=ids[target])
            )

    # Entry: the one non-delegate node with no dispatch predecessor.
    entries = [
        i for i in range(len(draft.nodes)) if not predecessors.get(i) and i not in delegate_targets
    ]
    if len(entries) != 1:
        names = ", ".join(repr(titles[i]) for i in entries) or "none"
        raise DraftError(
            f"the workflow must have exactly one entry node (no incoming edges); found: {names}"
        )
    entry = entries[0]
    sinks = [
        i
        for i in range(len(draft.nodes))
        if not successors.get(i) and i not in delegate_targets and kinds[i] != "decision"
    ]
    exit_index = sinks[-1] if sinks else entry

    positions = layout_positions(len(draft.nodes), entry, successors, edges, delegate_targets)

    nodes: list[SwarmNode] = []
    for i, node in enumerate(draft.nodes):
        io = NodeIo(input_type=node.input_type, output_type=node.output_type)
        common = {
            "id": ids[i],
            "kind": node.kind,
            "title": titles[i],
            "intent": node.intent.strip() or titles[i],
            "position": positions[i],
            "io": io,
            # A decision routes and a join reduces; neither has a body that
            # could read or write state, so a draft's reads/writes there are
            # dropped rather than left as dead declarations.
            "reads": [name for name in node.reads if name] if node.kind in _STEP_KINDS else [],
            "writes": [name for name in node.writes if name] if node.kind in _STEP_KINDS else [],
        }
        if node.kind == "agent":
            template = node.template or infer_template(node.intent).suggestion
            delegates = [ids[_resolve_ref(t, index, "delegatesTo")] for t in node.delegates_to]
            if delegates and template != "orchestrator":
                template = "orchestrator"
            nodes.append(
                SwarmNode(
                    **common,
                    template=template,
                    agent=AgentSpec(
                        instructions=(node.instructions or node.intent).strip() or titles[i],
                        delegates_to=delegates,
                    ),
                )
            )
        elif node.kind == "programmatic":
            nodes.append(
                SwarmNode(
                    **common,
                    programmatic=ProgrammaticSpec(
                        needs=list(node.needs), signature_hint=node.signature_hint
                    ),
                )
            )
        elif node.kind == "decision":
            nodes.append(
                SwarmNode(**common, decision=DecisionSpec(branches=branches_by_decision.get(i, [])))
            )
        else:
            nodes.append(SwarmNode(**common, join=JoinSpec(reducer=node.reducer or "list_append")))

    return SwarmGraph(
        id=graph_id,
        name=draft.name.strip() or "Generated workflow",
        entry_node_id=ids[entry],
        exit_node_id=ids[exit_index],
        state_fields=state_fields,
        nodes=nodes,
        edges=swarm_edges,
        updated_at=datetime.now(UTC),
    )


def layout_positions(
    count: int,
    entry: int,
    successors: dict[int, list[int]],
    edges: Sequence[tuple[int, int, DraftEdgeKind, str | None]],
    delegate_targets: set[int],
) -> list[Position]:
    """Layered left-to-right layout.

    Rank is the longest dispatch path from the entry (so a join sits right
    of every arm), x grows with rank, y with order inside a rank. A
    delegate-only child sits one column right of its orchestrator, below
    the main flow. Nodes unreachable from the entry are appended at the
    end so review can still point at them.
    """
    rank: dict[int, int] = {entry: 0}
    order = [entry]
    queue: deque[int] = deque([entry])
    while queue:
        current = queue.popleft()
        for nxt in successors.get(current, []):
            proposed = rank[current] + 1
            if proposed > rank.get(nxt, -1):
                rank[nxt] = proposed
                if nxt not in order:
                    order.append(nxt)
                queue.append(nxt)

    for source, target, kind, _match in edges:
        if kind == "delegate" and target in delegate_targets:
            rank[target] = max(rank.get(target, 0), rank.get(source, 0) + 1)

    unplaced = [i for i in range(count) if i not in rank]
    max_rank = max(rank.values(), default=0)
    for i in unplaced:
        max_rank += 1
        rank[i] = max_rank

    rows_used: dict[int, int] = defaultdict(int)
    positions: list[Position] = [Position(x=0, y=0) for _ in range(count)]
    # Main-flow nodes first (BFS order), then delegate children, then the rest.
    placement = [i for i in order if i not in delegate_targets]
    placement += [i for i in range(count) if i in delegate_targets]
    placement += [i for i in range(count) if i not in placement]
    for i in placement:
        column = rank[i]
        row = rows_used[column]
        rows_used[column] += 1
        positions[i] = Position(
            x=LAYOUT_ORIGIN_X + column * LAYOUT_COLUMN_WIDTH,
            y=LAYOUT_ORIGIN_Y + row * LAYOUT_ROW_HEIGHT,
        )
    return positions


# ---------------------------------------------------------------------------
# The model call and repair loop
# ---------------------------------------------------------------------------

GENERATE_INSTRUCTIONS = """\
You design agent workflows for Swarm Builder. Given a prose description, return
ONE GraphDraft: the nodes, the edges between them (by node title), and the
shared state fields. Someone will review and edit it on a canvas afterwards,
so prefer a small, clear graph (2-10 nodes) over an exhaustive one.

Node kinds:
- agent: an LLM step. Give it clear `instructions`. `template` is `chat`
  (default), `websearch` (needs live web results), or `orchestrator` (calls
  other agent nodes as tools; list their titles in `delegatesTo`, and do NOT
  draw seq edges to those children).
- programmatic: plain Python with no model (parsing, formatting, calling an
  API, classifying by rule). Describe it precisely in `intent` and
  `signatureHint`; a coding model writes the body later.
- decision: routes on the *output of the step before it*. Its predecessor
  must be a step whose outputType is `str` and returns exactly one of the
  branch `match` values. Draw one `branch` edge per outcome from the decision
  to the target, each with its `match`. Data does NOT pass through a decision:
  a branch target receives the match value itself, so its inputType must be
  `str`; anything else it needs must travel through a state field written
  upstream.
- join: fan-in. Draw a `fanout` edge from the splitting node to each arm and
  a `join` edge from each arm into the join. `reducer` is list_append
  (default), list_extend, dict_update, or sum.

Rules the reviewer enforces:
- Titles are unique and short (they become Python identifiers).
- Exactly one node has no incoming edge (the entry). No cycles.
- Consecutive steps' types line up: a node's inputType equals its
  predecessor's outputType. Port types are `str`, `json` (a dict), `list[str]`.
- A node lists in `writes` only state fields it sets, and in `reads` only
  fields some earlier node writes. Declare every field in `stateFields`.
- Every node has a one-sentence `intent`.

Return only the GraphDraft.
"""

_REPAIR_PROMPT = """\
The previous draft was rejected. Problems:
{problems}

Return a corrected, complete GraphDraft that fixes every problem while keeping
the workflow the description asked for.
"""


def build_generate_agent(model: Model | str) -> Agent[None, GraphDraft]:
    """The structured-output agent, built per call (never at import).

    ``PromptedOutput`` rather than pydantic-ai's default tool-based output:
    the default forces ``tool_choice`` onto an "output tool", and reasoning
    models reject that (DeepSeek's thinking mode answers ``400 Thinking mode
    does not support this tool_choice``). Prompted output puts the JSON
    schema in the instructions and validates the reply, which every chat
    model supports; ``retries`` covers a reply that fails validation.
    """
    return Agent(
        model,
        output_type=PromptedOutput(GraphDraft, name="GraphDraft"),
        instructions=GENERATE_INSTRUCTIONS,
        retries=AGENT_RETRIES,
        defer_model_check=True,
    )


def _format_problems(problems: Sequence[str]) -> str:
    return "\n".join(f"- {problem}" for problem in problems)


def _review_problems(findings: Sequence[Finding]) -> list[str]:
    return [
        f"{f.code}: {f.message}" + (f" (nodes: {', '.join(f.node_ids)})" if f.node_ids else "")
        for f in findings
    ]


def fake_generate_enabled() -> bool:
    """Whether the deterministic stub draft is used (read per call, never cached).

    True when ``SWARM_FAKE_GENERATE=1`` *or* the application's own dry-run
    switch is on (:func:`swarm_builder.runtime.dry_run_active`). Generation is
    a single request rather than a long job, so there is no mid-flight state
    to freeze: the answer is read once per request, which is exactly the
    lifetime of the decision it feeds.
    """
    from swarm_builder import runtime

    return runtime.dry_run_active() or (
        os.environ.get(FAKE_GENERATE_ENV_VAR) == FAKE_GENERATE_ENABLED_VALUE
    )


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+|;\s+|\bthen\b", re.IGNORECASE)
_AGENT_HINT_RE = re.compile(
    r"\b(agent|llm|model|summar|draft|write|answer|explain|search|research|chat|classify)\w*",
    re.IGNORECASE,
)


def fake_draft(description: str) -> GraphDraft:
    """A deterministic sentence-per-step linear draft (``SWARM_FAKE_GENERATE=1``).

    Each sentence becomes one node: an ``agent`` when it mentions
    model-ish work, a ``programmatic`` step otherwise. All ports are
    ``str`` so the chain is review-clean for any description.
    """
    sentences = [s.strip(" .") for s in _SENTENCE_SPLIT_RE.split(description) if s.strip(" .")]
    if not sentences:
        sentences = ["Handle the input"]
    sentences = sentences[:8]
    nodes: list[DraftNode] = []
    used: set[str] = set()
    for sentence in sentences:
        words = re.findall(r"[A-Za-z0-9]+", sentence)[:4]
        title = " ".join(words).strip() or f"Step {len(nodes) + 1}"
        base = title
        counter = 2
        while title.lower() in used:
            title = f"{base} {counter}"
            counter += 1
        used.add(title.lower())
        kind: NodeKind = "agent" if _AGENT_HINT_RE.search(sentence) else "programmatic"
        nodes.append(DraftNode(title=title, kind=kind, intent=sentence))
    edges = [
        DraftEdge(source=nodes[i].title, target=nodes[i + 1].title) for i in range(len(nodes) - 1)
    ]
    name = " ".join(re.findall(r"[A-Za-z0-9]+", sentences[0])[:5]) or "Generated workflow"
    return GraphDraft(name=name, nodes=nodes, edges=edges)


async def generate_graph(
    description: str,
    *,
    model: Model | str | None,
    graph_id: str,
    max_repairs: int = MAX_REPAIRS,
) -> GenerateResult:
    """Describe -> draft -> materialize -> review, repairing up to ``max_repairs`` times.

    Args:
        description: The user's prose.
        model: What ``Agent(...)`` runs on (a ``LiveModel.model``), or
            ``None`` -- allowed only with ``SWARM_FAKE_GENERATE=1``.
        graph_id: The id the document will be saved under.
        max_repairs: Feedback rounds after the first draft.

    Returns:
        The review-clean graph, its warnings, and how many drafts it took.

    Raises:
        GenerateError: If no attempt produced a review-clean graph, or the
            request budget ran out. Carries the last round's problems.
        ValueError: If ``model`` is ``None`` and the fake path is off.
    """
    if fake_generate_enabled():
        graph = materialize(fake_draft(description), graph_id)
        result = review(graph)
        if not result.ok:
            raise GenerateError(_review_problems(result.errors), 1)
        return GenerateResult(graph=graph, warnings=result.warnings, attempts=1)

    if model is None:
        raise ValueError("generate_graph needs a model unless SWARM_FAKE_GENERATE=1 is set")

    agent = build_generate_agent(model)
    limits = UsageLimits(request_limit=REQUEST_LIMIT)
    prompt = description
    history = None
    problems: list[str] = []
    attempts = 0
    while attempts <= max_repairs:
        attempts += 1
        try:
            run = await agent.run(prompt, message_history=history, usage_limits=limits)
        except UsageLimitExceeded as exc:
            raise GenerateError([*problems, f"request budget exhausted: {exc}"], attempts) from exc
        try:
            graph = materialize(run.output, graph_id)
        except DraftError as exc:
            problems = [str(exc)]
        else:
            result = review(graph)
            if result.ok:
                return GenerateResult(graph=graph, warnings=result.warnings, attempts=attempts)
            problems = _review_problems(result.errors)
        history = run.all_messages()
        prompt = _REPAIR_PROMPT.format(problems=_format_problems(problems))
    raise GenerateError(problems, attempts)


__all__ = [
    "AGENT_RETRIES",
    "FAKE_GENERATE_ENABLED_VALUE",
    "FAKE_GENERATE_ENV_VAR",
    "MAX_REPAIRS",
    "REQUEST_LIMIT",
    "DraftEdge",
    "DraftError",
    "DraftNode",
    "DraftStateField",
    "GenerateError",
    "GenerateResult",
    "GraphDraft",
    "build_generate_agent",
    "fake_draft",
    "fake_generate_enabled",
    "generate_graph",
    "layout_positions",
    "materialize",
]
