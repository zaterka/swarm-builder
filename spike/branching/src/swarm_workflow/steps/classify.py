"""``classify`` step: decide the length bucket of the input string.

Returns a ``Literal["big", "small"]`` so ``builder.match(...)`` in
``graph.py`` can dispatch on the value (PLAN.md fact 14).
"""

from __future__ import annotations

from typing import Literal

from pydantic_graph import StepContext

from swarm_workflow.deps import Deps
from swarm_workflow.state import State

Bucket = Literal["big", "small"]


async def classify(ctx: StepContext[State, Deps, str]) -> Bucket:
    bucket: Bucket = "big" if len(ctx.inputs) > 3 else "small"
    ctx.state.length_bucket = bucket
    return bucket
