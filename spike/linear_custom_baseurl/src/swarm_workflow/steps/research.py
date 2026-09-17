"""``research`` step: an agent step built from ``ctx.deps.model`` (fact 16).

The agent is constructed fresh on every call from ``build_researcher``,
never at import time, so importing this module requires no provider key.
"""

from __future__ import annotations

from pydantic_graph import StepContext

from swarm_workflow.agents.researcher import build_researcher
from swarm_workflow.deps import Deps
from swarm_workflow.state import State


async def research(ctx: StepContext[State, Deps, str]) -> str:
    agent = build_researcher(ctx.deps.model)
    result = await agent.run(ctx.inputs)
    ctx.state.notes = result.output
    return result.output
