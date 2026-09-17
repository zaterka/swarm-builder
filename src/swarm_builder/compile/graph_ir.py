"""Shared structural-graph analysis over a :class:`SwarmGraph`.

``emit_graph.py`` (codegen), ``scaffold.py`` and ``review.py``
(Phase-1 validation) all need the *same* answers to "what are this node's
structural successors", "which nodes are delegate-only", and "which nodes
will get a synthetic broadcast-fork node". Computing those independently
in each consumer is a two-copy drift risk: the emitter and the validator
would disagree about the very graph shape they are both reasoning about.
This module is the one place that computes them.

**Structural edges** are the edges that become a literal ``add_edge`` call
in the generated ``graph.py``: ``SeqEdge``, ``FanoutEdge`` and
``JoinEdge``. A ``BranchEdge`` wires via ``Decision.branch(...)`` instead,
and a ``DelegateEdge`` wires via nothing at all -- so neither is
structural in this sense. The *dispatch* sets are deliberately wider than
the structural ones; see :class:`GraphStructure`.
"""

from __future__ import annotations

from dataclasses import dataclass

from swarm_builder.models import (
    BranchEdge,
    DelegateEdge,
    FanoutEdge,
    JoinEdge,
    SeqEdge,
    SwarmEdge,
    SwarmGraph,
    SwarmNode,
)

#: Edge kinds emitted as a literal ``add_edge(source, target)``.
STRUCTURAL_EDGE_KINDS: tuple[str, ...] = ("seq", "fanout", "join")

#: Edge kinds that participate in data *flow* between steps, whether or not
#: they become an ``add_edge``. Used for reachability and cycle detection.
_DISPATCH_EDGE_KINDS = (SeqEdge, FanoutEdge, JoinEdge, BranchEdge)

#: A fan-out exists when a node has at least this many structural
#: successors; ``build()`` then injects a synthetic broadcast node.
_FANOUT_MIN_SUCCESSORS = 2


def is_structural(edge: SwarmEdge) -> bool:
    """True for an edge kind that becomes a literal ``add_edge`` call."""
    return edge.kind in STRUCTURAL_EDGE_KINDS


@dataclass(frozen=True)
class GraphStructure:
    """Precomputed structural facts about a :class:`SwarmGraph`.

    Frozen because it is shared by three consumers that must all see the
    same analysis of the same document.
    """

    graph: SwarmGraph
    node_by_id: dict[str, SwarmNode]
    #: Structural successors only (seq/fanout/join edges), in edge order.
    structural_successors: dict[str, list[str]]
    #: Structural predecessors only, in edge order.
    structural_predecessors: dict[str, list[str]]
    #: Dispatch successors: structural successors plus every BranchEdge
    #: target. Used for cycle detection and reachability, because a
    #: decision "flows" data through its branches even though it emits no
    #: add_edge -- a branch target is reachable and must not be reported
    #: as stranded.
    dispatch_successors: dict[str, list[str]]
    dispatch_predecessors: dict[str, list[str]]
    #: Node ids that are the target of at least one DelegateEdge and of NO
    #: structural or branch edge -- these get no ``builder.step`` call and
    #: no ``steps/<id>.py`` file, because their orchestrator calls them as
    #: tools instead.
    delegate_only_node_ids: frozenset[str]
    #: Node ids with two or more structural successors -- these get a
    #: synthetic ``"<id>_broadcast_fork"`` node injected by ``build()``.
    fanout_source_node_ids: frozenset[str]
    #: Node ids with zero structural successors -- these need an explicit
    #: ``add_edge(..., builder.end_node)``, or the graph never reaches its
    #: end node.
    structural_sink_node_ids: frozenset[str]

    def broadcast_fork_ids(self) -> dict[str, str]:
        """Return the predicted ``{source_id: "<source_id>_broadcast_fork"}`` map.

        Empty when the graph has no fan-out source.
        """
        return {
            node_id: f"{node_id}_broadcast_fork"
            for node_id in sorted(self.fanout_source_node_ids)
        }


def analyze(graph: SwarmGraph) -> GraphStructure:
    """Compute every structural fact the emitter and validator share.

    One pass over the edges builds four adjacency maps (structural and
    dispatch, successors and predecessors) plus the three node-id sets.

    Args:
        graph: The document to analyze.

    Returns:
        The precomputed structure. An edge naming an unknown node is
        skipped rather than raising: Phase 1 reports that separately, and
        analysis must not be the thing that crashes on a malformed
        in-memory graph.
    """
    node_by_id = {node.id: node for node in graph.nodes}

    structural_successors: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    structural_predecessors: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    dispatch_successors: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    dispatch_predecessors: dict[str, list[str]] = {node.id: [] for node in graph.nodes}

    delegate_targets: set[str] = set()
    non_delegate_targets: set[str] = set()

    for edge in graph.edges:
        if edge.source not in node_by_id or edge.target not in node_by_id:
            continue
        if isinstance(edge, DelegateEdge):
            delegate_targets.add(edge.target)
            continue
        non_delegate_targets.add(edge.target)
        if isinstance(edge, (SeqEdge, FanoutEdge, JoinEdge)):
            structural_successors[edge.source].append(edge.target)
            structural_predecessors[edge.target].append(edge.source)
        if isinstance(edge, _DISPATCH_EDGE_KINDS):
            dispatch_successors[edge.source].append(edge.target)
            dispatch_predecessors[edge.target].append(edge.source)

    # The entry node is never delegate-only even if its only inbound edge
    # is a DelegateEdge: the graph has to start somewhere.
    delegate_only_node_ids = frozenset(
        (delegate_targets - non_delegate_targets) - {graph.entry_node_id}
    )

    fanout_source_node_ids = frozenset(
        node_id
        for node_id, successors in structural_successors.items()
        if len(successors) >= _FANOUT_MIN_SUCCESSORS
    )

    structural_sink_node_ids = frozenset(
        node_id
        for node_id, successors in structural_successors.items()
        if len(successors) == 0
        and node_id not in delegate_only_node_ids
        # A decision node never gets an end edge from this rule: it always
        # dispatches through .branch(...).to(...), never add_edge.
        and node_by_id[node_id].kind != "decision"
    )

    return GraphStructure(
        graph=graph,
        node_by_id=node_by_id,
        structural_successors=structural_successors,
        structural_predecessors=structural_predecessors,
        dispatch_successors=dispatch_successors,
        dispatch_predecessors=dispatch_predecessors,
        delegate_only_node_ids=delegate_only_node_ids,
        fanout_source_node_ids=fanout_source_node_ids,
        structural_sink_node_ids=structural_sink_node_ids,
    )


def decision_source_id(structure: GraphStructure, decision_node_id: str) -> str | None:
    """Return the node whose value a decision classifies.

    That is the node with a structural edge into the decision -- its
    ``output_type``, not any node further upstream, is what the decision's
    branch targets must declare as their ``input_type``, because a branch
    target receives the decision source's match value as ``ctx.inputs``.

    Args:
        structure: Analysis of the document the decision belongs to.
        decision_node_id: The decision node to look up.

    Returns:
        The source node's id, or ``None`` when the decision has no inbound
        structural edge (a Phase-1 error case).
    """
    predecessors = structure.structural_predecessors.get(decision_node_id, [])
    return predecessors[0] if predecessors else None


__all__ = [
    "GraphStructure",
    "STRUCTURAL_EDGE_KINDS",
    "analyze",
    "decision_source_id",
    "is_structural",
]
