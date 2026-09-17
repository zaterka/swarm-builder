"""GraphBuilder wiring for the fanout spike (fact 13, codegen contract 7).

start -> split -> (fork) -> left, right -> join -> end

**Real fan-in needs an explicit join (fact 13, load-bearing).** Two
``add_edge`` calls out of ``split`` (to ``left`` and to ``right``) is a
plain multi-successor fan-out: it builds and runs both steps, but
``graph.run()`` returns only ONE arbitrary branch's value -- silently
dropping the other. Verified directly (see spike/FINDINGS.md). Adding an
explicit ``builder.join(reduce_list_append, initial_factory=list)`` and
edging both ``left`` and ``right`` into it makes ``graph.run()`` return
the full joined list, in this build's non-deterministic completion order
(``['R:x', 'L:x']`` was observed every run in the probe -- treated here
as an unordered collection, not an ordering guarantee).

``build()`` also injects a synthetic ``split_broadcast_fork`` node, so
``graph.nodes`` keys are **not** exactly the canvas node set (fact 13).
"""

from __future__ import annotations

from pydantic_graph import GraphBuilder, reduce_list_append

from swarm_workflow.deps import Deps
from swarm_workflow.state import State
from swarm_workflow.steps.left import left
from swarm_workflow.steps.right import right
from swarm_workflow.steps.split import split

builder = GraphBuilder(
    name="fanout",
    state_type=State,
    deps_type=Deps,
    input_type=str,
    output_type=list,
)

split_node = builder.step(split, node_id="split")
left_node = builder.step(left, node_id="left")
right_node = builder.step(right, node_id="right")

# Explicit join: the reducer accumulates every fan-in value into a list.
# This is what makes the joined collection -- not one arbitrary branch --
# the graph's output (fact 13).
join_node = builder.join(reduce_list_append, initial_factory=list, node_id="join")

builder.add_edge(builder.start_node, split_node)
builder.add_edge(split_node, left_node)
builder.add_edge(split_node, right_node)
builder.add_edge(left_node, join_node)
builder.add_edge(right_node, join_node)
builder.add_edge(join_node, builder.end_node)

graph = builder.build()

#: The synthetic fork node `build()` injects for `split`'s fan-out
#: (fact 13). Its exact id is derived from the source node's id, verified
#: directly and recorded so the emitter/validator can compute it too.
BROADCAST_FORK_NODE_ID = "split_broadcast_fork"
