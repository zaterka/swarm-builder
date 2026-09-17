"""Canvas graph -> ``graph.py`` wiring source (Phase 2 of the compile
pipeline).

Every binding rule below was established by probing the real
``pydantic_graph`` library, and each one is a defect that only shows up
when the generated project is built or run -- none is caught by
``review.py``:

- Use ``GraphBuilder(name=..., state_type=State, deps_type=Deps,
  input_type=..., output_type=...)`` then ``builder.build()``; never
  ``Graph(...)`` directly.
- Use the plain-call form ``builder.step(fn, node_id=<id>)``, with an
  explicit ``node_id`` on every step: the node id is what the emitted
  diagram and the boundary check key on, so a defaulted name would make
  them disagree with the canvas.
- Step bodies read ``ctx.inputs``, never ``ctx.input`` (enforced in the
  step templates, not here).
- A decision's complete ``.branch(...)`` chain is emitted BEFORE any
  ``add_edge`` that targets it, rebinding the variable each time.
- Every fan-in goes through an explicit ``builder.join(...)``; two bare
  ``add_edge`` calls are never emitted as a fan-out, because that shape
  silently keeps one arbitrary branch's value.
- ``delegate`` edges emit no ``add_edge`` at all: a delegate target is
  called as an agent tool, so an edge would execute it twice.
- A ``PortType`` goes through :data:`PORT_TYPE_ANNOTATIONS`, never
  interpolated verbatim -- the label ``json`` is not a valid annotation.
- Every generated file starts with ``from __future__ import
  annotations``.
- ``graph.run()``'s return value is used directly -- no ``.output``, no
  ``End`` assertion.

This module assumes the graph already passed Phase 1 (``review.py``) and
does not re-validate: it trusts the port-type and branch consistency rules
that pass already enforced, and a violation here surfaces as broken
generated code rather than as an error raised from emission.
"""

from __future__ import annotations

from swarm_builder.compile.graph_ir import analyze
from swarm_builder.models import (
    PORT_TYPE_ANNOTATIONS,
    PORT_TYPE_IMPORTS,
    REDUCER_FUNCTIONS,
    DecisionSpec,
    FanoutEdge,
    JoinEdge,
    JoinSpec,
    SeqEdge,
    SwarmGraph,
    SwarmNode,
)

#: The ``PORT_TYPE_IMPORTS`` entry meaning "this port type needs
#: ``typing.Any`` at runtime". Compared by value so the emitter reads the
#: requirement off the table instead of pattern-matching an annotation
#: string.
_ANY_IMPORT_LINE = "from typing import Any"

#: ReducerId -> the builtin the emitter passes as ``initial_factory`` when
#: the join node does not override it.
_INITIAL_FACTORY_BY_REDUCER: dict[str, str] = {
    "list_append": "list",
    "list_extend": "list",
    "dict_update": "dict",
    "sum": "int",
}

#: Node kinds whose builder variable is the bare node id rather than a
#: ``_node``-suffixed name: ``builder.decision()`` and ``builder.join()``
#: already return a builder object, so the suffix would only add noise.
_BARE_VARIABLE_KINDS = ("decision", "join")

#: Node kinds emitted as ``builder.step(...)``.
_STEP_KINDS = ("agent", "programmatic")


def _node_var(node: SwarmNode) -> str:
    """Return the local variable name this node's builder call binds.

    Matches the naming the reference spikes settled on: ``<id>_node`` for
    a step (which is a plain function reference until it is registered)
    and the bare ``<id>`` for decision and join nodes.

    Args:
        node: The node being wired.

    Returns:
        The variable name to emit for it.
    """
    if node.kind in _BARE_VARIABLE_KINDS:
        return node.id
    return f"{node.id}_node"


