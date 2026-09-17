"""Deterministic stub fill for ``SWARM_FAKE_FILL=1``.

Phase 3 is the only phase of the compile pipeline that calls a model.
Setting ``SWARM_FAKE_FILL=1`` swaps it for this module, which makes
phases 1, 2, 4, 5 and the whole SSE/cancel/409/reconnect job lifecycle
testable with no model, no credentials, and no ``$DSH_HOME`` (see
PLAN.md, "The compile pipeline").

Two properties make this a useful stand-in rather than a toy:

* **It writes only inside the body markers**, through the same
  marker-region splice the real fill agent's ``write_region`` tool
  performs, so a fake-fill compile exercises Phase 4's boundary check
  for real instead of bypassing it.
* **It produces type-correct code derived from each node's declared
  output port type**, so a fake-fill compile genuinely passes Phase 5's
  keyless gate (import, ``build()``, the ``render()`` golden, the
  node-set assertion, and the ``TestModel`` dry run).

The bodies are derived from the graph document itself rather than from a
per-fixture lookup table, so this works for an arbitrary user graph and
not merely for the repository's test fixtures.

``scaffold.py`` already renders a complete body for every ``agent`` node
(it builds the agent from ``ctx.deps.model`` and returns
``result.output``), so the only genuinely unfilled bodies are those of
``programmatic``, ``decision`` and ``join`` nodes, which it emits as
``raise NotImplementedError``. Filling exactly those is what the real
agent does too.
"""

from __future__ import annotations

from pathlib import Path

from swarm_builder.compile import body_marker_begin, body_marker_end
from swarm_builder.models import PortType, SwarmGraph, SwarmNode

#: The placeholder body ``scaffold.py`` emits for a step whose body the
#: fill stage is expected to replace. A step still carrying this after
#: Phase 3 means the fill did not happen.
UNFILLED_BODY_SENTINEL = 'raise NotImplementedError("swarm_builder: unfilled step body")'

#: Node kinds whose step bodies ``scaffold.py`` leaves unfilled.
#:
#: Only ``programmatic`` qualifies. An ``agent`` node is already complete
#: when it is scaffolded (its body builds the agent from
#: ``ctx.deps.model`` and returns ``result.output``), so filling it would
#: overwrite working code. A ``decision`` or ``join`` node gets no
#: ``steps/<id>.py`` module at all -- both are wired entirely in
#: ``graph.py`` via ``builder.decision(...)``/``builder.join(...)`` --
#: so there is nothing to fill; verified by scaffolding the decision and
#: fan-out fixtures and finding no step module for either kind.
FILLABLE_NODE_KINDS = frozenset({"programmatic"})

#: PortType -> an expression that is valid for that annotation and is
#: derived from the step's input, so a stub body still threads data
#: through the graph instead of returning a constant. Keyed by the
#: node's *output* port type (fact 17: a PortType is never interpolated
#: into an annotation or an expression verbatim).
_RETURN_EXPRESSIONS: dict[PortType, str] = {
    "str": 'f"{ctx.inputs}"',
    "list[str]": "[str(ctx.inputs)]",
    "json": '{"input": str(ctx.inputs)}',
}

#: Indentation of a step-function body: one level inside ``async def``.
_BODY_INDENT = "    "


def _decision_return_expression(node: SwarmNode) -> str:
    """Return an expression yielding one of a decision node's match values.

    A decision's downstream branches are dispatched on the value its
    source step returns, and a run raises ``RuntimeError: No branch
    matched inputs`` if that value matches no branch (fact 14). A stub
    body must therefore return a value that actually matches a declared
    branch, not an arbitrary string.

    Args:
        node: The decision node whose branch match values are read.

    Returns:
        A Python expression producing a matching branch value.
    """
    if node.decision is not None and node.decision.branches:
        return repr(node.decision.branches[0].match)
    return _RETURN_EXPRESSIONS["str"]


