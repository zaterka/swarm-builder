"""``summarize`` step: plain programmatic final step (no agent)."""

from __future__ import annotations

from pydantic_graph import StepContext

from swarm_workflow.state import State


async def summarize(ctx: StepContext[State, None, str]) -> str:
    return f"SUMMARY: {ctx.inputs.strip()}"
