"""``right`` branch step: the other fan-out arm."""

from __future__ import annotations

from pydantic_graph import StepContext

from swarm_workflow.deps import Deps
from swarm_workflow.state import State


async def right(ctx: StepContext[State, Deps, str]) -> str:
    return f"R:{ctx.inputs}"
