"""Reading harness ``settings.yaml`` and resolving the effective model.

**Why this module exists.** Swarm Builder never pins a model of its own.
It inherits whatever the Factored Harness's user has configured, by
reading ``$DSH_HOME/settings.yaml`` directly as plain YAML -- the
harness's own SDK wire protocol exposes no ``listModels`` method, so this
is the only channel that exists. The real file mixes many unrelated
top-level sections
(``ui-onboarding``, ``agent-presets``, ``ui-theme``,
``dev-mode-pipeline``, ...) with the two sections this module actually
cares about: ``llm-pi-ai.providers`` (the configured routes) and
``agent-default-model`` (the default selection). Tolerating
every unrecognized key -- at the top level and within a route entry --
is therefore a hard requirement, not a nicety: a strict schema that
rejected unknown keys would break the moment the harness added an
unrelated settings section, which happens routinely since the file is
hand-edited and versioned independently of this app. That is why this
module parses with plain ``yaml.safe_load`` into dicts and reads out
only the handful of keys it needs, rather than defining a pydantic model
with ``extra="forbid"`` the way :mod:`swarm_builder.models` does for the
graph document (that document is *this app's* schema; this file is
someone else's).

**Why nothing here is ever cached.** ``settings.yaml`` is hot-reloaded
and user-editable -- a user can change their default model between two
compiles in the same running server process. Every public function in
this module re-reads the file from disk on every call. There is no
module-level state, no ``functools.lru_cache``, nothing to invalidate.

**Never-splat, and why.** Every dataclass in this module is constructed
with explicit keyword arguments pulled individually out of a parsed
dict -- never ``SomeDataclass(**mapping)``. The real ``amazon-bedrock``
route's ``models:`` entries carry ``contextWindow``/``maxTokens`` keys
that :class:`ModelInfo` does not declare; splatting such an entry into
``ModelInfo(**entry)`` would raise an unhandled ``TypeError`` for a
perfectly valid, if unusually-shaped, settings file. Reading out exactly
``id``/``name`` and ignoring the rest is both simpler and is *how*
"unknown keys are tolerated" actually happens.

**Scalar coercion, and why it is not optional.** Plain ``@dataclass``
does not type-check its arguments, so a YAML scalar that parses to the
"wrong" Python type does not raise -- it silently produces a field
holding an ``int``/``float``/``bool`` where a ``str`` was expected. Two
concrete failure shapes motivate :func:`_coerce_str`:

- YAML 1.1 treats bare ``on``/``off``/``yes``/``no``/``true``/``false``
  as booleans. A provider literally named ``on`` in someone's
  ``providers:`` map would otherwise become the dict key ``True``, and
  since ``isinstance(True, int)`` is ``True`` in Python, any coercion
  rule that checks ``int`` before excluding ``bool`` would render it as
  the string ``"True"`` -- unrecoverably wrong, and worse than simply
  dropping the entry. :func:`_coerce_str` therefore excludes ``bool``
  *before* accepting ``int``/``float``.
- A model ``id: 3.5`` or an ``agent-default-model.model: 3.5`` parses to
  a Python ``float``. Silently discarding a value the user actually
  configured (because it happened to look numeric) would mean falling
  back to the bundle default without telling anyone, which is the silent
  mis-routing this module exists to prevent entirely. So non-``bool``
  scalars are coerced with
  ``str(value)`` rather than discarded, and only ``None``/empty-after-
  strip values count as genuinely absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from swarm_builder import config

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelInfo:
    """One entry from a route's explicit ``models:`` list.

    Most routes -- anything serving a full catalog rather than a fixed
    curated set -- have no ``models:`` list at all; this shape only exists
    for the routes that do enumerate one, such as a self-hosted
    OpenAI-compatible endpoint or a single-region Bedrock route.
    """

    id: str
    name: str | None


@dataclass(frozen=True)
class RouteConfig:
    """One entry from ``llm-pi-ai.providers`` in ``settings.yaml``.

    ``key`` is the settings map key itself (e.g. ``"amazon-bedrock"``,
    ``"kornerstone"``), which is also what a graph's model override and
    the ``agent-default-model`` section's ``provider`` field are matched
    against by :func:`resolve_effective_model`.

    ``api`` is the wire protocol, either explicit or inferred: a real
    ``amazon-bedrock`` route often has no explicit ``api:`` key at all,
    only ``awsProfile``/``awsRegion`` -- the harness itself infers
    ``bedrock-converse-stream`` from those fields being present, and this
    module reproduces that inference so callers never have to special-case
    a bedrock route with a missing ``api``. A route with
    neither an explicit ``api`` nor any AWS field has a genuinely
    undeterminable protocol, so ``api`` is ``None`` rather than a guess.
    """

    key: str
    api: str | None
    base_url: str | None
    api_key_env: str | None
    aws_profile: str | None
    aws_region: str | None
    models: tuple[ModelInfo, ...]


@dataclass(frozen=True)
class AgentDefaultModel:
    """The ``agent-default-model`` section: the harness's own configured
    default selection, independent of any particular route's catalog.

    Absent entirely is a normal state, not an error: it means the
    authoritative default lives in the harness's base bundle patch rather
    than in this user's settings file, so Swarm Builder falls back
    further (to env configuration, then to the bundle default).
    """

    provider: str
    model: str
    reasoning_effort: str | None


@dataclass(frozen=True)
class Settings:
    """The result of one :func:`read_settings` call.

    ``error`` distinguishes two very different "nothing useful here"
    outcomes that must never be conflated:

    - the file does not exist at all (a normal state -- the harness may
      not be installed) -- this is reported by :func:`read_settings`
      returning ``None``, not by this dataclass;
    - the file exists but is not valid YAML, or could not be read for a
      permissions reason -- a real user mistake that must surface, not
      be silently treated the same as "no harness installed". This is
      reported by returning a ``Settings`` with ``error`` set to a
      non-empty string naming the settings path and the underlying
      exception, and with ``routes=()``/``agent_default_model=None``.

    A third, easily-confused-with-the-second outcome is a file that
    *is* valid YAML but is empty, a bare scalar, a list, or has one of
    its two relevant sections in an unexpected shape (e.g.
    ``llm-pi-ai.providers`` given as a list instead of a mapping). None
    of those are YAML syntax errors -- ``yaml.safe_load`` accepts all of
    them -- so none of them set ``error``; they simply produce empty
    results for whichever section was mis-shaped. This matches the
    "no providers configured" state, which is normal and reportable by the
    health endpoint rather than a parse failure.
    """

    routes: tuple[RouteConfig, ...]
    agent_default_model: AgentDefaultModel | None
    error: str | None = None


@dataclass(frozen=True)
class EffectiveModel:
    """The outcome of :func:`resolve_effective_model`.

    One concrete model selection plus enough provenance (``source``,
    ``route``) for a caller to report *why* this particular model will be
    spent: a compile must never silently spend credentials on a route the
    user did not expect, so the resolution path is part of the result
    rather than an implementation detail.
    """

    provider: str
    model: str
    reasoning_effort: str | None
    base_url: str | None
    api_key_env: str | None
    source: Literal["graph-override", "settings-default", "env-fallback", "bundle-default"]
    route: RouteConfig | None


#: The last-resort selection: when neither a graph override, a
#: settings-configured default, nor env configuration is available, this
#: exact pair is what Swarm Builder falls back to. Deliberately pinned
#: rather than derived, so the fallback is a known quantity a user can
#: reason about instead of an implementation detail that drifts with
#: whatever happens to be installed.
_BUNDLE_DEFAULT_PROVIDER = "deepseek-official"
_BUNDLE_DEFAULT_MODEL = "deepseek-v4-flash"


# ---------------------------------------------------------------------------
# Scalar coercion
# ---------------------------------------------------------------------------


def _coerce_str(value: object) -> str | None:
    """Coerce one parsed YAML scalar to a non-empty ``str``, or ``None``.

    ``bool`` is rejected before the ``int``/``float`` branch is reached,
    even though ``isinstance(True, int)`` is ``True`` in Python --
    see the module docstring's "Scalar coercion" section for why a
    boolean-valued key or scalar cannot be usefully recovered as a
    string and is therefore treated as absent rather than misrendered.
    ``None`` and an empty-or-whitespace-only string are likewise
    treated as absent, so a key present in the YAML with an explicit
    ``null`` value coerces the same as the key being missing entirely.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str | int | float):
        text = str(value).strip()
        return text or None
    return None


