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

import keyword
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
    SeqEdge,
    SwarmGraph,
    SwarmNode,
)
from swarm_builder.slugify import IDENTIFIER_RE

#: Node kind -> the name of the single spec field that kind requires.
#: Used to assert "exactly one spec, and the right one" per node.
_KIND_TO_SPEC_ATTR: dict[str, str] = {
    "agent": "agent",
    "programmatic": "programmatic",
    "decision": "decision",
    "join": "join",
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

    structure = analyze(graph)

    _check_reachability(graph, structure, findings)
    _check_cycles(graph, structure, findings)
    _check_fanout_without_join(graph, structure, node_by_id, findings)
    _check_decision_branches(graph, structure, node_by_id, findings)
    _check_port_types(graph, node_by_id, findings)
    _check_sink_output_consistency(graph, structure, node_by_id, findings)
    _check_state_ownership(graph, structure, findings)
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
# Soft findings -> warnings
# ---------------------------------------------------------------------------


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