def _decision_feeding(graph: SwarmGraph, node: SwarmNode) -> SwarmNode | None:
    """Return the decision node this node feeds, if any.

    Data does not pass through a decision node: a branch target receives
    the value the decision's *source* step returned (fact 27). So a stub
    body for a step feeding a decision must itself return one of that
    decision's match values, or the graph run raises ``RuntimeError: No
    branch matched inputs`` even though every body is type-correct.

    Args:
        graph: The canvas document being filled.
        node: The candidate source node.

    Returns:
        The decision node fed by ``node``, or ``None``.
    """
    node_by_id = {n.id: n for n in graph.nodes}
    for edge in graph.edges:
        if edge.source != node.id:
            continue
        target = node_by_id.get(edge.target)
        if target is not None and target.kind == "decision":
            return target
    return None


def stub_body_for(node: SwarmNode, graph: SwarmGraph | None = None) -> str:
    """Build a type-correct stub body for one node's step function.

    Args:
        node: The node whose step body is being filled.
        graph: The document ``node`` belongs to. Supplying it lets a step
            that feeds a decision return a matching branch value; without
            it, that case cannot be detected.

    Returns:
        The body source, indented one level, with no trailing newline.
    """
    lines: list[str] = []

    # Honor declared state ownership: a stub that ignores `writes` would
    # leave a downstream `reads` field unset, which Phase 5's dry run
    # can surface as an AttributeError rather than a fill problem.
    for write_field in node.writes:
        lines.append(f"{_BODY_INDENT}ctx.state.{write_field} = ctx.inputs")

    fed_decision = _decision_feeding(graph, node) if graph is not None else None
    if node.kind == "decision":
        return_expression = _decision_return_expression(node)
    elif fed_decision is not None:
        return_expression = _decision_return_expression(fed_decision)
    else:
        return_expression = _RETURN_EXPRESSIONS[node.io.output_type]

    lines.append(f"{_BODY_INDENT}return {return_expression}")
    return "\n".join(lines)


def _splice_region(text: str, node_id: str, body: str) -> str:
    """Replace one body-marker region, leaving all other text untouched.

    Args:
        text: Current source of the step module.
        node_id: Node whose body-marker region is replaced.
        body: Replacement body source, already indented.

    Returns:
        The module source with that one region replaced.

    Raises:
        ValueError: If the node's body markers are absent, which means
            Phase 2 emitted a module the fill stage cannot address.
    """
    begin = body_marker_begin(node_id)
    end = body_marker_end(node_id)
    lines = text.splitlines(keepends=True)

    # Line arithmetic rather than a regex spanning the two markers: an
    # `imports` region is emitted with its markers on consecutive lines,
    # and a DOTALL pattern of the form `begin\n.*?\nend` cannot match a
    # zero-content region because it requires at least one line between
    # them. Matching each marker line independently handles an empty and
    # a populated region identically.
    begin_index: int | None = None
    end_index: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if begin_index is None and stripped == begin:
            begin_index = index
        elif begin_index is not None and stripped == end:
            end_index = index
            break

    if begin_index is None or end_index is None:
        raise ValueError(f"body markers for node {node_id!r} not found")

    # Preserve each marker line verbatim, including its indentation, so
    # Phase 4's outside-marker hash stays byte-identical.
    return "".join(
        [
            *lines[: begin_index + 1],
            f"{body}\n",
            *lines[end_index:],
        ]
    )


def apply_fake_fill(project_dir: Path, graph: SwarmGraph) -> tuple[str, ...]:
    """Fill every unfilled step body in a scaffolded project.

    Args:
        project_dir: Root of the project ``scaffold.py`` just emitted.
        graph: The canvas document that project was scaffolded from.

    Returns:
        The ids of the nodes whose bodies were filled, in graph order.

    Raises:
        ValueError: If a node's step module or body markers are missing.
    """
    steps_dir = project_dir / "src" / "swarm_workflow" / "steps"
    filled: list[str] = []

    for node in graph.nodes:
        if node.kind not in FILLABLE_NODE_KINDS:
            continue
        step_path = steps_dir / f"{node.id}.py"
        if not step_path.exists():
            # A delegate-only node is called as a tool by its
            # orchestrator and is never emitted as a graph step (I1),
            # so having no step module is expected, not an error.
            continue
        original = step_path.read_text()
        step_path.write_text(_splice_region(original, node.id, stub_body_for(node, graph)))
        filled.append(node.id)

    return tuple(filled)


__all__ = [
    "FILLABLE_NODE_KINDS",
    "UNFILLED_BODY_SENTINEL",
    "apply_fake_fill",
    "stub_body_for",
]
