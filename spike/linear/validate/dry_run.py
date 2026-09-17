"""Validation gate for the linear spike (PLAN.md Phase 5, codegen contract).

Asserts, in order:

1. ``graph.build()`` already succeeded at import time (importing
   ``swarm_workflow.graph`` calls ``builder.build()`` at module scope);
   here we just confirm the resulting object is usable.
2. ``graph.render()`` equals the golden diagram committed alongside this
   script -- drift here means the wiring changed.
3. ``set(graph.nodes)`` equals the expected node-id set: canvas step ids
   plus ``__start__``/``__end__`` (no synthetic fork node in a linear
   graph -- that only appears for fan-out, see spike/fanout).
4. ``graph.run(inputs=..., state=State(), deps=Deps(model=TestModel()))``
   returns without raising, and the returned value matches the declared
   ``output_type`` (``str``) -- no ``.output`` access, no ``End``
   assertion (fact 15: ``graph.run()`` returns the bare value).

Run with:

    UV_CACHE_DIR=<...> uv run python validate/dry_run.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pydantic_ai.models.test import TestModel

from swarm_workflow.deps import Deps
from swarm_workflow.graph import graph
from swarm_workflow.state import State

GOLDEN_PATH = Path(__file__).parent / "golden_render.txt"
EXPECTED_NODES = {"__start__", "intake", "research", "summarize", "__end__"}


def check_render() -> None:
    golden = GOLDEN_PATH.read_text()
    actual = graph.render()
    assert actual == golden, (
        f"render() drifted from golden.\n--- golden ---\n{golden}\n"
        f"--- actual ---\n{actual}"
    )
    print("OK render() matches golden")


def check_nodes() -> None:
    actual = set(graph.nodes.keys())
    assert actual == EXPECTED_NODES, (
        f"graph.nodes keys {actual} != expected {EXPECTED_NODES}"
    )
    print(f"OK graph.nodes == {sorted(actual)}")


async def check_run() -> None:
    out = await graph.run(
        inputs="swarm builder",
        state=State(),
        deps=Deps(model=TestModel()),
    )
    assert isinstance(out, str), f"expected str output, got {type(out)}: {out!r}"
    assert out.startswith("SUMMARY:"), f"unexpected output: {out!r}"
    print(f"OK graph.run() returned: {out!r}")


def main() -> None:
    check_render()
    check_nodes()
    asyncio.run(check_run())
    print("ALL CHECKS PASSED (linear)")


if __name__ == "__main__":
    main()
