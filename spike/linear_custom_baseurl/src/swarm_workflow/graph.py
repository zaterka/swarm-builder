"""GraphBuilder wiring for the linear spike (fact 6, codegen contract 1-4, 6, 12).

start -> intake -> research (agent) -> summarize -> end

This file is emitted deterministically in the real pipeline and never
touched by the fill model. It uses only the verified 2.43 API:
``GraphBuilder``, ``builder.step(fn, node_id=...)``, ``builder.add_edge``,
``builder.start_node`` / ``builder.end_node`` properties, and
``builder.build()``. ``Graph(...)`` is never called directly.
"""

from __future__ import annotations

from pydantic_graph import GraphBuilder

from swarm_workflow.deps import Deps
from swarm_workflow.state import State
from swarm_workflow.steps.intake import intake
from swarm_workflow.steps.research import research
from swarm_workflow.steps.summarize import summarize

builder = GraphBuilder(
    name="linear",
    state_type=State,
    deps_type=Deps,
    input_type=str,
    output_type=str,
)

intake_node = builder.step(intake, node_id="intake")
research_node = builder.step(research, node_id="research")
summarize_node = builder.step(summarize, node_id="summarize")

builder.add_edge(builder.start_node, intake_node)
builder.add_edge(intake_node, research_node)
builder.add_edge(research_node, summarize_node)
builder.add_edge(summarize_node, builder.end_node)

graph = builder.build()
