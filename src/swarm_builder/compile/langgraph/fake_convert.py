"""Deterministic stub conversion for ``SWARM_FAKE_FILL=1`` (Phase 7's stand-in).

Mirrors :mod:`swarm_builder.compile.fake_fill` for the LangGraph node
shape: every ``programmatic`` node body becomes a type-correct expression
of ``inputs``, honours ``writes``, and -- when the node feeds a decision --
returns that decision's first match value so the keyless dry run still
takes a real branch. Written through the same marker splice the real
conversion agent's ``write_region`` uses, so ``lg_boundary`` is exercised.
"""

from __future__ import annotations

from pathlib import Path

from swarm_builder.compile.agent import splice_region
from swarm_builder.compile.fake_fill import FILLABLE_NODE_KINDS
from swarm_builder.compile.langgraph import NODES_DIR_PARTS
from swarm_builder.models import PortType, SwarmGraph, SwarmNode

_RETURN_EXPRESSIONS: dict[PortType, str] = {
    "str": "str(inputs)",
    "list[str]": "[str(inputs)]",
    "json": '{"input": str(inputs)}',
}

_BODY_INDENT = "    "


def _decision_fed(graph: SwarmGraph, node: SwarmNode) -> SwarmNode | None:
    node_by_id = {n.id: n for n in graph.nodes}
    for edge in graph.edges:
        if edge.source == node.id:
            target = node_by_id.get(edge.target)
            if target is not None and target.kind == "decision":
                return target
    return None


def stub_langgraph_body(graph: SwarmGraph, node: SwarmNode) -> str:
    """A type-correct LangGraph body for ``node``, indented one level."""
    lines = [f'{_BODY_INDENT}writes["{field}"] = inputs' for field in node.writes]
    decision = _decision_fed(graph, node)
    if decision is not None and decision.decision is not None and decision.decision.branches:
        expression = repr(decision.decision.branches[0].match)
    else:
        expression = _RETURN_EXPRESSIONS[node.io.output_type]
    lines.append(f"{_BODY_INDENT}return {expression}")
    return "\n".join(lines)


def apply_fake_convert(project_dir: Path, graph: SwarmGraph) -> tuple[str, ...]:
    """Fill every programmatic node body of a scaffolded LangGraph project.

    Returns:
        The converted node ids, in graph order.
    """
    nodes_dir = project_dir.joinpath(*NODES_DIR_PARTS)
    converted: list[str] = []
    for node in graph.nodes:
        if node.kind not in FILLABLE_NODE_KINDS:
            continue
        path = nodes_dir / f"{node.id}.py"
        if not path.is_file():
            continue
        path.write_text(
            splice_region(path.read_text(), node.id, "body", stub_langgraph_body(graph, node))
        )
        converted.append(node.id)
    return tuple(converted)


__all__ = ["apply_fake_convert", "stub_langgraph_body"]