# ---------------------------------------------------------------------------
# Route / default-model parsing
# ---------------------------------------------------------------------------


def _parse_model_info(raw: object) -> ModelInfo | None:
    """Parse one entry of a route's ``models:`` list.

    A ``models:`` list entry that is not a mapping (a bare string,
    ``null``, a number) or that has no usable ``id`` after coercion is
    skipped rather than raising, so one malformed model entry never
    takes down the whole settings read.
    """
    if not isinstance(raw, dict):
        return None
    model_id = _coerce_str(raw.get("id"))
    if model_id is None:
        return None
    return ModelInfo(id=model_id, name=_coerce_str(raw.get("name")))


def _parse_route(key: str, raw: object) -> RouteConfig | None:
    """Parse one entry of ``llm-pi-ai.providers``.

    Returns ``None`` when ``raw`` is not a mapping at all (a route value
    given as a scalar or list) -- there is nothing sensible to extract,
    and this is the same "tolerate a mis-shaped section without
    erroring" policy documented on :class:`Settings`.
    """
    if not isinstance(raw, dict):
        return None

    api = _coerce_str(raw.get("api"))
    aws_profile = _coerce_str(raw.get("awsProfile"))
    aws_region = _coerce_str(raw.get("awsRegion"))
    if api is None and (aws_profile is not None or aws_region is not None):
        # The harness itself infers this protocol from the presence of
        # AWS fields when `api` is omitted: a bedrock route need not
        # repeat `api:` explicitly, so neither may we require it.
        api = "bedrock-converse-stream"

    raw_models = raw.get("models")
    models: tuple[ModelInfo, ...]
    if isinstance(raw_models, list):
        models = tuple(
            info for info in (_parse_model_info(entry) for entry in raw_models) if info is not None
        )
    else:
        # No `models:` key at all (the routine case) or a `models:` value
        # that is not a list -- both produce an empty tuple, never an
        # error, since the route is still usable without a catalog.
        models = ()

    return RouteConfig(
        key=key,
        api=api,
        base_url=_coerce_str(raw.get("baseURL")),
        api_key_env=_coerce_str(raw.get("apiKeyEnv")),
        aws_profile=aws_profile,
        aws_region=aws_region,
        models=models,
    )


