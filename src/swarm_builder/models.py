"""The graph document schema: the one source of truth for a Swarm Builder
canvas save file.

Validated on every read and write (Phase 1 review and Phase-5 codegen
build on top of this, but the shapes below are the actual gate), and the
schema the frontend's TypeScript types are generated from via the
server's OpenAPI document. Versioned from day one
(:class:`SwarmGraph.version`) so a future shape change is a migration,
never a silent break.

**Naming convention (read this before adding a field).** The project's
prose mixes camelCase (``entryNodeId``, ``stateFields``, ``delegatesTo``)
and snake_case (``entry_node_id``, ``state_fields``, ``join_node_id``) when
describing this schema. This module resolves that inconsistency by a
single deliberate rule, applied everywhere via :class:`SwarmBaseModel`:

- **Python-side field names are snake_case** (idiomatic Python, and what
  every constructor call and attribute access in this codebase uses).
- **JSON aliases are camelCase**, generated automatically
  (``entry_node_id`` <-> ``entryNodeId``), because graph documents
  round-trip through JSON files on disk and the frontend consumes
  OpenAPI-generated TypeScript types that must read naturally as
  JavaScript.
- ``populate_by_name=True`` means both spellings are accepted on the way
  in (so constructing a model in Python with keyword arguments uses the
  snake_case field name, while parsing JSON from a file or an HTTP
  request uses the camelCase alias); serialization always emits the
  camelCase alias (``by_alias=True`` is the persisted/wire shape).
- ``extra="forbid"`` on every model: an unrecognized key is almost always
  a typo or a stale frontend build, and version 1 has no forward-
  compatibility contract yet to justify silently dropping fields.

**Round-tripping.** A graph document is read from a JSON file, validated,
optionally edited, and written back. For that to be lossless (an explicit
acceptance criterion), field order must be stable across a dump ->
validate -> dump cycle -- which pydantic already guarantees for models
whose field order does not change -- and every field must have an
unambiguous camelCase alias so no information is renamed inconsistently
between the two on-disk/wire representations.

**Two authoritative lookup tables live here, not in the emitter or
validator**, because both need the exact same mapping and a second copy
would drift:

- :data:`PORT_TYPE_ANNOTATIONS` / :data:`PORT_TYPE_IMPORTS` -- a
  :data:`PortType` value must never be interpolated into a Python
  annotation verbatim: ``'json'`` is not a valid annotation (the
  identifier ``json`` is a module, not a type), and ``'list[str]'`` would
  be a string constant rather than a generic. Both the emitter's
  step-module codegen and Phase 1's port-type-mismatch check need one
  shared table.
- :data:`REDUCER_FUNCTIONS` -- exactly the four ``ReducerId`` literals,
  each mapping to one ``pydantic_graph`` reducer function name.
  ``reduce_null`` exists upstream but intentionally has no ``ReducerId``
  counterpart.
"""

from __future__ import annotations

from datetime import datetime

# `Any` looks unused but is not: the string in PORT_TYPE_ANNOTATIONS below
# is emitted verbatim as the annotation in a generated project's module,
# and PORT_TYPE_IMPORTS says which import line that module needs. This
# import is the executable proof that the name is importable from here.
from typing import Annotated, Any, Literal  # noqa: F401

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

# ---------------------------------------------------------------------------
# Base model: the naming-convention rule described in the module docstring.
# ---------------------------------------------------------------------------


