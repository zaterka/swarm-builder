"""Phase-1 review: the deterministic validator run before any code is
emitted.

This is the gate that catches the defects ``pydantic_graph.build()``
cannot: a self-cycle builds and runs, an unedged step is silently dropped,
a plain multi-successor fan-out silently keeps one arbitrary branch, a
decision with no branches builds and then raises ``RuntimeError: No branch
matched inputs`` at run time. Nothing downstream of Phase 1 re-checks any
of this, so a defect that gets past here reaches a generated project.

:func:`review` returns a structured :class:`ReviewResult` (errors plus
warnings, each naming the participating nodes) rather than raising, so
callers -- ``routes/graphs.py``'s review endpoint, the codegen test suite,
``pipeline.py`` -- can render every finding instead of stopping at the
first one. Returning a structured result is deliberate and is not the
"error signalling by return value" this project's conventions forbid:
there is no success/failure ambiguity to miss, because a caller that
ignores ``errors`` is a caller that ignores an explicit list of
violations.

``review.py`` deliberately does NOT check "unmappable route protocol":
that is a route-level concern owned by ``inherit/routes.py`` and the
pipeline orchestrator, since this module only ever sees a
:class:`SwarmGraph`, never a resolved route.
"""

from __future__ import annotations

import json
import keyword
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field

from swarm_builder.compile.graph_ir import (
    GraphStructure,
    analyze,
)
from swarm_builder.models import (
    BranchEdge,
    DelegateEdge,
    FanoutEdge,
    JoinEdge,
    NosqlSpec,
    SeqEdge,
    SqlSpec,
    SwarmGraph,
    SwarmNode,
    VectorDocument,
    VectorSpec,
)
from swarm_builder.slugify import IDENTIFIER_RE

#: Node kind -> the name of the single spec field that kind requires.
#: Used to assert "exactly one spec, and the right one" per node.
_KIND_TO_SPEC_ATTR: dict[str, str] = {
    "agent": "agent",
    "programmatic": "programmatic",
    "decision": "decision",
    "join": "join",
    "sql": "sql",
    "nosql": "nosql",
    "vector": "vector",
}

#: Depth-first-search colours for the cycle check.
_UNVISITED = 0
_ON_CURRENT_PATH = 1
_FINISHED = 2

#: Join reducers that accumulate into a list, and the output port type they
#: are expected to produce. A mismatch is a warning, not an error.
_LIST_REDUCERS = ("list_append", "list_extend")
_LIST_REDUCER_OUTPUT_TYPE = "list[str]"

#: Fewer inbound arms than this makes a join structurally unusual (and is
#: more often a modeling slip than a deliberate choice).
_MIN_JOIN_ARMS = 2

#: The three database kinds. Declared here rather than imported from
#: ``compile.database``, which has the same set: that module imports the starter
#: catalog at import time, and this module must not -- see
#: :func:`_check_db_starter_drift`.
_DATABASE_KINDS = ("sql", "nosql", "vector")

#: The one output port a database node may declare. A read returns rows, so
#: coercing the result to ``json`` (one dict) or ``str`` is lossy the moment it
#: crosses an edge.
_DATABASE_OUTPUT_TYPE = "list[json]"

#: kind -> the input port types that kind can bind. Everything else is rejected:
#: ``list[json]`` for all three (a list of rows is not a parameter source) and
#: ``list[str]`` for vector (a similarity search's input is the query text).
_BINDABLE_INPUT_TYPES: dict[str, tuple[str, ...]] = {
    "sql": ("str", "json", "list[str]"),
    "nosql": ("str", "json", "list[str]"),
    "vector": ("str",),
}

#: kind -> how that kind's bindable input reads inside a finding's message, so
#: the message states the rule rather than only the violation.
_INPUT_TYPE_RULE: dict[str, str] = {
    "sql": (
        "'str' (bound as the single parameter ':input'), "
        "'list[str]' (the same ':input' placeholder, expanded to one bound name per "
        "element so 'IN (:input)' works), or "
        "'json' (its keys bind directly as ':key', plus ':input' for the whole dict)"
    ),
    "nosql": (
        "'str' (the value a filter's '$input' sentinel takes), "
        "'list[str]' (the same sentinel, taking the whole list -- the shape an "
        "'$in' needs), or "
        "'json' (merged over the filter's top level)"
    ),
    "vector": "'str' (the query text)",
}

#: kind -> the spec fields :func:`_check_db_operations` refuses to see empty,
#: and why an empty one is a defect rather than a degraded node. One row per
#: field the plan names, so the message can say what would go wrong.
_EMPTY_OPERATION_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "sql": (
        ("query", "the emitted step would run an empty statement"),
        ("seed_sql", "the mock's database would have no schema and no rows"),
    ),
    "nosql": (
        ("collection", "the emitted step would read a collection with no name"),
        ("operation", "the emitted step would call an operation the repository does not implement"),
    ),
    "vector": (("collection", "the emitted index would have no name to search"),),
}

#: A ``:name`` placeholder in a SQL declaration, ignoring ``::type`` casts: the
#: lookbehind skips a cast's second colon, so ``total::numeric`` yields no
#: placeholder while ``WHERE name = :input`` yields ``input``.
_SQL_PLACEHOLDER_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")

#: kind -> the spec fields :func:`_check_db_starter_drift` compares with the
#: kind's ``starter.json``: the operation and the seed, which is what the plan's
#: rule names. ``note`` is deliberately absent -- it is free help text the
#: Inspector renders, so rewriting it does not change what the node does.
_DRIFT_FIELDS: dict[str, tuple[str, ...]] = {
    "sql": ("query", "seed_sql", "write"),
    "nosql": ("collection", "operation", "filter", "limit", "seed"),
    "vector": ("collection", "top_k", "min_score", "seed"),
}


@dataclass(frozen=True)
class _DbToolReference:
    """One ``<kind>:<node_id>`` entry in an agent's ``tools`` list, resolved.

    The two checks over these entries (a bad reference and a write operation)
    share this resolution, so "which node does this entry name" is answered in
    one place and neither check can disagree with the other about it.
    ``target`` is ``None`` when the id names no node at all.
    """

    agent_id: str
    entry: str
    kind: str
    target_id: str
    target: SwarmNode | None