def _parse_routes(document: dict[str, object]) -> tuple[RouteConfig, ...]:
    """Parse ``llm-pi-ai.providers`` out of the full settings document.

    Every level of this path is defensively checked: a document whose
    ``llm-pi-ai`` key is missing or not a mapping, or whose ``providers``
    key is missing or not a mapping (e.g. given as a list), yields no
    routes at all rather than raising -- the "no providers configured"
    state, which is normal and reportable rather than a parse error.
    """
    llm_pi_ai = document.get("llm-pi-ai")
    if not isinstance(llm_pi_ai, dict):
        return ()
    providers = llm_pi_ai.get("providers")
    if not isinstance(providers, dict):
        return ()

    routes: list[RouteConfig] = []
    for raw_key, raw_val in providers.items():
        key = _coerce_str(raw_key)
        if key is None:
            # A bool/null/empty-string provider key (YAML 1.1 lets an
            # unquoted `on`/`off`/etc. become a map key) cannot be
            # usefully matched by a caller's provider string later, so
            # it is dropped rather than misrendered.
            continue
        route = _parse_route(key, raw_val)
        if route is not None:
            routes.append(route)
    return tuple(routes)


def _parse_agent_default_model(document: dict[str, object]) -> AgentDefaultModel | None:
    """Parse the ``agent-default-model`` section.

    Returns ``None`` when the section is missing, not a mapping, or lacks
    a usable (post-coercion) ``provider``/``model`` pair -- all of which
    mean the same normal "no configured default" state, not an error.
    """
    raw = document.get("agent-default-model")
    if not isinstance(raw, dict):
        return None
    provider = _coerce_str(raw.get("provider"))
    model = _coerce_str(raw.get("model"))
    if provider is None or model is None:
        return None
    return AgentDefaultModel(
        provider=provider,
        model=model,
        reasoning_effort=_coerce_str(raw.get("reasoningEffort")),
    )


