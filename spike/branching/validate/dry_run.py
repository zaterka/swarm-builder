"""Validation gate for the branching spike (PLAN.md Phase 5, codegen contract,
fact 14).

Asserts, in order:

1. ``graph.render()`` equals the golden diagram (includes the decision
   ``<<choice>>`` marker and the note text -- proving render is a stable
   oracle for decision graphs too, per fact 18).
2. ``set(graph.nodes)`` equals the expected node-id set: canvas step ids
   (``classify``, ``decision``, ``big``, ``small``) plus
   ``__start__``/``__end__``. No synthetic fork node here -- that is a
   fan-out-only artifact (see spike/fanout).
3. ``graph.run(...)`` dispatches to the ``big`` branch for a long input
   and to the ``small`` branch for a short one, proving the ``.branch()``
   chain built BEFORE the ``add_edge`` to ``decision`` actually wires
   correctly (fact 14's emission-order rule).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic_ai.models.test import TestModel

from swarm_workflow.deps import Deps
from swarm_workflow.graph import graph
from swarm_workflow.state import State

GOLDEN_PATH = Path(__file__).parent / "golden_render.txt"
EXPECTED_NODES = {"__start__", "classify", "decision", "big", "small", "__end__"}


def check_render() -> None:
    golden = GOLDEN_PATH.read_text()
    actual = graph.render()
    assert actual == golden, (
        f"render() drifted from golden.\n--- golden ---\n{golden}\n"
        f"--- actual ---\n{actual}"
    )
    print("OK render() matches golden (includes decision <<choice>> + note)")


def check_nodes() -> None:
    actual = set(graph.nodes.keys())
    assert actual == EXPECTED_NODES, (
        f"graph.nodes keys {actual} != expected {EXPECTED_NODES}"
    )
    print(f"OK graph.nodes == {sorted(actual)}")


async def check_run_big() -> None:
    out = await graph.run(
        inputs="a long input string",
        state=State(),
        deps=Deps(model=TestModel()),
    )
    # `classify` returns the matched Literal value ("big"/"small"), and
    # THAT value -- not the original raw input -- becomes `ctx.inputs`
    # for the branch step the decision dispatches to.
    assert out == "BIG:big", f"unexpected output: {out!r}"
    print(f"OK big branch dispatched correctly: {out!r}")


async def check_run_small() -> None:
    out = await graph.run(
        inputs="ab",
        state=State(),
        deps=Deps(model=TestModel()),
    )
    assert out == "small:small", f"unexpected output: {out!r}"
    print(f"OK small branch dispatched correctly: {out!r}")


def main() -> None:
    check_render()
    check_nodes()
    asyncio.run(check_run_big())
    asyncio.run(check_run_small())
    print("ALL CHECKS PASSED (branching)")


if __name__ == "__main__":
    main()
