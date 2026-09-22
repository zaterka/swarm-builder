"""Mapping a resolved harness route to a PydanticAI model.

This module is the single place that turns an :class:`EffectiveModel`
(produced by :func:`swarm_builder.inherit.settings.resolve_effective_model`)
into something PydanticAI can actually use, in each of the two places the
resolved selection is spent:

1. :func:`build_live_model` -- for the in-process compile agent, which
   hands the result straight to ``pydantic_ai.Agent(...)``.
2. :func:`to_resolved_model` -- for the generated project's ``deps.py``
   (via :class:`swarm_builder.compile.ResolvedModel`), which needs
   literal Python *source text* rather than a live object, since the
   generated project runs in a different process (and often a different
   machine) than this compile agent.

**The two emission paths.** A harness route's model id is either a member
of PydanticAI's ``KnownModelName`` union -- in which case it is handed to
``Agent`` as a plain ``"<prefix>:<id>"`` string -- or it is not, in which
case it must be built structurally as
``OpenAIChatModel(id, provider=OpenAIProvider(base_url=..., api_key=...))``.
A bare, unprefixed model id string is never emitted alone in either path.

**Why base_url is checked before ever consulting the known-name prefix
table -- the correctness trap.** A route's model id can happen to
collide, as a bare string, with a real ``KnownModelName`` entry from an
entirely different provider: the settings example in this project's task
brief has a ``kornerstone`` route (a self-hosted OpenAI-compatible
endpoint) whose model id ``deepseek-v4-flash`` is *also* a member of
PydanticAI's ``deepseek:`` known-name family. If this module picked the
known-name-string path whenever the id happened to match, a route
declaring an explicit ``baseURL`` would silently be routed to the
*official* DeepSeek API instead of the user's self-hosted server: same
string, wrong endpoint, no error raised. The rule this module enforces
throughout is therefore: **a declared ``base_url`` always wins**, checked
first and unconditionally, before the known-name prefix table is even
consulted. The known-name-string path is used only when there is no
``base_url`` at all -- from either the route or the env-fallback
selection.

**Why this is route-classification, not model-id classification
(:func:`classify_route`).** ``/api/models`` needs to tell a picker UI
"this route needs the ``anthropic`` extra" or "this route is
unmappable" before any particular model id within that route -- let
alone a compile -- has even been chosen. :func:`classify_route`
therefore looks only at the route's own declared ``api``/``base_url``,
never at any model id, and is deliberately independent of
:func:`build_live_model`/:func:`to_resolved_model` (which classify a
resolved *model selection*, not a route in isolation).

**Why extras resolution is factored into one private helper
(:func:`_resolve_emission`).** :func:`build_live_model` and
:func:`to_resolved_model` must never be allowed to drift apart on what
counts as mappable, unmappable, or which extras a given protocol needs
-- both call the exact same base_url/prefix/extras decision procedure
and raise the exact same :class:`UnmappableRouteError` for the exact
same inputs. Only the *rendering* differs: one builds a live object,
the other renders literal source text mirroring the two spike
``deps.py`` files.

**Where the extras belong.** An extra derived here is the extra a
*generated project* declares in its own ``pyproject.toml``: it is what
the generated code needs in order to import, and it says nothing about
whether *this* server can build the same model in-process. Those are two
separate questions, and only :func:`_resolve_emission`'s import check
answers the second one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, get_args

from pydantic_ai.models import KnownModelName, Model

from swarm_builder.compile import ResolvedModel
from swarm_builder.inherit.settings import EffectiveModel, RouteConfig

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnmappableRouteError(ValueError):
    """Raised when a route cannot be turned into a usable PydanticAI model.

    Three distinct causes, all reported through this one type: the route's
    ``api`` protocol has no PydanticAI counterpart; the provider has no
    known prefix mapping and no ``baseURL`` to build a model from
    structurally; or a required extra fails to import on *this server's*
    own installed dependency set.

    The last one is worth separating from the generated project's extras:
    it concerns whether the in-process compile agent, running on this
    server's packages, can build the model object at all -- not whether the
    generated project declares the right dependency. Always names the
    route's provider key and, when known, its ``api`` protocol, plus a
    human-readable reason.
    """

    def __init__(self, provider: str, api: str | None, reason: str | None = None) -> None:
        """Build the error, embedding the provider, protocol and reason.

        Args:
            provider: The route's provider key.
            api: The route's declared protocol, when it has one.
            reason: Why the route is unmappable. Omitted when the cause is
                self-evident from the provider and protocol.
        """
        self.provider = provider
        self.api = api
        detail = f"route {provider!r} (api={api!r}) has no usable PydanticAI mapping"
        if reason:
            detail += f": {reason}"
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Public result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveModel:
    """A model object or spec for ``pydantic_ai.Agent(...)``.

    ``model`` is EITHER a plain prefixed string (e.g.
    ``"bedrock:us.anthropic.claude-opus-5"``) for the known-name path, OR
    an actual ``pydantic_ai.models.openai.OpenAIChatModel`` INSTANCE for
    the structural custom-baseURL path. Never a bare, unprefixed id
    string: PydanticAI would resolve that against its own known-name
    table, which is exactly the mis-routing the base-url-first rule
    exists to prevent.
    """

    model: Model | str
    pyproject_extras: tuple[str, ...]
    reasoning_effort: str | None
    source_description: str


#: Lazily-populated cache for :func:`_known_model_names`.
_KNOWN_MODEL_NAMES_CACHE: frozenset[str] | None = None


@dataclass(frozen=True)
class RouteEmission:
    """Route-level (not model-id-level) classification, independent of
    any specific model chosen within the route -- used by ``/api/models``
    to tell the picker UI "this route needs the anthropic extra" or
    "this route is unmappable" before a compile is even attempted.
    """

    emission: Literal["known-name", "structural", "unmappable"]
    required_extra: str | None
    unmappable_reason: str | None


# ---------------------------------------------------------------------------
# Static tables
# ---------------------------------------------------------------------------

#: Static provider-key -> PydanticAI known-name prefix table. Covers
#: every provider key this project's own settings/env can plausibly
#: name. A provider key not in this table, with no baseURL to fall back
#: on structurally, is unmappable.
_KNOWN_ROUTE_PREFIXES: dict[str, str] = {
    "amazon-bedrock": "bedrock",
    "bedrock": "bedrock",
    "deepseek-official": "deepseek",
    "deepseek": "deepseek",
    "anthropic": "anthropic",
    "openai": "openai",
}

#: api protocol -> the bracketed ``pydantic-ai-slim[...]`` extra a
#: generated project needs in order to import that protocol's model class.
#: A protocol not in this table has no PydanticAI counterpart at all.
_PROTOCOL_EXTRAS: dict[str, tuple[str, ...]] = {
    "openai-completions": ("openai",),
    "openai-responses": ("openai",),
    "anthropic-messages": ("anthropic",),
    "bedrock-converse-stream": ("bedrock",),
}

#: Fallback extras keyed by known-name PREFIX, used only when there is no
#: ``api`` protocol available to look up in ``_PROTOCOL_EXTRAS`` (e.g. the
#: env-fallback and bundle-default paths, which have no RouteConfig at
#: all and therefore no ``api``).
_PREFIX_EXTRAS_FALLBACK: dict[str, tuple[str, ...]] = {
    "bedrock": ("bedrock",),
    "deepseek": ("openai",),
    "anthropic": ("anthropic",),
    "openai": ("openai",),
}

#: Protocols that are OpenAI-compatible enough for the structural
#: ``OpenAIChatModel``/``OpenAIProvider`` construction to make sense
#: against a custom ``base_url``.
_OPENAI_COMPATIBLE_PROTOCOLS = ("openai-completions", "openai-responses")

#: Stand-in API key used when building a live model for a route that names
#: no key env var. The model object is only constructed here, never called,
#: so a real credential is not needed and must not be invented from the
#: environment: a placeholder makes an accidental real call fail loudly.
_PLACEHOLDER_API_KEY = "unset-placeholder-key"


# ---------------------------------------------------------------------------
# classify_route
# ---------------------------------------------------------------------------


def classify_route(route: RouteConfig) -> RouteEmission:
    """Classify one :class:`RouteConfig` independently of any chosen model
    id (see the module docstring's "route-classification, not model-id
    classification" note).

    Decision procedure, mirroring :func:`_resolve_emission` but without
    a specific model id to build against:

    - ``route.base_url`` set: structural path, PROVIDED ``route.api`` is
      either unset/unknown or one of the OpenAI-compatible protocols.
      A ``base_url`` paired with a declared non-OpenAI-compatible
      protocol (e.g. a hypothetical ``bedrock-converse-stream`` route
      that also somehow declared a ``baseURL``) is unmappable -- there
      is no structural PydanticAI equivalent for that combination.
    - No ``base_url``: look up ``route.key`` in
      ``_KNOWN_ROUTE_PREFIXES``. Not found -> unmappable (no known
      prefix and no baseURL to fall back on). Found -> known-name path;
      the required extra is derived from ``route.api`` via
      ``_PROTOCOL_EXTRAS`` when set (an ``api`` set to an unmappable
      protocol is itself unmappable, even though the provider key is
      known), else the prefix's ``_PREFIX_EXTRAS_FALLBACK`` entry.
    """
    if route.base_url is not None:
        if route.api is not None and route.api not in _OPENAI_COMPATIBLE_PROTOCOLS:
            return RouteEmission(
                emission="unmappable",
                required_extra=None,
                unmappable_reason=(
                    "a baseURL route's protocol must be an OpenAI-compatible "
                    "one to use the structural OpenAIChatModel path"
                ),
            )
        return RouteEmission(emission="structural", required_extra="openai", unmappable_reason=None)

    prefix = _KNOWN_ROUTE_PREFIXES.get(route.key)
    if prefix is None:
        return RouteEmission(
            emission="unmappable",
            required_extra=None,
            unmappable_reason=(
                "no known PydanticAI prefix for this provider and no baseURL "
                "to build a structural model from"
            ),
        )

    if route.api is not None:
        extras = _PROTOCOL_EXTRAS.get(route.api)
        if extras is None:
            return RouteEmission(
                emission="unmappable",
                required_extra=None,
                unmappable_reason="api protocol has no PydanticAI counterpart",
            )
    else:
        extras = _PREFIX_EXTRAS_FALLBACK.get(prefix, ("openai",))

    return RouteEmission(emission="known-name", required_extra=extras[0], unmappable_reason=None)


# ---------------------------------------------------------------------------
# Shared decision procedure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Emission:
    """Internal result of :func:`_resolve_emission`: enough for both
    :func:`build_live_model` and :func:`to_resolved_model` to render
    their respective output without duplicating any decision logic.
    """

    kind: Literal["known-name", "structural"]
    #: known-name path only: the known-name prefix (e.g. "bedrock").
    prefix: str | None
    #: structural path only: the base_url actually in effect.
    base_url: str | None
    extras: tuple[str, ...]



def is_known_model_name(candidate: str) -> bool:
    """Report whether ``candidate`` is a name PydanticAI actually knows.

    The known-name emission path hands a bare ``provider:model`` string to
    ``Agent``, which only resolves it if the string is a member of
    PydanticAI's ``KnownModelName`` union. Nothing else in the pipeline can
    catch a miss: agents are constructed with ``defer_model_check=True``
    and the Phase-5 dry run injects ``TestModel``, so a project naming a
    nonexistent model passes the whole keyless gate and fails only when a
    user finally runs the export with real credentials.

    A miss is reported as a Phase-1 warning rather than an error, because
    the union is pinned to the installed ``pydantic-ai`` version and a
    genuinely newer provider model id would otherwise be refused.

    Args:
        candidate: A prefixed model name such as ``bedrock:us.anthropic...``.

    Returns:
        ``True`` when the name is in the installed union.
    """
    return candidate in _known_model_names()


def _known_model_names() -> frozenset[str]:
    """The installed ``KnownModelName`` union, read once per process."""
    global _KNOWN_MODEL_NAMES_CACHE
    if _KNOWN_MODEL_NAMES_CACHE is None:
        # Resolving the union member list is not free, and it cannot change
        # while the process runs -- unlike settings.yaml, which is
        # deliberately re-read per call.
        value = getattr(KnownModelName, "__value__", KnownModelName)
        _KNOWN_MODEL_NAMES_CACHE = frozenset(str(name) for name in get_args(value))
    return _KNOWN_MODEL_NAMES_CACHE


def _resolve_emission(effective: EffectiveModel) -> _Emission:
    """The single decision procedure shared by :func:`build_live_model`
    and :func:`to_resolved_model` (base_url always wins -- see the
    module docstring's "correctness trap" note). Raises
    :class:`UnmappableRouteError` for every unmappable input; never
    returns a partial or ambiguous result.
    """
    base_url = effective.base_url if effective.base_url is not None else (
        effective.route.base_url if effective.route is not None else None
    )

    if base_url is not None:
        api = effective.route.api if effective.route is not None else None
        if api is not None and api not in _OPENAI_COMPATIBLE_PROTOCOLS:
            raise UnmappableRouteError(
                effective.provider,
                api,
                reason=(
                    "a baseURL route's protocol must be an OpenAI-compatible "
                    "one to use the structural OpenAIChatModel path"
                ),
            )
        return _Emission(kind="structural", prefix=None, base_url=base_url, extras=("openai",))

    prefix = _KNOWN_ROUTE_PREFIXES.get(effective.provider)
    if prefix is None:
        raise UnmappableRouteError(
            effective.provider,
            effective.route.api if effective.route is not None else None,
            reason=(
                "no known PydanticAI prefix for this provider and no baseURL "
                "to build a structural model from"
            ),
        )

    api = effective.route.api if effective.route is not None else None
    extras = _PROTOCOL_EXTRAS.get(api) if api is not None else None
    if api is not None and extras is None:
        # An explicitly-declared bad protocol must be refused, not
        # silently patched over with the prefix-based guess.
        raise UnmappableRouteError(
            effective.provider,
            api,
            reason="api protocol has no PydanticAI counterpart",
        )
    if extras is None:
        extras = _PREFIX_EXTRAS_FALLBACK.get(prefix, ("openai",))

    if "anthropic" in extras:
        # Verify the extra is actually importable on THIS server, right
        # before returning success. Keyed off the EXTRAS this call
        # actually resolved -- not off `prefix` -- so a route whose
        # provider key happens to map to a different known-name prefix
        # (e.g. a route keyed "openai" that nonetheless declares
        # `api: anthropic-messages`) still gets checked: `extras` comes
        # from `route.api` via `_PROTOCOL_EXTRAS` independently of the
        # prefix, so the two can disagree, and it is `extras` -- what
        # will actually be imported/declared -- that must gate this
        # check, not the provider-key-derived prefix. bedrock/openai are
        # otherwise skipped: this project's own pyproject.toml already
        # pins `pydantic-ai-slim[bedrock,openai]`, so those always
        # import.
        try:
            import anthropic  # noqa: F401
        except ImportError:
            raise UnmappableRouteError(
                effective.provider,
                api,
                reason=(
                    f"this server's own installed dependencies lack the "
                    f"'{extras[0]}' extra needed to construct this route's "
                    "model (a generated project still gets the extra "
                    "declared in its own pyproject.toml -- this failure is "
                    "about the SERVER's ability to run the compile agent "
                    "against this route, not about the generated project)"
                ),
            ) from None

    return _Emission(kind="known-name", prefix=prefix, base_url=None, extras=extras)


# ---------------------------------------------------------------------------
# build_live_model
# ---------------------------------------------------------------------------


def build_live_model(effective: EffectiveModel) -> LiveModel:
    """Build the model object the compile agent hands to ``Agent(...)``.

    See :func:`_resolve_emission` for the shared base_url/prefix/extras
    decision procedure (base_url always wins -- the module docstring's
    "correctness trap" note). This function only renders a *live*
    object from that decision; :func:`to_resolved_model` renders the
    equivalent literal source text for the generated project instead.
    """
    emission = _resolve_emission(effective)

    if emission.kind == "structural":
        # Lazy on purpose: these two live behind the optional `openai`
        # extra, which this server's own dependency set does not have to
        # include. Importing them at module level would make the whole
        # `inherit` package -- and therefore server startup -- depend on an
        # extra only some routes need.
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        api_key = (
            os.environ.get(effective.api_key_env, _PLACEHOLDER_API_KEY)
            if effective.api_key_env
            else _PLACEHOLDER_API_KEY
        )
        model = OpenAIChatModel(
            effective.model,
            provider=OpenAIProvider(base_url=emission.base_url, api_key=api_key),
        )
        return LiveModel(
            model=model,
            pyproject_extras=("openai",),
            reasoning_effort=effective.reasoning_effort,
            source_description=(
                f"{effective.provider} (custom endpoint {emission.base_url}, "
                f"source={effective.source})"
            ),
        )

    return LiveModel(
        model=f"{emission.prefix}:{effective.model}",
        pyproject_extras=emission.extras,
        reasoning_effort=effective.reasoning_effort,
        source_description=f"{effective.provider}:{effective.model} (source={effective.source})",
    )


# ---------------------------------------------------------------------------
# to_resolved_model
# ---------------------------------------------------------------------------


def to_resolved_model(effective: EffectiveModel) -> ResolvedModel:
    """Render this selection as source text for a generated project.

    Produces the :class:`~swarm_builder.compile.ResolvedModel`
    ``scaffold.py`` splices into ``deps.py``, mirroring the two spike
    ``deps.py`` files' exact source-fragment shape
    (``spike/linear/.../deps.py`` for the known-name path,
    ``spike/linear_custom_baseurl/.../deps.py`` for the structural path).

    Reuses :func:`_resolve_emission`'s decision procedure -- same
    base_url/prefix/extras resolution, same :class:`UnmappableRouteError`
    -- so behavior can never drift from :func:`build_live_model`; only
    the rendering (literal Python source text vs. a live object)
    differs.
    """
    emission = _resolve_emission(effective)

    if emission.kind == "structural":
        base_url = emission.base_url
        model_id_literal = repr(effective.model)
        base_url_literal = repr(base_url)
        api_key_env_default_literal = repr(effective.api_key_env or "SWARM_API_KEY")

        helper_source = (
            f"DEFAULT_MODEL_ID: str = {model_id_literal}\n"
            "\n"
            f"DEFAULT_BASE_URL: str = {base_url_literal}\n"
            "\n"
            f'_MODEL_ID = os.environ.get("SWARM_MODEL") or DEFAULT_MODEL_ID\n'
            f'_BASE_URL = os.environ.get("SWARM_BASE_URL") or DEFAULT_BASE_URL\n'
            f'_API_KEY_ENV = os.environ.get("SWARM_API_KEY_ENV") or {api_key_env_default_literal}\n'
            "\n"
            "\n"
            "def _resolve_default_model() -> Model:\n"
            "    return OpenAIChatModel(\n"
            "        _MODEL_ID,\n"
            "        provider=OpenAIProvider(\n"
            "            base_url=_BASE_URL,\n"
            '            api_key=os.environ.get(_API_KEY_ENV, "unset-placeholder-key"),\n'
            "        ),\n"
            "    )\n"
        )
        return ResolvedModel(
            helper_source=helper_source,
            default_factory_name="_resolve_default_model",
            extra_imports=(
                "from pydantic_ai.models.openai import OpenAIChatModel",
                "from pydantic_ai.providers.openai import OpenAIProvider",
            ),
            pyproject_extras=("openai",),
            # The exported project must name the route it inherited, so a
            # reader can see and change it without going back to the
            # canvas (PLAN.md acceptance criterion 8). All three are the
            # env vars `_resolve_default_model` above actually reads.
            env_lines=(
                f"SWARM_MODEL={effective.model}",
                f"SWARM_BASE_URL={base_url}",
                f"SWARM_API_KEY_ENV={effective.api_key_env or 'SWARM_API_KEY'}",
            ),
            readme_model_note=(
                f"Inherited default model: {effective.model} via custom endpoint "
                f"{base_url} (source: {effective.source}); override with "
                "SWARM_MODEL/SWARM_BASE_URL/SWARM_API_KEY_ENV."
            ),
        )

    # `or` rather than a `.get` default: a sourced `.env` with `SWARM_MODEL=`
    # leaves the variable set to "" and the generated project must still fall
    # back to its inherited default (found running a generated project for real).
    default_model_literal = repr(f"{emission.prefix}:{effective.model}")
    helper_source = (
        f"DEFAULT_MODEL: str = {default_model_literal}\n"
        "\n"
        "\n"
        "def _resolve_default_model() -> str:\n"
        '    return os.environ.get("SWARM_MODEL") or DEFAULT_MODEL\n'
    )
    return ResolvedModel(
        helper_source=helper_source,
        default_factory_name="_resolve_default_model",
        extra_imports=(),
        pyproject_extras=emission.extras,
        # As above: the inherited route is recorded in the exported
        # project's own .env.example, not only in its README.
        env_lines=(f"SWARM_MODEL={emission.prefix}:{effective.model}",),
        readme_model_note=(
            f"Inherited default model: {emission.prefix}:{effective.model} "
            f"(source: {effective.source}; override with SWARM_MODEL)."
        ),
    )


__all__ = [
    "LiveModel",
    "RouteEmission",
    "UnmappableRouteError",
    "build_live_model",
    "classify_route",
    "to_resolved_model",
]