# ---------------------------------------------------------------------------
# read_settings
# ---------------------------------------------------------------------------


def read_settings(dsh_home: Path) -> Settings | None:
    """Read ``<dsh_home>/settings.yaml``.

    Returns ``None`` only when the file does not exist at all -- a normal
    state, since the harness may not be installed on this machine, which
    is distinct from a misconfigured one. Returns a :class:`Settings` in
    every other case:
    with ``error`` set when the file exists but is unparsable YAML or
    unreadable for a permissions/filesystem reason, or with ``error``
    left ``None`` -- possibly alongside empty ``routes``/
    ``agent_default_model`` -- when the file parses fine but is empty,
    a bare scalar/list, or has one of its two relevant sections in an
    unexpected shape. Never raises: every exception this function's own
    I/O and parsing can produce is caught and translated into one of
    the return shapes above. Never caches: this function reads the file
    from disk fresh on every call, since ``settings.yaml`` is hot-
    reloaded and user-editable.
    """
    path = Path(dsh_home) / "settings.yaml"

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Checked before the general OSError branch below: this is a
        # subclass of OSError, and ordering here is load-bearing --
        # "file absent" must never be swallowed into "file unreadable".
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return Settings(
            routes=(),
            agent_default_model=None,
            error=f"failed to read {path}: {exc}",
        )

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return Settings(
            routes=(),
            agent_default_model=None,
            error=f"failed to parse {path}: {exc}",
        )

    # `yaml.safe_load` happily accepts an empty file (-> None), a bare
    # scalar (-> str/int/...), or a top-level list -- none of those are
    # YAML syntax errors, so none of them set `error`; they simply carry
    # no routes and no default, the same as a document missing the
    # relevant sections entirely.
    mapping = document if isinstance(document, dict) else {}

    return Settings(
        routes=_parse_routes(mapping),
        agent_default_model=_parse_agent_default_model(mapping),
        error=None,
    )


# ---------------------------------------------------------------------------
# resolve_effective_model
# ---------------------------------------------------------------------------


def _find_route(routes: tuple[RouteConfig, ...], provider: str) -> RouteConfig | None:
    """Case-sensitive exact match of ``provider`` against ``RouteConfig.key``."""
    return next((route for route in routes if route.key == provider), None)


