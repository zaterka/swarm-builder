"""Agent factory for the ``research`` step.

Per the shared agent contract (PLAN.md "Templates" / codegen contract
rule 3): no module constructs an ``Agent`` at import time. The model
always arrives from ``ctx.deps.model`` inside the step body, which is
what lets the validation dry run inject ``TestModel()``.
"""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.models import Model


def build_researcher(model: Model | str) -> Agent[None, str]:
    """Build (never at import time) the research agent for this model."""
    return Agent(
        model,
        instructions=(
            "You research the given topic and write two or three concise "
            "sentences of factual notes about it."
        ),
        defer_model_check=True,
    )
