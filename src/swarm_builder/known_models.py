"""The installed PydanticAI ``KnownModelName`` union, read in one place.

**Why this is its own module.** Two unrelated layers need to ask the same
question -- "is this ``provider:model`` string something PydanticAI actually
knows?" -- and they sit on opposite sides of an import chain:

- :mod:`swarm_builder.compile.pipeline` asks it to warn about a project
  whose emitted default names an unknown model (via
  ``inherit.routes.is_known_model_name``);
- :mod:`swarm_builder.providers` asks it to keep the in-app model picker's
  curated suggestions honest.

``inherit/routes.py`` imports ``compile``, and ``providers.py`` is imported by
``appconfig``/``runtime``, which ``inherit.settings`` imports -- so asking the
question *from* ``inherit.routes`` would close an import cycle the moment
``appconfig`` needed a provider lookup. Hoisting the union into this leaf
module (which imports nothing but ``pydantic_ai``) removes the cycle instead
of papering over it with a function-local import.

**Why the answer is cached.** Resolving the union's member list is not free,
and it cannot change while the process runs -- unlike ``settings.yaml`` or
``workspace/settings.json``, both of which are deliberately re-read per call.
The cache is a plain module global, populated at most once.
"""

from __future__ import annotations

from typing import get_args

try:  # pragma: no cover - the import itself is the branch under test
    from pydantic_ai.models import KnownModelName
except ImportError:  # pragma: no cover
    KnownModelName = None  # type: ignore[assignment]

#: Lazily-populated cache of the union's member names.
_KNOWN_MODEL_NAMES_CACHE: frozenset[str] | None = None


def known_model_names() -> frozenset[str]:
    """Every ``provider:model`` string the installed PydanticAI knows.

    Returns an empty set when ``pydantic_ai`` cannot be imported at all, so a
    caller degrades to "nothing is known" rather than raising during an
    unrelated request.
    """
    global _KNOWN_MODEL_NAMES_CACHE
    if _KNOWN_MODEL_NAMES_CACHE is None:
        if KnownModelName is None:  # pragma: no cover - defensive
            _KNOWN_MODEL_NAMES_CACHE = frozenset()
        else:
            value = getattr(KnownModelName, "__value__", KnownModelName)
            _KNOWN_MODEL_NAMES_CACHE = frozenset(str(name) for name in get_args(value))
    return _KNOWN_MODEL_NAMES_CACHE


def is_known_model_name(candidate: str) -> bool:
    """Report whether ``candidate`` is a name PydanticAI actually knows.

    The known-name emission path hands a bare ``provider:model`` string to
    ``Agent``, which only resolves it if the string is a member of
    PydanticAI's ``KnownModelName`` union. Nothing else in the pipeline can
    catch a miss: agents are constructed with ``defer_model_check=True`` and
    the Phase-5 dry run injects ``TestModel``, so a project naming a
    nonexistent model passes the whole keyless gate and fails only when a user
    finally runs the export with real credentials.

    A miss is reported as a Phase-1 warning rather than an error, because the
    union is pinned to the installed ``pydantic-ai`` version and a genuinely
    newer provider model id would otherwise be refused.

    Args:
        candidate: A prefixed model name such as ``bedrock:us.anthropic...``.

    Returns:
        ``True`` when the name is in the installed union.
    """
    return candidate in known_model_names()


#: Output-token budget an in-process agent call asks for on a model that thinks
#: before it answers.
#:
#: Measured, not guessed: drafting from a vision prompt through
#: ``anthropic:claude-sonnet-5`` used 5135 output tokens for one GraphDraft, while
#: pydantic-ai's Anthropic default is ``max_tokens=4096``. A thinking model spends
#: that budget on thinking first, so it sometimes returns ``finish_reason=length``
#: with no text at all -- and pydantic-ai deliberately does *not* retry that case,
#: failing the whole request with "Model token limit (provider default) exceeded
#: before any response was generated". Text-only drafts squeaked under the limit;
#: attaching a file pushed the input (and the thinking) past it.
THINKING_MODEL_MAX_OUTPUT_TOKENS = 16384


def default_max_output_tokens(model: str) -> int | None:
    """The ``max_tokens`` an in-process agent call should ask for, or ``None``.

    ``None`` means "leave the provider's own default alone": only the model
    families where a too-small default is a *known* failure get an explicit
    budget, so every other route keeps behaving exactly as it did.

    The check is on the model id's last dot-separated segment, which covers both
    a bare id (``claude-sonnet-5``) and a Bedrock one
    (``us.anthropic.claude-sonnet-4-6``) without a second normalizer.

    Args:
        model: The resolved model id, possibly with a ``provider:`` prefix.

    Returns:
        The budget, or ``None`` to use the provider default.
    """
    text = (model or "").strip().lower()
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    segment = text.rsplit(".", 1)[-1]
    if segment.startswith("claude-"):
        return THINKING_MODEL_MAX_OUTPUT_TOKENS
    return None


__all__ = [
    "THINKING_MODEL_MAX_OUTPUT_TOKENS",
    "default_max_output_tokens",
    "is_known_model_name",
    "known_model_names",
]
