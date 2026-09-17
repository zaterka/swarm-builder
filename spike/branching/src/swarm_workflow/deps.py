"""Dependency seam (PLAN.md fact 16), present for scaffold-shape parity.

This spike has no agent step, so no step reads ``ctx.deps.model`` -- but
``GraphBuilder(deps_type=Deps)`` still wires it through, mirroring the
Phase-2 scaffold shape (``deps.py`` is always emitted).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pydantic_ai.models import Model

DEFAULT_MODEL: str = "bedrock:us.anthropic.claude-opus-5"


def _resolve_default_model() -> str:
    return os.environ.get("SWARM_MODEL", DEFAULT_MODEL)


@dataclass
class Deps:
    model: Model | str = field(default_factory=_resolve_default_model)