@dataclass(frozen=True)
class Finding:
    """One review finding: a machine-readable ``code``, a human message,
    and the node ids involved (possibly empty for a graph-wide finding)."""

    code: str
    message: str
    node_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewResult:
    """Everything Phase 1 found, split by severity.

    A caller decides what to do with each list; the pipeline treats any
    entry in :attr:`errors` as a Phase-1 failure, and surfaces
    :attr:`warnings` without blocking the compile.
    """

    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the graph passed review, i.e. produced no errors."""
        return not self.errors


class _FindingCollector:
    """Accumulate findings during one review pass.

    Exists so every check function takes one collaborator instead of
    threading two mutable lists (and so a check can be unit-tested by
    inspecting what was collected from it alone).
    """

    def __init__(self) -> None:
        """Start a review pass with nothing collected."""
        self.errors: list[Finding] = []
        self.warnings: list[Finding] = []

    def error(
        self, code: str, message: str, node_ids: tuple[str, ...] = ()
    ) -> None:
        """Record a blocking finding."""
        self.errors.append(Finding(code=code, message=message, node_ids=node_ids))

    def warn(
        self, code: str, message: str, node_ids: tuple[str, ...] = ()
    ) -> None:
        """Record a non-blocking finding."""
        self.warnings.append(Finding(code=code, message=message, node_ids=node_ids))


def review(graph: SwarmGraph) -> ReviewResult:
    """Validate one canvas document, collecting every finding.

    Runs the structural-integrity re-checks first (they are cheap and
    every later check assumes ids resolve), then analyses the graph shape
    once and reuses that analysis across the reachability, cycle, fan-out,
    decision, port-type, sink and state-ownership rules.

    Args:
        graph: The document to validate, as read from disk or a request.

    Returns:
        Every finding, split into errors and warnings. Never raises for a
        defective graph -- an unreadable or unparseable document fails
        earlier, at the pydantic layer.
    """
    findings = _FindingCollector()
    node_by_id = {node.id: node for node in graph.nodes}

    _check_unknown_edge_endpoints(graph, node_by_id, findings)
    _check_node_id_identifiers(graph, findings)
    _check_kind_spec_consistency(graph, findings)
    _check_missing_intent(graph, findings)
    _check_delegate_vs_structural_target(graph, findings)
    _check_delegate_targets_are_agents(graph, node_by_id, findings)

    structure = analyze(graph)

    _check_reachability(graph, structure, findings)
    _check_cycles(graph, structure, findings)
    _check_fanout_without_join(graph, structure, node_by_id, findings)
    _check_decision_branches(graph, structure, node_by_id, findings)
    _check_port_types(graph, node_by_id, findings)
    _check_sink_output_consistency(graph, structure, node_by_id, findings)
    _check_state_ownership(graph, structure, findings)
    _check_db_operations(graph, findings)
    _check_db_template_set(graph, findings)
    _check_db_operation_io(graph, findings)
    _check_db_input_type_binding(graph, findings)
    _check_db_placeholders(graph, findings)
    _check_db_seed_validity(graph, findings)
    _check_db_tool_references(graph, node_by_id, findings)
    _check_db_tool_writes(graph, node_by_id, findings)
    _check_db_limit_warnings(graph, findings)
    _check_db_starter_drift(graph, findings)
    _check_db_seed_sharing(graph, findings)
    _check_delegates_to_warnings(graph, node_by_id, findings)
    _check_join_shape_warnings(graph, structure, node_by_id, findings)

    return ReviewResult(errors=findings.errors, warnings=findings.warnings)


# ---------------------------------------------------------------------------
# Structural-integrity re-checks (defensive; models.py already guarantees
# most of this at the document level, but review.py re-derives from the
# edge list directly rather than trusting the caller didn't mutate).
# ---------------------------------------------------------------------------


def _check_unknown_edge_endpoints(
    graph: SwarmGraph, node_by_id: dict[str, SwarmNode], findings: _FindingCollector
) -> None:
    """Report an edge whose source or target names no known node.

    ``models.py`` already rejects such a document on load, but this
    re-derives the check from the edge list rather than trusting that a
    caller did not assemble a graph in memory: every later rule indexes
    ``node_by_id`` by an edge endpoint, so a missing key there would
    surface as a ``KeyError`` rather than as a finding.
    """
    for edge in graph.edges:
        if edge.source not in node_by_id:
            findings.error(
                "unknown_edge_endpoint",
                f"edge {edge.id!r} ({edge.kind}) has unknown source node id {edge.source!r}",
                (edge.source,),
            )
        if edge.target not in node_by_id:
            findings.error(
                "unknown_edge_endpoint",
                f"edge {edge.id!r} ({edge.kind}) has unknown target node id {edge.target!r}",
                (edge.target,),
            )


def _check_node_id_identifiers(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Reject a node id that is not usable as a generated identifier.

    A node id becomes the emitted step function's name and its module
    filename, so it must match a Python-identifier pattern and not be a
    keyword. Deriving and deduplicating the id from ``title`` is the
    frontend's job (see ``models.py``); this module only validates, and
    never re-slugifies.
    """
    for node in graph.nodes:
        if not IDENTIFIER_RE.match(node.id):
            findings.error(
                "invalid_node_id",
                f"node id {node.id!r} is not a valid Python identifier",
                (node.id,),
            )
        elif keyword.iskeyword(node.id) or keyword.issoftkeyword(node.id):
            findings.error(
                "invalid_node_id",
                f"node id {node.id!r} collides with a Python keyword",
                (node.id,),
            )


