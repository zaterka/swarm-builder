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


#: Output-token budget an in-process agent call asks for, per model family.
#:
#: The first entry was measured, not guessed: drafting from a vision prompt
#: through ``anthropic:claude-sonnet-5`` used 5135 output tokens for one
#: GraphDraft, while pydantic-ai's Anthropic default is ``max_tokens=4096``. A
#: thinking model spends that budget on thinking first, so it sometimes returns
#: ``finish_reason=length`` with no text at all -- and pydantic-ai deliberately
#: does *not* retry that case, failing the whole request with "Model token limit
#: (provider default) exceeded before any response was generated".
#:
#: The same too-small-default failure generalizes to every provider: the fill and
#: LangGraph-convert agents previously set *no* budget and therefore inherited a
#: provider default (Anthropic 4096, DeepSeek 4096) that truncated multi-hundred-
#: line step bodies. So the table below assigns every family an explicit budget
#: close to its current documented output limit. Where a specific model's hard cap
#: is lower than the family value (e.g. a legacy Claude 3.x model), the provider
#: answers 400 and the remedy is the Max output tokens field in Model settings,
#: which outranks this table via :func:`resolve_max_output_tokens`.
#:
#: Values are deliberately conservative *near* the current limits, not at them:
#: ``deepseek`` stays at its documented chat hard max of 8192 (its reasoner
#: accepts more, but 8192 is always safe), and the ``claude``/``gpt``/``gemini``
#: families use 32768 even though newer members support 64k-128k, because a single
#: family-wide number must not 400 on a still-in-service older member.
_DEFAULT_OUTPUT_TOKEN_BUDGET = 16384

#: family (matched as a prefix of the whole id or any dot-separated segment) ->
#: budget. Order matters: the first matching key wins.
_MODEL_FAMILY_OUTPUT_TOKENS: tuple[tuple[str, int], ...] = (
    ("claude-", 32768),
    ("gpt-", 32768),
    ("o1", 32768),
    ("o3", 32768),
    ("o4", 32768),
    ("deepseek", 8192),
    ("gemini", 32768),
    ("google", 32768),
    ("groq", 16384),
    ("mistral", 16384),
    ("mixtral", 16384),
    ("llama", 16384),
    ("grok", 16384),
)

#: Backwards-compatible alias kept for callers that referenced the old constant.
THINKING_MODEL_MAX_OUTPUT_TOKENS = 32768


def default_max_output_tokens(model: str) -> int:
    """The ``max_tokens`` an in-process agent call should ask for.

    Every family now gets an explicit budget: the providers' own defaults are
    exactly the too-small values this table exists to raise, so returning
    ``None`` ("leave the provider default alone") would silently reintroduce the
    truncation this fixes. An unknown/custom endpoint gets the conservative
    floor :data:`_DEFAULT_OUTPUT_TOKEN_BUDGET` rather than nothing.

    A leading ``provider:`` prefix is stripped first. The remaining id is then
    matched against the whole string *and* every dot-separated segment, because
    both naming shapes occur in the wild: a Bedrock id carries its family in a
    dot segment (``us.anthropic.claude-sonnet-4-6``), while a bare modern id
    carries it up front and the dots are version separators (``gpt-5.6-sol``,
    ``deepseek-v4.1-flash``) -- matching only the last dot segment would miss
    every versioned id.

    Args:
        model: The resolved model id, possibly with a ``provider:`` prefix.

    Returns:
        The output-token budget to pass as ``ModelSettings(max_tokens=...)``.
    """
    text = (model or "").strip().lower()
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    candidates = [text, *text.split(".")]
    for family, budget in _MODEL_FAMILY_OUTPUT_TOKENS:
        if any(candidate.startswith(family) for candidate in candidates):
            return budget
    return _DEFAULT_OUTPUT_TOKEN_BUDGET


def resolve_max_output_tokens(model: str, override: int | None) -> int:
    """Resolve the effective output budget: an explicit user setting wins,
    otherwise the family default applies.

    Deliberately scalar parameters rather than an ``EffectiveModel``: this module
    is a leaf (it may import nothing but ``pydantic_ai`` -- see the module
    docstring), so the override lookup cannot depend on ``inherit.settings``.
    Callers pass ``effective.model`` and ``effective.max_tokens``.

    Args:
        model: The resolved model id, possibly with a ``provider:`` prefix.
        override: The user-configured budget, or ``None`` to use the default.

    Returns:
        The budget to ask for.
    """
    if override is not None:
        return override
    return default_max_output_tokens(model)


__all__ = [
    "THINKING_MODEL_MAX_OUTPUT_TOKENS",
    "default_max_output_tokens",
    "is_known_model_name",
    "known_model_names",
    "resolve_max_output_tokens",
]