def emit_graph(graph: SwarmGraph) -> str:
    """Render the generated project's ``graph.py``.

    One deterministic pass over the document, in a fixed emission order
    chosen so no line references a name that has not been bound yet:
    imports, then ``GraphBuilder(...)``, then step nodes, then join nodes,
    then every decision's bare variable, then every decision's branch
    chain (a separate pass, so chained decisions can forward-reference
    each other), then the structural edges, then the end edges.

    Args:
        graph: The document to wire up. Must already have passed Phase 1.

    Returns:
        The complete ``graph.py`` source.
    """
    structure = analyze(graph)
    node_by_id = structure.node_by_id
    var_by_id = {node.id: _node_var(node) for node in graph.nodes}

    entry_node = node_by_id[graph.entry_node_id]
    exit_node = node_by_id[graph.exit_node_id]
    input_annotation = PORT_TYPE_ANNOTATIONS[entry_node.io.input_type]
    output_annotation = PORT_TYPE_ANNOTATIONS[exit_node.io.output_type]

    lines: list[str] = []
    lines.append('"""GraphBuilder wiring for the generated project.')
    lines.append("")
    lines.append("This file is emitted deterministically by swarm_builder's")
    lines.append("compile pipeline and is never touched by the fill model.")
    lines.append('"""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")

    # `typing` imports for expressions GraphBuilder(...) evaluates at
    # RUNTIME (input_type=/output_type=, and Literal[...] inside a
    # decision's .match(...) call) -- `from __future__ import
    # annotations` only defers annotations and never these.
    has_decision = any(node.kind == "decision" for node in graph.nodes)
    # Ask PORT_TYPE_IMPORTS which import a port type needs, rather than
    # inspecting the rendered annotation. The annotation for `json` is the
    # string "dict[str, Any]", so a membership test against the annotation
    # values never matches a bare "Any": the import was silently dropped
    # and the emitted graph.py raised `NameError: name 'Any' is not
    # defined` at import time, which is exactly the class of breakage
    # fact 17 and contract rule 9 exist to prevent.
    needs_any_import = any(
        PORT_TYPE_IMPORTS[port_type] == _ANY_IMPORT_LINE
        for port_type in (entry_node.io.input_type, exit_node.io.output_type)
    )
    typing_imports = sorted(
        name
        for name, needed in (("Literal", has_decision), ("Any", needs_any_import))
        if needed
    )
    if typing_imports:
        lines.append(f"from typing import {', '.join(typing_imports)}")
        lines.append("")

    reducer_imports = sorted(
        {
            REDUCER_FUNCTIONS[node.join.reducer]
            for node in graph.nodes
            if node.kind == "join" and node.join is not None
        }
    )
    if reducer_imports:
        lines.append(
            "from pydantic_graph import GraphBuilder, " + ", ".join(reducer_imports)
        )
    else:
        lines.append("from pydantic_graph import GraphBuilder")
    lines.append("")
    lines.append("from swarm_workflow.deps import Deps")
    lines.append("from swarm_workflow.state import State")

    step_import_ids = sorted(
        node.id
        for node in graph.nodes
        if node.kind in _STEP_KINDS and node.id not in structure.delegate_only_node_ids
    )
    for node_id in step_import_ids:
        lines.append(f"from swarm_workflow.steps.{node_id} import {node_id}")
    lines.append("")

    lines.append("builder = GraphBuilder(")
    lines.append(f"    name={graph.id!r},")
    lines.append("    state_type=State,")
    lines.append("    deps_type=Deps,")
    lines.append(f"    input_type={input_annotation},")
    lines.append(f"    output_type={output_annotation},")
    lines.append(")")
    lines.append("")

    # Step nodes, canvas order.
    for node in graph.nodes:
        is_step_node = (
            node.kind in _STEP_KINDS and node.id not in structure.delegate_only_node_ids
        )
        if is_step_node:
            var = var_by_id[node.id]
            lines.append(f'{var} = builder.step({node.id}, node_id="{node.id}")')
    if any(
        node.kind in _STEP_KINDS and node.id not in structure.delegate_only_node_ids
        for node in graph.nodes
    ):
        lines.append("")

    # Join nodes -- emitted before any edge references them.
    join_nodes = [
        node for node in graph.nodes if node.kind == "join" and node.join is not None
    ]
    for node in join_nodes:
        join_spec: JoinSpec = node.join  # type: ignore[assignment]
        reducer_function = REDUCER_FUNCTIONS[join_spec.reducer]
        factory = join_spec.initial_factory or _INITIAL_FACTORY_BY_REDUCER[join_spec.reducer]
        var = var_by_id[node.id]
        lines.append(
            f'{var} = builder.join({reducer_function}, initial_factory={factory}, '
            f'node_id="{node.id}")'
        )
    if join_nodes:
        lines.append("")

    # Decision nodes -- the complete .branch(...) chain is emitted before
    # any add_edge targeting them. Two passes, not one: a decision whose
    # branch target is ANOTHER decision (chained decisions) needs that
    # target decision's bare `builder.decision(...)` variable to already
    # exist before the `.to(target_var)` reference. Emitting each
    # decision's full "declare + branch chain" in one canvas-order pass
    # would therefore raise ``NameError`` on a forward reference;
    # declaring every bare variable first makes pass order irrelevant.
    decision_nodes = [
        node for node in graph.nodes if node.kind == "decision" and node.decision is not None
    ]
    for node in decision_nodes:
        decision_spec: DecisionSpec = node.decision  # type: ignore[assignment]
        var = var_by_id[node.id]
        if decision_spec.note:
            note_repr = repr(decision_spec.note)
            lines.append(f'{var} = builder.decision(node_id="{node.id}", note={note_repr})')
        else:
            lines.append(f'{var} = builder.decision(node_id="{node.id}")')
    if decision_nodes:
        lines.append("")

    for node in decision_nodes:
        decision_spec = node.decision  # type: ignore[assignment]
        var = var_by_id[node.id]
        for branch in decision_spec.branches:
            target_var = var_by_id[branch.target_node_id]
            match_repr = repr(branch.match)
            lines.append(
                f"{var} = {var}.branch(builder.match(Literal[{match_repr}]).to({target_var}))"
            )
        lines.append("")

    # add_edge for structural edges (seq, fanout, join), canvas edge order.
    # builder.start_node -> entry is never a document edge, so it is
    # emitted unconditionally and first.
    entry_var = var_by_id[graph.entry_node_id]
    lines.append(f"builder.add_edge(builder.start_node, {entry_var})")

    for edge in graph.edges:
        if isinstance(edge, (SeqEdge, FanoutEdge, JoinEdge)):
            source_var = var_by_id[edge.source]
            target_var = var_by_id[edge.target]
            lines.append(f"builder.add_edge({source_var}, {target_var})")

    # Every non-delegate-only, non-decision node with zero structural
    # successors needs an explicit end edge, or the graph never reaches
    # the end node and ``run()`` has no result to return.
    for node_id in sorted(structure.structural_sink_node_ids):
        var = var_by_id[node_id]
        lines.append(f"builder.add_edge({var}, builder.end_node)")

    lines.append("")
    lines.append("graph = builder.build()")

    fork_ids = structure.broadcast_fork_ids()
    if fork_ids:
        lines.append("")
        lines.append("#: Synthetic broadcast-fork node ids build() injects.")
        entries = ", ".join(f'"{key}": "{value}"' for key, value in fork_ids.items())
        lines.append(f"BROADCAST_FORK_NODE_IDS: dict[str, str] = {{{entries}}}")

    lines.append("")
    return "\n".join(lines)


__all__ = ["emit_graph"]
