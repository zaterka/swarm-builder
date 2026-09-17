"""``split`` step: the fan-out source. Two `add_edge` calls from this
node (to ``left`` and ``right`` in graph.py) trigger `build()` to inject
a synthetic ``split_broadcast_fork`` node (PLAN.md fact 13)."""

from __future__ import annotations

from pydantic_graph import StepContext

from swarm_workflow.deps import Deps
from swarm_workflow.state import State


async def split(ctx: StepContext[State, Deps, str]) -> str:
    return ctx.inputs
