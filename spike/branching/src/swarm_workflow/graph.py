"""GraphBuilder wiring for the branching spike (fact 14, codegen contract 8).

start -> classify -> decision -> (big | small) -> end

**Emission-order rule (fact 14, load-bearing).** ``Decision`` is
immutable: ``Decision.branch()`` returns a *new* ``Decision`` object.
The complete ``.branch(...)`` chain must be built and rebound to
``decision`` BEFORE the ``add_edge`` call that targets it. Adding the
edge first and rebinding afterward fails at ``build()`` with
``GraphValidationError: The following nodes have no outgoing edges:
['decision']`` -- verified directly (see spike/FINDINGS.md). A decision
with zero branches also builds and only fails at run time with
``RuntimeError: No branch matched inputs`` (fact 14) -- also verified.
"""

from __future__ import annotations

from typing import Literal

from pydantic_graph import GraphBuilder

from swarm_workflow.deps import Deps
from swarm_workflow.state import State
from swarm_workflow.steps.big import big
from swarm_workflow.steps.classify import classify
from swarm_workflow.steps.small import small

builder = GraphBuilder(
    name="branching",
    state_type=State,
    deps_type=Deps,
    input_type=str,
    output_type=str,
)

classify_node = builder.step(classify, node_id="classify")
big_node = builder.step(big, node_id="big")
small_node = builder.step(small, node_id="small")

# Build the COMPLETE branch chain and rebind `decision` before any
# add_edge targets it -- Decision.branch() returns a new object each time,
# so the pre-branch object must never be the one edged in.
decision = builder.decision(node_id="decision", note="classify by length")
decision = decision.branch(builder.match(Literal["big"]).to(big_node))
decision = decision.branch(builder.match(Literal["small"]).to(small_node))

builder.add_edge(builder.start_node, classify_node)
builder.add_edge(classify_node, decision)
builder.add_edge(big_node, builder.end_node)
builder.add_edge(small_node, builder.end_node)

graph = builder.build()
