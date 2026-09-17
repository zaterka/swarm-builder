"""``big`` branch step: handles inputs classified as long."""

from __future__ import annotations

from pydantic_graph import StepContext

from swarm_workflow.deps import Deps
from swarm_workflow.state import State


async def big(ctx: StepContext[State, Deps, str]) -> str:
    return f"BIG:{ctx.inputs}"
