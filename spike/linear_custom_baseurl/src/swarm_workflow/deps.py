"""Dependency seam carrying the model into every step (fact 16).

``Deps.model`` is a ``pydantic_ai.models.Model`` instance or a known model
string. Steps build their own ``Agent`` from ``ctx.deps.model`` inside the
step body, never at import time (facts 7, 16) -- this is what lets the
validation gate inject ``TestModel()`` with no provider key present.

This is the **structural custom-baseURL** emission path (PLAN.md fact 22,
row 3): the harness route ``kornerstone`` / ``qwen38-27b-fp8`` is *not* a
member of PydanticAI's ``KnownModelName`` union (it points at a
self-hosted OpenAI-compatible endpoint), so it cannot be handed to
``Agent`` as a bare prefixed string. It is instead built structurally as
``OpenAIChatModel(id, provider=OpenAIProvider(base_url=..., api_key=...))``
-- probed working in PLAN.md fact 22. Compare with
``spike/linear/src/swarm_workflow/deps.py`` for the known-name path.

Constructing ``OpenAIChatModel``/``OpenAIProvider`` does **not** perform a
live call, so this module still imports and constructs with no API key
reachable and no network access (the validation dry run below injects
``TestModel()`` before any step runs).
"""

from __future__ import annotations

import os

from dataclasses import dataclass, field

from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

#: The route's provider-owned bare model id (not a KnownModelName).
DEFAULT_MODEL_ID: str = "qwen38-27b-fp8"

#: The route's custom OpenAI-compatible endpoint (PLAN.md fact 22, row 3).
DEFAULT_BASE_URL: str = "https://kornerstone.example.invalid/v1"

#: Env var naming convention mirrors PLAN.md's `.env.example` contract:
#: SWARM_MODEL / SWARM_BASE_URL / SWARM_API_KEY_ENV.
_MODEL_ID = os.environ.get("SWARM_MODEL", DEFAULT_MODEL_ID)
_BASE_URL = os.environ.get("SWARM_BASE_URL", DEFAULT_BASE_URL)
_API_KEY_ENV = os.environ.get("SWARM_API_KEY_ENV", "SWARM_API_KEY")


def _resolve_default_model() -> Model:
    """Build (lazily, at Deps-construction time -- never at import time)
    the structural custom-baseURL model. No live call is made here."""
    return OpenAIChatModel(
        _MODEL_ID,
        provider=OpenAIProvider(
            base_url=_BASE_URL,
            api_key=os.environ.get(_API_KEY_ENV, "unset-placeholder-key"),
        ),
    )


@dataclass
class Deps:
    """Graph-level dependency injection seam.

    ``model`` defaults to the structurally-built custom-baseURL model; the
    validation dry run overrides it with ``TestModel()`` (a ``Model``
    instance) so no live provider is ever contacted.
    """

    model: Model | str = field(default_factory=_resolve_default_model)