def _check_kind_spec_consistency(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Require exactly the one spec field a node's ``kind`` calls for.

    Without this, ``emit_graph.py`` dereferences a spec that is not there
    -- ``node.join.reducer`` on a ``kind="join"`` node carrying no
    ``join`` spec is an ``AttributeError`` deep inside emission, after
    scaffolds have already been written.
    """
    for node in graph.nodes:
        specs_present = [
            attr for attr in _KIND_TO_SPEC_ATTR
            if getattr(node, attr) is not None
        ]
        expected_attr = _KIND_TO_SPEC_ATTR[node.kind]
        if specs_present != [expected_attr]:
            findings.error(
                "kind_spec_mismatch",
                f"node {node.id!r} has kind={node.kind!r} but spec fields "
                f"present are {specs_present!r} (expected exactly [{expected_attr!r}])",
                (node.id,),
            )


def _check_missing_intent(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Reject a node whose intent is empty or whitespace.

    The intent is both the fill agent's only instruction for what a step
    body should do and the source of the emitted module's docstring, so an
    empty one produces an unbuildable step rather than a degraded one.
    """
    for node in graph.nodes:
        if not node.intent.strip():
            findings.error("missing_intent", f"node {node.id!r} has no intent", (node.id,))


def _check_delegate_vs_structural_target(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Reject a node reached both by delegation and by a real edge.

    A delegate target is called as an agent *tool* by its orchestrator and
    is never emitted as a graph step; a node that is also a ``seq`` or
    ``branch`` target is emitted as a step too, so it would execute twice.
    """
    delegate_targets = {e.target for e in graph.edges if isinstance(e, DelegateEdge)}
    seq_or_branch_targets = {
        e.target for e in graph.edges if isinstance(e, (SeqEdge, BranchEdge))
    }
    both = delegate_targets & seq_or_branch_targets
    for node_id in sorted(both):
        findings.error(
            "delegate_and_sequence_target",
            f"node {node_id!r} is both a delegate target and a seq/branch target",
            (node_id,),
        )


# ---------------------------------------------------------------------------
# Reachability / cycles
# ---------------------------------------------------------------------------


def _check_reachability(
    graph: SwarmGraph, structure: GraphStructure, findings: _FindingCollector
) -> None:
    """Require a path from entry to exit, and no stranded nodes.

    Walks the *dispatch* graph (structural plus branch edges), not the
    structural graph alone: a decision's branch targets are reachable
    through ``.branch(...).to(...)`` even though that never becomes a
    literal ``add_edge``, and walking only structural edges would report
    every branch target as unreachable.

    A delegate-only node is exempt: it is called as a tool by its
    orchestrator and never appears on a path from entry to exit.

    An entry id that resolves to no node is left alone here rather than
    reported: ``_check_unknown_edge_endpoints`` already covers edges, and
    ``models.py`` rejects a document with an unknown ``entry_node_id`` on
    load, so this can only be reached by a graph assembled in memory. The
    early return keeps review's findings unchanged from before.
    """
    if graph.entry_node_id not in structure.node_by_id:
        return

    reachable: set[str] = set()
    stack = [graph.entry_node_id]
    while stack:
        node_id = stack.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        stack.extend(structure.dispatch_successors.get(node_id, []))

    if graph.exit_node_id not in reachable:
        findings.error(
            "no_path_to_exit",
            f"no path from entry {graph.entry_node_id!r} to exit {graph.exit_node_id!r}",
            (graph.entry_node_id, graph.exit_node_id),
        )

    unreachable = [
        n.id
        for n in graph.nodes
        if n.id not in reachable and n.id not in structure.delegate_only_node_ids
    ]
    for node_id in unreachable:
        findings.error(
            "unreachable_node",
            f"node {node_id!r} is not reachable from entry {graph.entry_node_id!r}",
            (node_id,),
        )


def _check_cycles(
    graph: SwarmGraph, structure: GraphStructure, findings: _FindingCollector
) -> None:
    """Reject any cycle in the graph.

    Cycles are checked over the *dispatch* graph (structural plus branch
    edges, never delegate), because that is the set of edges the emitted
    ``build()`` actually walks. This check is load-bearing rather than
    defensive: a self-cycle builds and runs, so nothing downstream of
    Phase 1 will ever notice one.
    """
    color: dict[str, int] = dict.fromkeys(structure.node_by_id, _UNVISITED)
    cyclic_nodes: set[str] = set()

    def visit(node_id: str, path: list[str]) -> None:
        """Depth-first walk, recording every node on a back-edge's cycle."""
        color[node_id] = _ON_CURRENT_PATH
        path.append(node_id)
        for successor in structure.dispatch_successors.get(node_id, []):
            successor_color = color.get(successor, _UNVISITED)
            if successor_color == _UNVISITED:
                visit(successor, path)
            elif successor_color == _ON_CURRENT_PATH:
                # A back-edge: everything from the successor onwards in the
                # current path, plus that successor, is the cycle.
                if successor in path:
                    cycle_start = path.index(successor)
                    cyclic_nodes.update(path[cycle_start:])
                else:
                    cyclic_nodes.add(successor)
        path.pop()
        color[node_id] = _FINISHED

    for node_id in structure.node_by_id:
        if color[node_id] == _UNVISITED:
            visit(node_id, [])

    if cyclic_nodes:
        findings.error(
            "cycle",
            f"cycle detected among nodes {sorted(cyclic_nodes)!r}",
            tuple(sorted(cyclic_nodes)),
        )


# ---------------------------------------------------------------------------
# Fan-out / join
# ---------------------------------------------------------------------------


def _check_fanout_without_join(
    graph: SwarmGraph,
    structure: GraphStructure,
    node_by_id: dict[str, SwarmNode],
    findings: _FindingCollector,
) -> None:
    """Require every fan-out to converge on one real join node.

    A plain multi-successor ``add_edge`` pair is silently lossy: the built
    graph returns one arbitrary branch's value and drops the rest, with no
    build-time or run-time error. So every node that *looks* like a fan-out
    must be spelled as ``FanoutEdge`` arms, and those arms must agree on a
    ``join_node_id`` that names a real ``kind="join"`` node, and each arm
    target must itself carry a ``JoinEdge`` into that node.

    Two distinct populations are checked, and missing either lets a lossy
    graph through:

    1. every node with a ``FanoutEdge`` -- including a lone one-arm
       ``FanoutEdge``, which ``models.py`` does not forbid and which must
       still resolve to a real join node. ``fanout_source_node_ids`` alone
       would miss that case, since it only names nodes with two or more
       structural successors;
    2. every node with two or more structural successors *however those
       are spelled* -- crucially including two plain ``SeqEdge``s and no
       ``FanoutEdge`` at all. That is the canonical lossy shape a user
       actually draws (``a -> left`` plus ``a -> right`` builds and runs,
       returning one arbitrary branch), and iterating only
       ``fanout_edges_by_source`` would never examine it, because such a
       node has no ``FanoutEdge`` to key on.
    """
    fanout_edges_by_source: dict[str, list[FanoutEdge]] = {}
    seq_edges_by_source: dict[str, list[SeqEdge]] = {}
    for edge in graph.edges:
        if isinstance(edge, FanoutEdge):
            fanout_edges_by_source.setdefault(edge.source, []).append(edge)
        elif isinstance(edge, SeqEdge):
            seq_edges_by_source.setdefault(edge.source, []).append(edge)

    all_fanout_source_ids = sorted(
        set(fanout_edges_by_source) | set(structure.fanout_source_node_ids)
    )

    for node_id in all_fanout_source_ids:
        fanout_edges = fanout_edges_by_source.get(node_id, [])
        seq_edges = seq_edges_by_source.get(node_id, [])
        total_structural = len(structure.structural_successors.get(node_id, []))

        if seq_edges or len(fanout_edges) != total_structural:
            findings.error(
                "fanout_without_join",
                f"node {node_id!r} has {total_structural} structural successors "
                "that are not all FanoutEdge arms converging on one join node "
                "(a plain multi-successor fan-out silently drops all "
                "but one branch's output)",
                (node_id,),
            )
            continue

        join_ids = {e.join_node_id for e in fanout_edges}
        if len(join_ids) != 1:
            findings.error(
                "fanout_without_join",
                f"node {node_id!r}'s fan-out arms disagree on join_node_id: {join_ids!r}",
                (node_id,),
            )
            continue

        join_id = next(iter(join_ids))
        join_node = node_by_id.get(join_id)
        if join_node is None or join_node.kind != "join" or join_node.join is None:
            findings.error(
                "fanout_without_join",
                f"node {node_id!r}'s declared join_node_id {join_id!r} is not a "
                "real kind='join' node",
                (node_id, join_id),
            )
            continue

        # Declaring the join is not enough: without a JoinEdge from each
        # arm target into it, the emitted graph has arm steps that feed
        # nothing, so the join receives fewer values than it has arms.
        for fanout_edge in fanout_edges:
            arm_target = fanout_edge.target
            has_join_edge = any(
                isinstance(edge, JoinEdge)
                and edge.source == arm_target
                and edge.target == join_id
                for edge in graph.edges
            )
            if not has_join_edge:
                findings.error(
                    "fanout_arm_missing_join_edge",
                    f"fan-out arm {arm_target!r} (from {node_id!r}) has no "
                    f"JoinEdge into declared join node {join_id!r}",
                    (node_id, arm_target, join_id),
                )


# ---------------------------------------------------------------------------
# Decision / branch
# ---------------------------------------------------------------------------


def _check_decision_branches(
    graph: SwarmGraph,
    structure: GraphStructure,
    node_by_id: dict[str, SwarmNode],
    findings: _FindingCollector,
) -> None:
    """Validate a decision node's branch table against its edges.

    Four things must hold, and each fails at run time rather than at
    build time if it does not: the decision must declare at least one
    branch *and* have at least one ``BranchEdge`` (otherwise the emitted
    graph raises ``RuntimeError: No branch matched inputs``); the
    declared ``(match, target_node_id)`` pairs must equal the drawn
    ``(match, target)`` edge pairs; the decision must have exactly one
    inbound structural edge, so that "the source step" is unambiguous; and
    every branch target's ``input_type`` must equal that source step's
    ``output_type``.

    That last rule is the one that surprises: data does not pass *through*
    a decision node. A branch target receives, as ``ctx.inputs``, the
    match value returned by the decision's source step -- so if the source
    returns ``"big"``, the target observes ``"big"``, never the original
    pre-classification payload. A workflow needing that payload inside the
    branch target has to carry it through ``State`` explicitly.
    """
    branch_edges_by_source: dict[str, list[BranchEdge]] = {}
    for edge in graph.edges:
        if isinstance(edge, BranchEdge):
            branch_edges_by_source.setdefault(edge.source, []).append(edge)

    for node in graph.nodes:
        if node.kind != "decision" or node.decision is None:
            continue

        spec_branches = node.decision.branches
        edge_branches = branch_edges_by_source.get(node.id, [])

        if not spec_branches or not edge_branches:
            findings.error(
                "branchless_decision",
                f"decision node {node.id!r} has zero branches "
                "(it builds and fails at run time with "
                "'RuntimeError: No branch matched inputs')",
                (node.id,),
            )
            continue

        spec_pairs = {(b.match, b.target_node_id) for b in spec_branches}
        edge_pairs = {(e.match, e.target) for e in edge_branches}
        if spec_pairs != edge_pairs:
            findings.error(
                "decision_branch_mismatch",
                f"decision node {node.id!r}: DecisionSpec.branches "
                f"{sorted(spec_pairs)!r} does not match BranchEdge set "
                f"{sorted(edge_pairs)!r}",
                (node.id,),
            )
            continue

        # A branch target's input_type must equal the decision's SOURCE
        # STEP's output_type, not any node further upstream: the match
        # value the source returns is what a branch target receives as
        # ctx.inputs. That requires exactly one inbound structural edge --
        # with more than one, "the source step" is ambiguous and the rule
        # cannot even be stated.
        source_ids = structure.structural_predecessors.get(node.id, [])
        if len(source_ids) == 0:
            findings.error(
                "decision_no_source",
                f"decision node {node.id!r} has no inbound structural edge "
                "(nothing feeds it a value to classify)",
                (node.id,),
            )
            continue
        if len(source_ids) > 1:
            findings.error(
                "decision_multiple_sources",
                f"decision node {node.id!r} has {len(source_ids)} inbound "
                f"structural edges {source_ids!r} -- its branch targets' input "
                "types are defined against exactly one source step",
                (node.id, *source_ids),
            )
            continue
        source_id = source_ids[0]

        source_output_type = node_by_id[source_id].io.output_type
        for branch in spec_branches:
            target = node_by_id.get(branch.target_node_id)
            if target is None:
                continue
            if target.io.input_type != source_output_type:
                findings.error(
                    "decision_branch_type_mismatch",
                    f"branch target {branch.target_node_id!r}'s input_type "
                    f"({target.io.input_type!r}) must equal decision "
                    f"{node.id!r}'s source step {source_id!r}'s output_type "
                    f"({source_output_type!r}) -- data does not pass through "
                    "a decision node; a branch target receives the source "
                    "step's match value as ctx.inputs",
                    (node.id, source_id, branch.target_node_id),
                )


# ---------------------------------------------------------------------------
# Port types
# ---------------------------------------------------------------------------


def _check_port_types(
    graph: SwarmGraph, node_by_id: dict[str, SwarmNode], findings: _FindingCollector
) -> None:
    """Require matching port types across every edge that carries data.

    For a ``SeqEdge``, ``FanoutEdge`` or ``JoinEdge`` -- the kinds emitted
    as a real ``add_edge`` -- the source's ``output_type`` must equal the
    target's ``input_type``, or the generated step function is called with
    a value it does not accept.

    ``BranchEdge`` is exempt because it has its own, stricter rule (a
    branch target's type is defined against the decision's source step,
    not against the decision node). ``DelegateEdge`` is exempt because a
    delegate is called as a tool, never as a graph step, so no value flows
    along it at all.
    """
    for edge in graph.edges:
        if not isinstance(edge, (SeqEdge, FanoutEdge, JoinEdge)):
            continue
        source = node_by_id.get(edge.source)
        target = node_by_id.get(edge.target)
        if source is None or target is None:
            continue
        if source.io.output_type != target.io.input_type:
            findings.error(
                "port_type_mismatch",
                f"edge {edge.id!r} ({edge.kind}): source {edge.source!r} "
                f"output_type {source.io.output_type!r} != target "
                f"{edge.target!r} input_type {target.io.input_type!r}",
                (edge.source, edge.target),
            )


def _check_sink_output_consistency(
    graph: SwarmGraph,
    structure: GraphStructure,
    node_by_id: dict[str, SwarmNode],
    findings: _FindingCollector,
) -> None:
    """Require every structural sink to agree with the exit node's type.

    A sink (a node with no structural successor) is wired to the end node
    explicitly, and the built graph carries exactly one output type, so a
    sink whose ``output_type`` differs from ``exit_node_id``'s would make
    the graph's declared output type depend on which sink happened to run.
    """
    exit_node = node_by_id.get(graph.exit_node_id)
    if exit_node is None:
        return
    expected = exit_node.io.output_type
    for node_id in sorted(structure.structural_sink_node_ids):
        actual = node_by_id[node_id].io.output_type
        if actual != expected:
            findings.error(
                "sink_output_type_mismatch",
                f"sink node {node_id!r} output_type {actual!r} != exit node "
                f"{graph.exit_node_id!r} output_type {expected!r}",
                (node_id, graph.exit_node_id),
            )


# ---------------------------------------------------------------------------
# State ownership (I2)
# ---------------------------------------------------------------------------


def _check_state_ownership(
    graph: SwarmGraph, structure: GraphStructure, findings: _FindingCollector
) -> None:
    """Enforce the three rules that make shared state safe.

    ``State`` is one mutable object threaded through the entire run, so it
    is the only channel by which data can reach a step that ``ctx.inputs``
    cannot carry (notably across a decision). Three rules keep it sound:

    - a node may only write a state field the document declares;
    - a field may have only one writer, so the value a reader sees does not
      depend on which writer ran;
    - a node that reads a field must have that field's writer somewhere
      upstream of it on the *dispatch* graph (structural plus branch edges,
      never delegate) -- a read with no upstream write observes the
      dataclass default, which is a silent wrong answer rather than an
      error.
    """
    declared_fields = {f.name for f in graph.state_fields}

    writers_by_field: dict[str, list[str]] = {}
    for node in graph.nodes:
        for field_name in node.writes:
            writers_by_field.setdefault(field_name, []).append(node.id)
            if field_name not in declared_fields:
                findings.error(
                    "undeclared_state_write",
                    f"node {node.id!r} writes undeclared state field {field_name!r}",
                    (node.id,),
                )

    for field_name, writer_ids in writers_by_field.items():
        if len(writer_ids) > 1:
            findings.error(
                "duplicate_state_writer",
                f"state field {field_name!r} is written by multiple nodes: {writer_ids!r}",
                tuple(writer_ids),
            )

    # Ancestors are computed over the dispatch graph, not the structural
    # one: State survives a decision hop even though ctx.inputs does not,
    # so a writer upstream of a decision *is* upstream of that decision's
    # branch targets.
    ancestors: dict[str, set[str]] = {node.id: set() for node in graph.nodes}
    for node in graph.nodes:
        stack = list(structure.dispatch_predecessors.get(node.id, []))
        seen: set[str] = set()
        while stack:
            predecessor = stack.pop()
            if predecessor in seen:
                continue
            seen.add(predecessor)
            stack.extend(structure.dispatch_predecessors.get(predecessor, []))
        ancestors[node.id] = seen

    for node in graph.nodes:
        for field_name in node.reads:
            writer_ids = writers_by_field.get(field_name, [])
            if not any(writer in ancestors[node.id] for writer in writer_ids):
                findings.error(
                    "unwritten_state_read",
                    f"node {node.id!r} reads state field {field_name!r} with "
                    "no upstream writer",
                    (node.id,),
                )


# ---------------------------------------------------------------------------
# Database nodes (kinds sql / nosql / vector)
#
# Phase 1 is the only defence for these. A database node's operation and seed
# are *declared data*: the emitter renders them verbatim into a generated
# project and nothing downstream re-derives them, so a defect that gets past
# this section reaches that project as a run-time failure -- an unbound
# `:placeholder`, an empty query, a seed that does not parse, a write reachable
# from a model's tool call. Every rule below is decided from the document alone,
# except `db_starter_drift`, which reads the kind's `starter.json` through a
# lazy import inside the check and is skipped entirely when the catalog cannot
# be imported: `review()` must not gain an import-time dependency on a catalog
# it consults for one warning.
# ---------------------------------------------------------------------------


def _db_spec(node: SwarmNode) -> SqlSpec | NosqlSpec | VectorSpec | None:
    """The one spec a database-kind node carries, or ``None`` when it is absent.

    ``SwarmNode`` cannot express "kind=sql implies a sql spec" as a field rule,
    so a document can carry a database kind with no spec at all. Every rule in
    this section therefore tolerates the absence: ``db_empty_operation`` reports
    it once, and the rest skip the node rather than raising ``AttributeError``
    out of ``review()``.
    """
    if node.kind == "sql":
        return node.sql
    if node.kind == "nosql":
        return node.nosql
    if node.kind == "vector":
        return node.vector
    return None


def _check_db_operations(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Require every database node to declare a non-empty operation and seed.

    A database node's body is emitted from its declared operation, never written
    by a model, so an empty one cannot be filled in later: the emitted step would
    hand the repository an empty statement, an unnamed collection or no seed to
    load, and fail at run time in a project that already compiled. An empty value
    means the node was never materialized from its kind's starter, or was emptied
    by hand -- the case the plan calls out for a handwritten ``sql: null`` node.
    """
    for node in graph.nodes:
        if node.kind not in _DATABASE_KINDS:
            continue
        spec = _db_spec(node)
        if spec is None:
            findings.error(
                "db_empty_operation",
                f"{node.kind} node {node.id!r} carries no {node.kind!r} spec, so it "
                "declares no operation at all (the Inspector materializes one from "
                "the kind's starter when the node is created)",
                (node.id,),
            )
            continue
        for field_name, why in _EMPTY_OPERATION_FIELDS[node.kind]:
            if str(getattr(spec, field_name, None) or "").strip():
                continue
            findings.error(
                "db_empty_operation",
                f"{node.kind} node {node.id!r} declares an empty {field_name!r} "
                f"({why}); a database node's operation is declared data, so nothing "
                "downstream can fill it in -- the Inspector copies it from the kind's "
                "starter when the node is created",
                (node.id,),
            )


def _check_db_template_set(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Reject a ``template`` on a database node.

    Templates are agent-only: a template selects the model-authored agent module
    and its instructions, and the Inspector hides the field for the three
    database kinds. The emitter ignores a database node's ``template``
    entirely, so a value here would be silently dropped -- and the only way one
    appears is a hand edit or a document whose node changed kind, which is
    exactly the case a silent ignore would hide forever.
    """
    for node in graph.nodes:
        if node.kind not in _DATABASE_KINDS or node.template is None:
            continue
        findings.error(
            "db_template_set",
            f"{node.kind} node {node.id!r} also carries template "
            f"{node.template!r}; templates are agent-only, so this value is never "
            "emitted (clear the field, or make the node an agent node)",
            (node.id,),
        )


def _check_db_operation_io(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Require every database node to declare its kind's mandatory I/O pair.

    A database node's ports are not a choice: the emitted step returns rows
    (``list[json]``) and takes the kind's bindable input type, and the Inspector
    copies the pair from the starter. Anything else either makes the step's
    return value lossy on the next edge or declares an input the binding rule
    cannot supply -- so the pair is checked as a pair, not as two ports.
    """
    for node in graph.nodes:
        if node.kind not in _DATABASE_KINDS:
            continue
        allowed_inputs = _BINDABLE_INPUT_TYPES[node.kind]
        if node.io.input_type in allowed_inputs and node.io.output_type == _DATABASE_OUTPUT_TYPE:
            continue
        findings.error(
            "db_op_io_mismatch",
            f"{node.kind} node {node.id!r} declares io {node.io.input_type!r} -> "
            f"{node.io.output_type!r}; a {node.kind} node's pair is mandatory: "
            f"{_INPUT_TYPE_RULE[node.kind]} -> 'list[json]' (the step passes rows on, "
            "never one document)",
            (node.id,),
        )


def _check_db_input_type_binding(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Reject an input port type the node's kind cannot bind.

    The upstream value is a database node's only parameter source, and §4.3
    gives three shapes a binding rule: one ``str``, one ``json`` dict, and (for
    SQL only) a ``list[str]`` expanded into ``:input_0, :input_1, …``. An input
    type outside that set has no rule, so it cannot be honoured at run time
    (SQL/NoSQL would raise on an unbound parameter; a vector node would quietly
    search for nothing). This rule therefore overlaps ``db_op_io_mismatch`` on
    exactly the rejected cells -- both statements are true of such a node, and
    each names the rule it breaks -- while ``db_op_io_mismatch`` also covers the
    pairs built from a bindable type with the wrong port.
    """
    for node in graph.nodes:
        if node.kind not in _DATABASE_KINDS:
            continue
        if node.io.input_type in _BINDABLE_INPUT_TYPES[node.kind]:
            continue
        findings.error(
            "db_input_type_unsupported",
            f"{node.kind} node {node.id!r} declares input_type "
            f"{node.io.input_type!r}, which a {node.kind} node cannot bind: expected "
            f"{_INPUT_TYPE_RULE[node.kind]}",
            (node.id,),
        )


def _check_db_placeholders(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Require a SQL node's ``:name`` placeholders to match its declared input.

    §4.3 fixes one binding rule per input type, and a placeholder nothing
    supplies is not a modelling detail: sqlite3 -- and every live driver -- raises
    on the first row requested, inside the generated project. The rules are: a
    ``str`` input binds exactly one parameter, named ``:input``; a ``json``
    input's keys bind directly, so the query needs at least one placeholder; and
    a ``list[str]`` input expands a *single* ``:input`` placeholder into
    ``:input_0, :input_1, …``.

    Placeholders are parsed with a regex that ignores ``::type`` casts, because a
    cast's second colon is not a placeholder: without that, ``total::numeric``
    would be read as a parameter named ``numeric`` and reported as a mismatch on
    a perfectly bindable query.
    """
    for node in graph.nodes:
        if node.kind != "sql" or node.sql is None:
            continue
        query = str(node.sql.query or "")
        if not query.strip():
            # An empty query is already reported once, by db_empty_operation.
            continue
        input_type = node.io.input_type
        placeholders = _SQL_PLACEHOLDER_RE.findall(query)
        if input_type == "str":
            if set(placeholders) == {"input"}:
                continue
            findings.error(
                "db_placeholder_mismatch",
                f"SQL node {node.id!r} declares input_type 'str', which binds exactly "
                "one parameter named ':input', but its query's named placeholders are "
                f"{sorted(set(placeholders))!r} (':name' placeholders are parsed "
                "ignoring '::' casts, and one nothing binds fails at run time)",
                (node.id,),
            )
        elif input_type == "json":
            if placeholders:
                continue
            findings.error(
                "db_placeholder_mismatch",
                f"SQL node {node.id!r} declares input_type 'json', whose keys bind "
                "directly (as ':key', plus ':input' for the whole dict), but its query "
                "has no ':name' placeholder to bind anything to",
                (node.id,),
            )
        elif input_type == "list[str]":
            if placeholders == ["input"]:
                continue
            findings.error(
                "db_placeholder_mismatch",
                f"SQL node {node.id!r} declares input_type 'list[str]', which expands a "
                "single ':input' placeholder into ':input_0, :input_1, …', but its "
                f"query's named placeholders are {placeholders!r}",
                (node.id,),
            )
        # Any other input type (only 'list[json]') has no binding rule at all, so
        # there is no placeholder rule to check against it: db_input_type_unsupported
        # reports it.


def _check_db_seed_validity(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Require every declared seed to parse for its kind.

    The seed *is* the mock: the emitter copies it into the generated project and
    the mock loads it before the first read, so a seed that does not parse fails
    there, at run time, on the first step that touches the node -- long after
    Phase 1 could have said so. A SQL seed is probed by running it through an
    in-memory SQLite database (stdlib only, no server, no file, nothing left
    behind); a NoSQL or vector seed has to be a list of JSON objects. An *empty*
    list stays legal: it is a node whose mock holds no documents yet, which is a
    valid state rather than an unparseable one.
    """
    for node in graph.nodes:
        if node.kind not in _DATABASE_KINDS:
            continue
        spec = _db_spec(node)
        if spec is None:
            continue
        problem: str | None = None
        if isinstance(spec, SqlSpec):
            if not str(spec.seed_sql or "").strip():
                # Already reported once, by db_empty_operation.
                continue
            problem = _sql_seed_problem(spec.seed_sql)
        elif isinstance(spec, NosqlSpec):
            problem = _document_seed_problem(spec.seed, requires_text=False)
        elif isinstance(spec, VectorSpec):
            problem = _document_seed_problem(spec.seed, requires_text=True)
        if problem is None:
            continue
        findings.error(
            "db_seed_invalid",
            f"{node.kind} node {node.id!r} has a seed that does not parse ({problem}); "
            "the seed is the mock's schema and rows, so every step on this node would "
            "fail in the generated project",
            (node.id,),
        )


def _sql_seed_problem(seed_sql: str) -> str | None:
    """Run a SQL seed through an in-memory SQLite probe; ``None`` when it runs.

    ``sqlite3`` is the mock's own engine, so probing with it is the only faithful
    test of "this seed will load" that needs no server and no driver. The
    connection is created, used and closed inside this function and never
    escapes: a leak would keep a database alive for the life of the process for
    a check that is otherwise pure.

    ``ValueError`` is caught alongside ``sqlite3.Error`` because that is what
    ``sqlite3`` raises for a statement containing a NUL character -- a defect a
    hand-edited seed can contain, and one this check has to report rather than
    propagate as a crash out of ``review()``.
    """
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(seed_sql)
    except (sqlite3.Error, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()
    return None


def _document_seed_problem(seed: object, *, requires_text: bool) -> str | None:
    """Why a document or vector seed is not a JSON array of objects, or ``None``.

    ``requires_text`` is the vector node's extra requirement: a vector document
    is only searchable with a non-empty ``id`` (which the mock returns, and which
    breaks score ties) and a non-empty ``text`` (which the embedder hashes), so an
    empty one is a document the mock can never return.
    """
    if not isinstance(seed, (list, tuple)):
        return f"the seed is {type(seed).__name__}, not a JSON array of documents"
    for index, entry in enumerate(seed):
        document = _seed_document(entry)
        if document is None:
            return f"seed entry {index} is {type(entry).__name__}, not a JSON object"
        if not requires_text:
            continue
        for field_name in ("id", "text"):
            if not str(document.get(field_name) or "").strip():
                return f"seed entry {index} has no non-empty {field_name!r}"
    return None


def _seed_document(entry: object) -> dict[str, object] | None:
    """One seed entry as a plain dict, or ``None`` when it is not an object.

    Two shapes reach here: a NoSQL seed is declared ``list[dict]``, and a vector
    seed is a list of :class:`VectorDocument` models. Both become a JSON object
    once rendered, which is what the mock loads and what this check is about.
    """
    if isinstance(entry, Mapping):
        return dict(entry)
    if isinstance(entry, VectorDocument):
        return entry.model_dump()
    return None


def _db_tool_references(
    graph: SwarmGraph, node_by_id: dict[str, SwarmNode]
) -> list[_DbToolReference]:
    """Every ``<kind>:<node_id>`` entry in any agent's ``tools`` list, resolved.

    ``tools`` is a free ``list[str]`` that also carries catalog names such as
    ``web_search``, so an entry is only treated as a database reference when its
    namespace before the first colon is one of the three database kinds. A
    trailing colon (``"sql:"``) is kept rather than dropped: it is a malformed
    reference, which is a finding, not an entry to ignore.
    """
    references: list[_DbToolReference] = []
    for node in graph.nodes:
        if node.agent is None:
            continue
        for entry in node.agent.tools:
            kind, separator, target_id = entry.partition(":")
            if not separator or kind not in _DATABASE_KINDS:
                continue
            references.append(
                _DbToolReference(
                    agent_id=node.id,
                    entry=entry,
                    kind=kind,
                    target_id=target_id,
                    target=node_by_id.get(target_id),
                )
            )
    return references


def _check_db_tool_references(
    graph: SwarmGraph, node_by_id: dict[str, SwarmNode], findings: _FindingCollector
) -> None:
    """Require every database tool entry to name a node of that kind.

    An agent attaches a database node as a read-only tool by naming it
    ``<kind>:<node_id>``, and the emitter builds that tool from the *named
    node's* declared operation. So a bad reference is a generated project whose
    agent has no tool for a tool its prompt promises -- or a tool built from a
    different node than the document says. A malformed entry (``"sql:"`` with no
    id) is reported here too, since it names nothing and would otherwise be
    silently ignored.
    """
    for reference in _db_tool_references(graph, node_by_id):
        if not reference.target_id:
            findings.error(
                "db_tool_unknown_node",
                f"agent node {reference.agent_id!r} lists {reference.entry!r} in "
                "tools, but a database tool entry must be '<kind>:<node_id>' and this "
                "one names no node",
                (reference.agent_id,),
            )
            continue
        if reference.target is None:
            findings.error(
                "db_tool_unknown_node",
                f"agent node {reference.agent_id!r} lists {reference.entry!r} in "
                f"tools, but no node {reference.target_id!r} exists in this graph",
                (reference.agent_id, reference.target_id),
            )
            continue
        if reference.target.kind != reference.kind:
            findings.error(
                "db_tool_unknown_node",
                f"agent node {reference.agent_id!r} lists {reference.entry!r} in "
                f"tools, but node {reference.target_id!r} has kind "
                f"{reference.target.kind!r}, not {reference.kind!r}",
                (reference.agent_id, reference.target_id),
            )


def _check_db_tool_writes(
    graph: SwarmGraph, node_by_id: dict[str, SwarmNode], findings: _FindingCollector
) -> None:
    """Reject an agent tool that names a database node in write mode.

    A database node in write mode emits an ``execute()`` call rather than a read,
    and a write reachable from a model's tool call is the injection surface this
    design refuses to create -- there is deliberately no free-form SQL tool in
    v1. So the combination is an error rather than a silent downgrade to a
    read-only tool: a document that asks for it has to be changed, and a
    downgrade would hide a graph that does not do what it says.

    References that do not resolve are skipped: ``db_tool_unknown_node`` already
    reports them.
    """
    for reference in _db_tool_references(graph, node_by_id):
        target = reference.target
        if target is None or target.kind != reference.kind:
            continue
        write_detail: str | None = None
        if target.sql is not None and target.sql.write:
            write_detail = "sql.write is true"
        elif target.nosql is not None and target.nosql.operation == "insert_one":
            write_detail = "nosql.operation is 'insert_one'"
        if write_detail is None:
            continue
        findings.error(
            "db_write_as_tool",
            f"agent node {reference.agent_id!r} lists {reference.entry!r} in tools, "
            f"but node {reference.target_id!r} is a write operation ({write_detail}); "
            "a write reachable from a model's tool call is the injection surface this "
            "design refuses to create, so an agent may attach a database node's read "
            "operation only",
            (reference.agent_id, reference.target_id),
        )


def _check_db_limit_warnings(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Warn about a NoSQL node whose declared limit reads as "no limit".

    ``limit <= 0`` is documented as unbounded, so the step returns every matching
    document -- legal, and occasionally deliberate, but far more often a
    half-finished edit than a choice. It is a warning for that reason: the graph
    still compiles and runs.
    """
    for node in graph.nodes:
        spec = node.nosql
        if node.kind != "nosql" or spec is None:
            continue
        if spec.limit > 0:
            continue
        findings.warn(
            "db_limit_unset",
            f"NoSQL node {node.id!r} declares limit {spec.limit}, which the repository "
            "reads as 'no limit': the step would return every matching document",
            (node.id,),
        )


def _check_db_starter_drift(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Warn when a database node's operation or seed differs from its starter.

    A database node is created from a starter template whose operation and seed
    are consistent by construction, and the Inspector offers "Reset to example".
    A drifted node is therefore either a deliberate edit or a stale one, and the
    warning is what tells the two apart in a compiled project whose mock returns
    something other than the example data.

    The catalog is imported *inside* this check and the whole check is skipped
    when that import fails, so ``review()`` never gains an import-time dependency
    on ``templates/database`` -- the same degradation seam the API routes use.
    A catalog that cannot be read is not a graph defect, so this check reports
    nothing rather than raising. A document with no database node never imports
    it at all, so review is unchanged for every graph that has none.
    """
    declared = [
        (node, spec)
        for node in graph.nodes
        if node.kind in _DATABASE_KINDS and (spec := _db_spec(node)) is not None
    ]
    if not declared:
        return
    try:
        from swarm_builder.templates.database import get_database_entry
    except Exception:
        return
    for node, spec in declared:
        try:
            starter_spec = get_database_entry(node.kind).starter_spec
        except Exception:
            return
        drifted = [
            field_name
            for field_name in _DRIFT_FIELDS[node.kind]
            if getattr(spec, field_name, None) != getattr(starter_spec, field_name, None)
        ]
        if not drifted:
            continue
        findings.warn(
            "db_starter_drift",
            f"{node.kind} node {node.id!r} differs from the {node.kind} starter in "
            f"{drifted!r}; the starter is the example this node was created from, so "
            "the mock will not contain the example data (the Inspector's 'Reset to "
            "example' restores it)",
            (node.id,),
        )


def _check_db_seed_sharing(graph: SwarmGraph, findings: _FindingCollector) -> None:
    """Warn when two nodes of one kind declare different seeds.

    The generated factory keys one mock instance per ``(kind, seed)``, so two
    nodes over the same seed share one database -- a write in the first step is
    visible to the second -- while two nodes over different seeds get independent
    instances and neither node's seed is silently ignored. That is correct, and
    it is also invisible: a workflow that expected one shared database gets two,
    with a write in one step missing from the other's reads. Hence a warning, not
    an error: the graph builds and runs either way.
    """
    seeds_by_kind: dict[str, dict[str, list[str]]] = {}
    for node in graph.nodes:
        if node.kind not in _DATABASE_KINDS:
            continue
        spec = _db_spec(node)
        if spec is None:
            continue
        key = _seed_key(node, spec)
        seeds_by_kind.setdefault(node.kind, {}).setdefault(key, []).append(node.id)
    for kind in sorted(seeds_by_kind):
        by_seed = seeds_by_kind[kind]
        if len(by_seed) < 2:
            continue
        node_ids = tuple(sorted(node_id for ids in by_seed.values() for node_id in ids))
        findings.warn(
            "db_separate_mock_instances",
            f"this graph has {len(node_ids)} {kind} nodes over {len(by_seed)} different "
            f"seeds {list(node_ids)!r}: each seed gets its own mock instance, so a "
            "write in one step is not visible to the others (give them the same seed "
            "to share one)",
            node_ids,
        )


def _seed_key(node: SwarmNode, spec: SqlSpec | NosqlSpec | VectorSpec) -> str:
    """The seed text the generated factory keys a mock instance by.

    **Delegates to the renderer** (:func:`~swarm_builder.compile.database.database_seed_text`)
    instead of normalizing the spec fields here. A second copy of "what the seed
    file will contain" is exactly the kind of table that drifts: the factory keys
    the mock by a digest of the *rendered* text, so a check that predicted it
    differently would warn about two nodes that in fact share one database, or
    stay silent about two that do not. ``database_seed_text`` returns ``None``
    for a node with no spec, which ``_db_spec`` has already filtered out here.

    A seed that is not a list at all can only come from a document built by hand
    rather than validated; it is reported by ``db_seed_invalid``, and all this
    function has to do with it is produce a key that collides with no real
    seed's.
    """
    # Imported here, not at module level, for the same reason the catalog read
    # below is: ``compile.database`` imports ``templates.database``, so a
    # module-level import would make every ``review()`` call -- including one for
    # a graph with no database node at all -- pull the catalog in, which is the
    # import-time dependency this module deliberately does not have.
    from swarm_builder.compile.database import database_seed_text

    rendered = database_seed_text(node)
    if rendered is not None:
        return rendered
    seed = spec.seed
    if not isinstance(seed, (list, tuple)):
        return json.dumps(seed, default=repr, sort_keys=True)
    documents = [_seed_document(document) for document in seed]
    return json.dumps(documents, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Soft findings -> warnings
# ---------------------------------------------------------------------------


def _check_delegate_targets_are_agents(
    graph: SwarmGraph, node_by_id: dict[str, SwarmNode], findings: _FindingCollector
) -> None:
    """A delegate target must be an existing ``agent`` node.

    Delegation is compiled as a **tool**: an orchestrator's factory emits, for
    every entry in ``agent.delegates_to``, an import of
    ``.../agents/<child>.py`` and a call to its ``build_agent``. Only ``agent``
    nodes get an ``agents/`` module (``scaffold.py`` writes one for
    ``kind == "agent"`` alone), so a ``delegates_to`` entry naming a database,
    decision, join or ``programmatic`` node -- or naming nothing at all -- emits a
    module that imports a file no emitter wrote, and the generated project fails
    at its keyless import with ``ModuleNotFoundError``. Nothing else catches it:
    ``_check_delegates_to_warnings`` only notices a *missing edge*, and a
    ``delegate`` edge to a non-agent node is drawn happily by the canvas.

    This is an error rather than a warning for the same reason a missing marker is
    a hard failure: the emitted project cannot import, so there is no degraded
    build to fall back on.
    """
    for node in graph.nodes:
        if node.agent is None:
            continue
        for child_id in node.agent.delegates_to:
            child = node_by_id.get(child_id)
            if child is None:
                findings.error(
                    "delegate_target_not_agent",
                    f"node {node.id!r} delegates to {child_id!r}, which is not a node in this "
                    "graph: a delegate target is called as an agent tool, so it must name an "
                    "existing 'agent' node",
                    (node.id,),
                )
            elif child.kind != "agent":
                findings.error(
                    "delegate_target_not_agent",
                    f"node {node.id!r} delegates to {child_id!r}, which is a {child.kind!r} node: "
                    "a delegate target is called as an agent tool and only 'agent' nodes get an "
                    "agents/ module, so this would emit an import of a file that does not exist",
                    (node.id, child.id),
                )
    for edge in graph.edges:
        if not isinstance(edge, DelegateEdge):
            continue
        target = node_by_id.get(edge.target)
        if target is not None and target.kind != "agent":
            findings.error(
                "delegate_target_not_agent",
                f"delegate edge {edge.id!r} targets {edge.target!r}, which is a "
                f"{target.kind!r} node: delegation is compiled as an agent tool, so the target "
                "must be an 'agent' node",
                (edge.source, edge.target),
            )


def _check_delegates_to_warnings(
    graph: SwarmGraph, node_by_id: dict[str, SwarmNode], findings: _FindingCollector
) -> None:
    """Warn when ``delegates_to`` and the drawn delegate edges disagree.

    The canvas can express delegation in two places (the agent spec's
    ``delegates_to`` list and a ``DelegateEdge``), and only the edges are
    emitted, so a name listed without a matching edge is silently dropped
    rather than being an error the user must fix.
    """
    delegate_edge_pairs = {
        (edge.source, edge.target) for edge in graph.edges if isinstance(edge, DelegateEdge)
    }
    for node in graph.nodes:
        if node.agent is None:
            continue
        for child_id in node.agent.delegates_to:
            if (node.id, child_id) not in delegate_edge_pairs:
                findings.warn(
                    "delegate_without_edge",
                    f"node {node.id!r} lists {child_id!r} in delegates_to but "
                    "has no matching DelegateEdge",
                    (node.id, child_id),
                )


def _check_join_shape_warnings(
    graph: SwarmGraph,
    structure: GraphStructure,
    node_by_id: dict[str, SwarmNode],
    findings: _FindingCollector,
) -> None:
    """Warn about a join whose shape is legal but probably unintended.

    Both findings are warnings, not errors: a list reducer whose output
    type is not ``list[str]``, and a join with fewer than two inbound
    arms, both build and run. They are surfaced because they are far more
    often a modeling slip than a deliberate choice.
    """
    for node in graph.nodes:
        if node.kind != "join" or node.join is None:
            continue
        reducer = node.join.reducer
        if reducer in _LIST_REDUCERS and node.io.output_type != _LIST_REDUCER_OUTPUT_TYPE:
            findings.warn(
                "join_output_type_unusual",
                f"join node {node.id!r} uses reducer {reducer!r} but "
                f"output_type is {node.io.output_type!r}, not "
                f"{_LIST_REDUCER_OUTPUT_TYPE!r}",
                (node.id,),
            )
        inbound = len(structure.structural_predecessors.get(node.id, []))
        if inbound < _MIN_JOIN_ARMS:
            findings.warn(
                "join_with_few_inputs",
                f"join node {node.id!r} has only {inbound} inbound structural "
                f"edge(s) -- a join of fewer than {_MIN_JOIN_ARMS} arms is unusual",
                (node.id,),
            )


__all__ = ["Finding", "ReviewResult", "review"]