class SwarmBaseModel(BaseModel):
    """Shared config for every graph-document model: snake_case Python
    fields, camelCase JSON aliases, both spellings accepted on input."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

NodeKind = Literal["agent", "programmatic", "decision", "join"]

#: The v1 template catalog. A 'rag' template is deliberately deferred: its
#: embedder and cache bring dependency and sandbox friction no other
#: template has. Nothing currently indexes this Literal positionally, so
#: adding a fourth member later is an additive change rather than a
#: reshape of this type or of anything that matches on it.
TemplateId = Literal["chat", "orchestrator", "websearch"]

PortType = Literal["str", "json", "list[str]"]

#: Exactly four literals: `reduce_null` exists upstream but has no
#: `ReducerId` counterpart, so this type's lookup table
#: (`REDUCER_FUNCTIONS`) has exactly four entries too. Joining without a
#: reducer is expressed by omitting the join spec, not by `reduce_null`.
ReducerId = Literal["list_append", "list_extend", "dict_update", "sum"]


# ---------------------------------------------------------------------------
# Authoritative lookup tables. One copy here: the emitter and the
# validator both import from this module rather than redefining either
# table, because a second copy would drift and only one of the two would
# be updated when a port type or reducer is added.
# ---------------------------------------------------------------------------

#: PortType -> the Python annotation the emitter must write for a step's
#: parameter/return type. Never interpolate a PortType value directly into
#: generated source: the label `json` is not a usable annotation, so
#: emitting it verbatim produces a module that raises `NameError` when the
#: graph is built.
PORT_TYPE_ANNOTATIONS: dict[PortType, str] = {
    "str": "str",
    "list[str]": "list[str]",
    "json": "dict[str, Any]",
}

#: PortType -> the import line the emitted module needs for its
#: annotation, or None when no import is required (`str` and `list[str]`
#: are builtins). Kept alongside PORT_TYPE_ANNOTATIONS since the two are
#: always consulted together.
PORT_TYPE_IMPORTS: dict[PortType, str | None] = {
    "str": None,
    "list[str]": None,
    "json": "from typing import Any",
}

#: ReducerId -> the pydantic_graph reducer function name to import and
#: pass to `builder.join(...)`. Exactly four entries (see ReducerId's
#: docstring above) -- `reduce_null` is deliberately absent.
REDUCER_FUNCTIONS: dict[ReducerId, str] = {
    "list_append": "reduce_list_append",
    "list_extend": "reduce_list_extend",
    "dict_update": "reduce_dict_update",
    "sum": "reduce_sum",
}


# ---------------------------------------------------------------------------
# Small shared shapes
# ---------------------------------------------------------------------------


class Position(SwarmBaseModel):
    """Canvas coordinates for a node. Presentation-only: never read by
    the emitter, kept purely so the canvas restores node placement."""

    x: float
    y: float


class NodeIo(SwarmBaseModel):
    """A node's declared input/output port types.

    Phase 1 review uses these to reject a port-type mismatch across an
    edge and, for a decision's branch targets, to enforce that the
    target's ``input_type`` equals the decision's *source step's*
    ``output_type`` -- see :class:`DecisionSpec` for why the source step is
    the relevant node rather than the decision itself.
    """

    input_type: PortType
    output_type: PortType


class StateField(SwarmBaseModel):
    """One field of the generated workflow's ``@dataclass State``.

    ``type`` goes through :data:`PORT_TYPE_ANNOTATIONS` like any other
    port type when the emitter writes the dataclass field.
    ``default`` is literal Python source for the field's default value
    (e.g. ``'""'`` or ``"0"``); ``None`` means the emitter writes a bare
    ``name: type`` line with no default.
    """

    name: str
    type: PortType
    default: str | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# Per-kind node specs
# ---------------------------------------------------------------------------


class AgentSpec(SwarmBaseModel):
    """Present on ``kind="agent"`` nodes: instructions, tools, delegation,
    and optional structured output for the generated agent factory.

    ``output_schema`` is a JSON Schema object. Its values are typed as
    ``object`` rather than ``Any``: the *values* are arbitrary, but the
    container is not, and ``Any`` would also silently accept a list or a
    scalar where a schema object was meant.
    """

    instructions: str
    tools: list[str] = Field(default_factory=list)
    delegates_to: list[str] = Field(default_factory=list)
    output_schema: dict[str, object] | None = None


class ProgrammaticSpec(SwarmBaseModel):
    """Present on ``kind="programmatic"`` nodes: plain-Python steps with
    no agent. ``needs`` are extra package requirements the scaffolder
    unions into the generated ``pyproject.toml``; ``signature_hint`` is
    free-text guidance the fill agent reads (never executed or parsed)."""

    needs: list[str] = Field(default_factory=list)
    signature_hint: str | None = None


class DecisionBranch(SwarmBaseModel):
    """One branch of a :class:`DecisionSpec`: a match value and the node
    it dispatches to. ``target_node_id`` must also be the target of a
    ``branch`` :class:`SwarmEdge` with a matching ``match`` string."""

    match: str
    target_node_id: str


class DecisionSpec(SwarmBaseModel):
    """Present on ``kind="decision"`` nodes: the branch dispatch table.

    **Data does not pass through a decision node.** A branch
    target step receives, via ``ctx.inputs``, the *match value returned
    by the decision's source step* -- not the payload that flowed into
    that source step. Concretely: if the source step returns
    ``Literal["big", "small"]``, the ``big`` branch target observes
    ``ctx.inputs == "big"``, never the original pre-classification
    string. Two consequences that hold regardless of what Phase 1 (Group
    2's review.py) enforces mechanically:

    - a branch target's declared :class:`NodeIo.input_type` must equal
      the **decision's source step's** ``output_type``, never any node
      further upstream;
    - a workflow that needs the pre-classification payload inside a
      branch target must carry it through ``State`` explicitly (the
      classifying step writes a state field the branch target reads);
      ``ctx.inputs`` will never carry it through the decision.

    Two consequences for the emitted code, both enforced elsewhere: the
    full ``.branch(...)`` chain must be emitted before the ``add_edge``
    that targets this decision (``emit_graph.py``), and branch targets'
    declared input types must match the source step's output type, not any
    node further upstream (``review.py``).
    """

    branches: list[DecisionBranch] = Field(default_factory=list)
    note: str | None = None


class JoinSpec(SwarmBaseModel):
    """Present on ``kind="join"`` nodes: the fan-in reducer.

    ``initial_factory`` names the Python builtin the emitter passes as
    ``builder.join(..., initial_factory=...)``. When unset, the emitter
    derives it from ``reducer`` (``list_append``/``list_extend`` ->
    ``list``, ``dict_update`` -> ``dict``, ``sum`` -> ``int``); set it
    explicitly only to override that default.
    """

    reducer: ReducerId
    initial_factory: Literal["list", "dict", "int"] | None = None


# ---------------------------------------------------------------------------
# SwarmNode
# ---------------------------------------------------------------------------


class SwarmNode(SwarmBaseModel):
    """One canvas node.

    ``id`` is emitted verbatim as the ``node_id=`` argument to
    ``builder.step``/``builder.decision``/``builder.join`` and as the step
    module's filename, so it must already be a valid, unique Python
    identifier. The frontend is responsible for deriving it from ``title``
    via :func:`swarm_builder.slugify.slugify_titles` and keeping it stable
    across edits: duplicate titles deduplicate with a numeric suffix, and
    the title-to-id mapping must stay stable across recompiles or a
    recompile would rename step modules instead of updating them.

    Exactly one of ``agent`` / ``programmatic`` / ``decision`` / ``join``
    is expected to be set, matching ``kind`` -- enforced by Phase 1
    (``review.py``), not here, since that cross-field rule belongs with the
    rest of the structural graph validation.
    """

    id: str
    kind: NodeKind
    title: str
    intent: str
    position: Position
    template: TemplateId | None = None
    io: NodeIo
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)
    agent: AgentSpec | None = None
    programmatic: ProgrammaticSpec | None = None
    decision: DecisionSpec | None = None
    join: JoinSpec | None = None


# ---------------------------------------------------------------------------
# SwarmEdge: a discriminated union, not a bare {source, target} pair. A
# single edge type cannot express a branch's match value, a fan-out's join
# target, or the fact that a delegate edge must never become an add_edge
# at all -- and each of those distinctions changes the emitted code.
# ---------------------------------------------------------------------------


class SeqEdge(SwarmBaseModel):
    """A plain sequential edge: ``builder.add_edge(source, target)``."""

    kind: Literal["seq"]
    id: str
    source: str
    target: str
    label: str | None = None


class BranchEdge(SwarmBaseModel):
    """One arm of a decision's dispatch, drawn on the canvas from the
    ``decision`` node to a branch target. ``match`` must correspond to
    one entry in the decision node's :class:`DecisionSpec.branches`."""

    kind: Literal["branch"]
    id: str
    source: str
    target: str
    match: str


class FanoutEdge(SwarmBaseModel):
    """One arm of a multi-successor fan-out.

    ``join_node_id`` names the ``join`` node this arm must converge on.
    It is required rather than optional because a plain multi-successor
    ``add_edge`` pair is silently lossy: ``build()`` accepts it, and only
    one arbitrary branch's value survives without an explicit join.
    """

    kind: Literal["fanout"]
    id: str
    source: str
    target: str
    join_node_id: str


class JoinEdge(SwarmBaseModel):
    """An edge from a fan-out arm's target into the ``join`` node."""

    kind: Literal["join"]
    id: str
    source: str
    target: str


class DelegateEdge(SwarmBaseModel):
    """A delegation link from an orchestrator node to a child agent node.

    **Never emitted as an ``add_edge``.** The child is called as an agent
    *tool*, not executed as a graph step; emitting a real edge would run it
    twice. This edge kind exists purely so the canvas can draw the
    relationship and the Inspector can show it: ``emit_graph.py`` skips
    every ``delegate`` edge, and Phase 1 rejects a node that is both a
    ``delegate`` target and a ``seq``/``branch`` target.
    """

    kind: Literal["delegate"]
    id: str
    source: str
    target: str


SwarmEdge = Annotated[
    SeqEdge | BranchEdge | FanoutEdge | JoinEdge | DelegateEdge,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Model selection and the top-level document
# ---------------------------------------------------------------------------


class ModelSelection(SwarmBaseModel):
    """One inherited route, resolved to something PydanticAI can
    construct (facts 21-24). Absent on :class:`SwarmGraph` means
    "inherit the resolved default" rather than "no model"."""

    provider: str
    model: str
    reasoning_effort: str | None = None


class SwarmGraph(SwarmBaseModel):
    """The complete canvas save file: nodes, edges, state shape, and the
    optional per-workflow model override. ``version`` is fixed at ``1``
    for this schema generation; a future incompatible shape change ships
    as ``version=2`` plus a migration, not a silent field rename.
    """

    version: Literal[1] = 1
    id: str
    name: str
    entry_node_id: str
    exit_node_id: str
    state_fields: list[StateField] = Field(default_factory=list)
    nodes: list[SwarmNode] = Field(default_factory=list)
    edges: list[SwarmEdge] = Field(default_factory=list)
    model: ModelSelection | None = None
    updated_at: datetime

    @model_validator(mode="after")
    def _check_structural_integrity(self) -> SwarmGraph:
        """Reject a document whose ids don't refer to nodes that exist.

        Deliberately narrow: cycle/orphan/reachability/state-ownership
        checks belong to ``review.py`` (Phase 1), which has
        the full picture (edge kinds, state fields, delegation) needed
        for those richer rules. This validator only guards the small set
        of invariants every other check silently assumes:

        - every node ``id`` is unique (a duplicate is emitted verbatim as
          ``node_id=`` and raises ``GraphBuildingError`` at build time,
          far downstream of where a document-load check could have caught
          it instead);
        - ``entry_node_id`` and ``exit_node_id`` each name a real node;
        - every edge's ``source``/``target`` names a real node.

        So a malformed document can never even reach Phase 1.
        """
        node_ids_seen: set[str] = set()
        for node in self.nodes:
            if node.id in node_ids_seen:
                raise ValueError(f"duplicate node id {node.id!r}")
            node_ids_seen.add(node.id)

        if self.entry_node_id not in node_ids_seen:
            raise ValueError(
                f"entry_node_id {self.entry_node_id!r} is not a known node id"
            )
        if self.exit_node_id not in node_ids_seen:
            raise ValueError(
                f"exit_node_id {self.exit_node_id!r} is not a known node id"
            )

        for edge in self.edges:
            if edge.source not in node_ids_seen:
                raise ValueError(
                    f"edge {edge.id!r} ({edge.kind}) has unknown source "
                    f"node id {edge.source!r}"
                )
            if edge.target not in node_ids_seen:
                raise ValueError(
                    f"edge {edge.id!r} ({edge.kind}) has unknown target "
                    f"node id {edge.target!r}"
                )
        return self
