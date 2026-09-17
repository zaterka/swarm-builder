"""``intake`` step: normalize the raw input topic string.

Plain programmatic step -- writes ``State.topic`` (the field it owns per
the graph's reads/writes convention, PLAN.md I2).
"""

from __future__ import annotations

from pydantic_graph import StepContext

from swarm_workflow.state import State


async def intake(ctx: StepContext[State, None, str]) -> str:
    topic = ctx.inputs.strip()
    ctx.state.topic = topic
    return topic