def resolve_effective_model(
    dsh_home: Path,
    graph_override: tuple[str, str, str | None] | None = None,
) -> EffectiveModel:
    """Resolve which model a compile will actually spend.

    Re-reads ``settings.yaml`` fresh on every call via :func:`read_settings`
    (never cached, for the same hot-reload reason that function documents)
    and applies this exact precedence:

    1. ``graph_override``, if not ``None`` -- a ``(provider, model,
       reasoning_effort)`` tuple standing in for a graph's own
       ``ModelSelection`` (this module has no dependency on
       ``models.py``; callers convert their ``ModelSelection`` to this
       tuple shape themselves). ``source="graph-override"``. The
       override's provider/model/reasoning_effort are used verbatim
       regardless of whether a matching route exists -- an override
       naming an unconfigured provider still wins; only ``route`` (and
       therefore ``base_url``/``api_key_env``) come back empty in that
       case.
    2. Else the settings file's ``agent-default-model`` section, when
       :func:`read_settings` returned a :class:`Settings` with
       ``agent_default_model`` set (which requires the file to be
       present, parse without ``error``, and have the section).
       ``source="settings-default"``. A settings read that came back
       ``None`` (file absent) or with ``error`` set is treated the same
       as "no settings default available" here -- resolution simply
       falls through to step 3, it never raises and never treats a
       broken settings file as if it were a configured empty default.
    3. Else the ``SWARM_MODEL`` env var (via
       ``config.get_swarm_model()``, which already normalizes an unset
       or empty string to ``None``), if set. ``source="env-fallback"``,
       ``route=None`` always (there is no settings route corresponding
       to an env-var fallback).
       - If ``SWARM_BASE_URL`` (``config.get_swarm_base_url()``) is also
         set: ``provider="custom"``, ``model`` is the raw
         ``SWARM_MODEL`` value unsplit, ``base_url`` is that base URL,
         ``api_key_env`` is ``config.get_swarm_api_key_env()`` (may be
         ``None``).
       - Else: ``base_url=None``, ``api_key_env=None``, and
         ``SWARM_MODEL`` is split on the first ``:`` into
         provider/model for reporting purposes only. No ``:`` at all,
         or an empty provider before the first ``:`` (e.g.
         ``":foo"``), both report ``provider="env"``.
    4. Else the bundle default -- ``source="bundle-default"``,
       ``route=None``, ``base_url=None``, ``api_key_env=None``,
       ``provider="deepseek-official"``, ``model="deepseek-v4-flash"``,
       ``reasoning_effort=None``. That exact pair is pinned in
       :data:`_BUNDLE_DEFAULT_PROVIDER`/:data:`_BUNDLE_DEFAULT_MODEL` and
       must never be substituted.
    """
    settings = read_settings(dsh_home)
    routes = settings.routes if settings is not None and settings.error is None else ()

    if graph_override is not None:
        provider, model, reasoning_effort = graph_override
        route = _find_route(routes, provider)
        return EffectiveModel(
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            base_url=route.base_url if route is not None else None,
            api_key_env=route.api_key_env if route is not None else None,
            source="graph-override",
            route=route,
        )

    default = (
        settings.agent_default_model
        if settings is not None and settings.error is None
        else None
    )
    if default is not None:
        route = _find_route(routes, default.provider)
        return EffectiveModel(
            provider=default.provider,
            model=default.model,
            reasoning_effort=default.reasoning_effort,
            base_url=route.base_url if route is not None else None,
            api_key_env=route.api_key_env if route is not None else None,
            source="settings-default",
            route=route,
        )

    raw_model = config.get_swarm_model()
    if raw_model is not None:
        base_url = config.get_swarm_base_url()
        if base_url is not None:
            return EffectiveModel(
                provider="custom",
                model=raw_model,
                reasoning_effort=None,
                base_url=base_url,
                api_key_env=config.get_swarm_api_key_env(),
                source="env-fallback",
                route=None,
            )

        if ":" in raw_model:
            provider, model = raw_model.split(":", 1)
            if not provider:
                provider = "env"
        else:
            provider, model = "env", raw_model
        return EffectiveModel(
            provider=provider,
            model=model,
            reasoning_effort=None,
            base_url=None,
            api_key_env=None,
            source="env-fallback",
            route=None,
        )

    return EffectiveModel(
        provider=_BUNDLE_DEFAULT_PROVIDER,
        model=_BUNDLE_DEFAULT_MODEL,
        reasoning_effort=None,
        base_url=None,
        api_key_env=None,
        source="bundle-default",
        route=None,
    )
