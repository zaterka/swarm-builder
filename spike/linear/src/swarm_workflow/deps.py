"""Dependency seam carrying the model into every step (fact 16).

``Deps.model`` is a ``pydantic_ai.models.Model`` instance or a known model
string. Steps build their own ``Agent`` from ``ctx.deps.model`` inside the
step body, never at import time (facts 7, 16) -- this is what lets the
validation gate inject ``TestModel()`` with no provider key present.

This is the **known-name** emission path (PLAN.md fact 22, row 1): the
harness route ``amazon-bedrock`` / ``us.anthropic.claude-opus-5`` is a
member of PydanticAI's ``KnownModelName`` union, so it is handed to
``Agent`` as a plain prefixed string. Compare with
``spike/linear_custom_baseurl/src/swarm_workflow/deps.py`` for the
structural custom-``baseURL`` path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pydantic_ai.models import Model

#: Default production model: a known name, resolved lazily (never
#: constructed at import time). Overridable via SWARM_MODEL so the
#: exported project stays portable (PLAN.md "Model inheritance").
DEFAULT_MODEL: str = "bedrock:us.anthropic.claude-opus-5"


def _resolve_default_model() -> str:
    return os.environ.get("SWARM_MODEL", DEFAULT_MODEL)


@dataclass
class Deps:
    """Graph-level dependency injection seam.

    ``model`` defaults to the resolved known-name string; the validation
    dry run overrides it with ``TestModel()`` (a ``Model`` instance) so no
    live provider is ever contacted.
    """

    model: Model | str = field(default_factory=_resolve_default_model)
