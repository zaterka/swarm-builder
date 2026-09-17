"""Validation gate for the fanout spike (PLAN.md Phase 5, codegen contract,
fact 13).

Asserts, in order:

1. ``graph.render()`` equals the golden diagram, which includes the
   synthetic ``split_broadcast_fork`` ``<<fork>>`` marker and the
   ``join`` ``<<join>>`` marker (fact 13).
2. ``set(graph.nodes)`` equals the expected set: canvas step ids
   (``split``, ``left``, ``right``, ``join``) plus ``__start__``/
   ``__end__`` **plus** the synthetic ``split_broadcast_fork`` node that
   ``build()`` injects. This is the check that would fail if the emitter
   assumed ``graph.nodes`` keys are exactly the canvas node set.
3. ``graph.run(...)`` returns the **joined collection** -- a list
   containing both ``left`` and ``right``'s output -- not a single
   arbitrary branch value. This is checked as a set comparison because
   the join's completion order is not a documented guarantee (the probe
   observed ``['R:x', 'L:x']`` consistently, but the emitter/validator
   must not assume order).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic_ai.models.test import TestModel

from swarm_workflow.deps import Deps
from swarm_workflow.graph import BROADCAST_FORK_NODE_ID, graph
from swarm_workflow.state import State

GOLDEN_PATH = Path(__file__).parent / "golden_render.txt"
EXPECTED_NODES = {
    "__start__",
    "split",
    "left",
    "right",
    "join",
    "__end__",
    BROADCAST_FORK_NODE_ID,
}


def check_render() -> None:
    golden = GOLDEN_PATH.read_text()
    actual = graph.render()
    assert actual == golden, (
        f"render() drifted from golden.\n--- golden ---\n{golden}\n"
        f"--- actual ---\n{actual}"
    )
    print("OK render() matches golden (includes fork <<fork>> + join <<join>>)")


def check_nodes() -> None:
    actual = set(graph.nodes.keys())
    assert actual == EXPECTED_NODES, (
        f"graph.nodes keys {actual} != expected {EXPECTED_NODES}\n"
        f"(synthetic fork node id assumed: {BROADCAST_FORK_NODE_ID!r})"
    )
    print(f"OK graph.nodes == {sorted(actual)}")
    assert BROADCAST_FORK_NODE_ID in actual, (
        "synthetic broadcast-fork node missing from graph.nodes"
    )
    print(f"OK synthetic fork node {BROADCAST_FORK_NODE_ID!r} present in graph.nodes")


async def check_run_joined() -> None:
    out = await graph.run(
        inputs="x",
        state=State(),
        deps=Deps(model=TestModel()),
    )
    assert isinstance(out, list), f"expected list output, got {type(out)}: {out!r}"
    assert set(out) == {"L:x", "R:x"}, (
        f"expected the JOINED collection {{'L:x', 'R:x'}}, got {out!r} "
        "-- this would indicate fan-out lost a branch (fact 13)"
    )
    assert len(out) == 2, f"expected exactly 2 joined values, got {len(out)}: {out!r}"
    print(f"OK graph.run() returned the joined collection: {out!r}")


def main() -> None:
    check_render()
    check_nodes()
    asyncio.run(check_run_joined())
    print("ALL CHECKS PASSED (fanout)")


if __name__ == "__main__":
    main()
