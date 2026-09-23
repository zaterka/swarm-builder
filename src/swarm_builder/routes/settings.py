"""``/api/settings`` -- the in-app model configuration screen's data source.

Three endpoints, all of which read and write **Swarm Builder's own** settings
file (``<workspace>/settings.json``, :mod:`swarm_builder.appconfig`) rather
than the inherited harness ``settings.yaml``:

- ``GET``  -- everything the screen needs to render: the provider catalog, the
  saved model (never its key, only whether one is stored and its last four
  characters), the dry-run switch and whether the environment has locked it,
  and what a compile would spend right now.
- ``PUT``  -- save a provider/model/key and/or the dry-run switch. Validated
  before anything is written, so a rejected form leaves the previous
  configuration untouched.
- ``POST /test`` -- one real, minimal model call, so a brand-new user learns
  whether their key works *before* spending a whole compile on it.

**Never cached**, like every other route module: the file is user-editable and
the switch is expected to take effect on the very next request (``routes/__init__.py``).

**The key never leaves the server.** It is written to a ``0600`` file, published
into this process's environment under the provider's own variable name (see
:mod:`swarm_builder.runtime`) so the in-process agent and every run subprocess
can use it, and reported back only as ``hasApiKey`` plus a four-character hint.
No response body in this module contains the value, and the test endpoint
scrubs it out of any message a provider returns.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from swarm_builder import runtime
from swarm_builder.appconfig import (
    AppConfig,
    AppModelConfig,
    apply_update,
    config_problems,
    load_config,
    spec_for,
    validate_model_config,
)
from swarm_builder.config import get_app_config_path, get_dsh_home, get_workspace_dir
from swarm_builder.inherit.routes import UnmappableRouteError, build_live_model
from swarm_builder.inherit.settings import read_settings, resolve_effective_model
from swarm_builder.providers import PROVIDERS, ProviderSpec
from swarm_builder.routes.health import _path_writable
from swarm_builder.routes.llm_routes import ResolvedDefaultOut

router = APIRouter(tags=["settings"])

#: Serializes the read-modify-write in :func:`put_settings`. Two overlapping
#: saves (a double click, or two tabs) would otherwise interleave: one reads
#: the previous model, the other writes its own version first, and the loser's
#: write silently discards the winner's model. Single-user or not, losing a
#: credential the user just typed is not an acceptable race.
_SETTINGS_LOCK = threading.RLock()

#: How long one Test-connection call may take before it is abandoned. Long
#: enough for a cold provider round trip, short enough that a user staring at
#: a spinner learns something.
TEST_TIMEOUT_SECONDS = 20.0

#: Longest provider error text echoed back to the UI. Providers occasionally
#: return whole HTML error pages; the screen is a settings form, not a log.
_MAX_DETAIL_CHARS = 600

#: The prompt the test call sends. Deliberately tiny: this endpoint exists to
#: prove a credential works, not to produce anything.
_TEST_PROMPT = "Reply with the single word: ok"


class _CamelModel(BaseModel):
    """Local replica of ``models.py``'s two-line camelCase config, as in every
    other route module's response shape (see ``routes/health.py``)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ProviderOut(_CamelModel):
    """One entry of the provider catalog the settings screen renders."""

    key: str
    label: str
    requires_api_key: bool
    requires_base_url: bool
    api_key_env: str | None
    default_model: str | None
    models: list[str]
    note: str | None
    #: Whether *this server* can currently build a live model for the
    #: provider. Every provider the picker offers is pinned in
    #: ``pyproject.toml``, so this is normally true; it exists so a
    #: stripped-down install explains itself instead of failing at compile
    #: time, and so the custom endpoint is honest about needing a base URL.
    usable: bool
    unusable_reason: str | None


class SavedModelOut(_CamelModel):
    """The saved model, minus the secret.

    ``api_key_hint`` is the last four characters of the stored key -- enough
    for a user to tell two keys apart, not enough to use one. The value itself
    is never part of any response body.
    """

    provider: str
    model: str
    base_url: str | None
    reasoning_effort: str | None
    has_api_key: bool
    api_key_hint: str | None


class SettingsResponse(_CamelModel):
    """The full ``GET /api/settings`` body."""

    config_path: str
    config_error: str | None
    model: SavedModelOut | None
    dry_run: bool
    dry_run_forced_by_env: bool
    dry_run_env_vars: list[str]
    providers: list[ProviderOut]
    resolved_default: ResolvedDefaultOut
    #: How many routes an inherited ``settings.yaml`` contributes, so the
    #: advanced footer can say "also inheriting N routes" without pretending
    #: the file is required.
    inherited_routes: int
    workspace_writable: bool


class ModelInput(_CamelModel):
    """One provider/model/key submission.

    An omitted ``api_key`` keeps the stored key when the provider is
    unchanged; ``clear_api_key`` removes it; a non-empty ``api_key`` replaces
    it (:func:`swarm_builder.appconfig.apply_update`).
    """

    provider: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    #: ``None`` (the omitted case) means "keep the stored key"; ``True``
    #: removes it. Deliberately ``bool | None`` rather than ``bool = False``:
    #: a field with a default is published as *required* by the OpenAPI
    #: schema generator, which would force every caller -- including one that
    #: only wants to change the dry-run switch -- to spell out a field it does
    #: not care about.
    clear_api_key: bool | None = None
    reasoning_effort: str | None = None


class SettingsUpdateRequest(_CamelModel):
    """Body of ``PUT /api/settings``.

    ``None`` means "leave this alone", which is what lets the dry-run switch be
    toggled without resubmitting the model form (and vice versa). Removing the
    in-app model entirely is the explicit ``clear_model`` flag rather than an
    ambiguous ``model: null``.
    """

    model: ModelInput | None = None
    dry_run: bool | None = None
    #: ``True`` removes the in-app model entirely. ``bool | None`` for the
    #: same reason as :attr:`ModelInput.clear_api_key`.
    clear_model: bool | None = None


class TestConnectionRequest(_CamelModel):
    """Body of ``POST /api/settings/test`` -- all fields optional.

    Anything omitted falls back to the saved configuration, so the common
    case (``{}``) tests exactly what a compile would use. Supplying fields is
    how the screen tests an unsaved form.
    """

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class TestConnectionResponse(_CamelModel):
    """The result of one test call.

    A provider refusing the key, timing out, or being unreachable are all
    *results*, not HTTP errors: the screen shows the detail inline next to the
    form the user needs to fix.
    """

    ok: bool
    detail: str
    latency_ms: int | None
    model: str


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


#: Shortest key that may be partially echoed. Below this, four characters of
#: a short token are a meaningful fraction of the secret itself, so no hint is
#: shown at all -- ``hasApiKey`` already tells the user that one is stored.
_MIN_HINTABLE_KEY_CHARS = 12


def _api_key_hint(api_key: str | None) -> str | None:
    """The last four characters of a key, prefixed with an ellipsis.

    Returns ``None`` for a key short enough that a four-character tail would
    leak a usable part of it (a local gateway token, a test fixture). The hint
    exists so a user can tell two long keys apart, not to render any secret in
    part.
    """
    if not api_key or len(api_key) < _MIN_HINTABLE_KEY_CHARS:
        return None
    return f"…{api_key[-4:]}"


def _saved_model_out(model: AppModelConfig | None) -> SavedModelOut | None:
    """Render the saved model without its secret."""
    if model is None:
        return None
    return SavedModelOut(
        provider=model.provider,
        model=model.model,
        base_url=model.base_url,
        reasoning_effort=model.reasoning_effort,
        has_api_key=bool(model.api_key),
        api_key_hint=_api_key_hint(model.api_key),
    )


def _missing_provider_module(spec: ProviderSpec) -> str | None:
    """The provider package this install lacks, or ``None`` when present.

    Mirrors ``inherit.routes._resolve_emission``'s import check so the catalog
    the screen shows and the compile path agree about what is usable.
    """
    from swarm_builder.inherit.routes import _EXTRA_IMPORT_MODULES

    if spec.known_name_prefix is None:
        # The custom endpoint is built structurally through the openai extra,
        # which this project always pins.
        return None
    extra = {"google": "google", "groq": "groq", "mistral": "mistral"}.get(spec.key)
    if extra is None:
        return None
    module = _EXTRA_IMPORT_MODULES.get(extra)
    if module is None:
        return None
    try:
        found = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        found = None
    return None if found is not None else module


def _provider_out(spec: ProviderSpec, saved: AppModelConfig | None) -> ProviderOut:
    """Render one catalog entry, including whether this server can run it."""
    missing = _missing_provider_module(spec)
    usable = missing is None
    reason: str | None = None
    if missing is not None:
        usable = False
        reason = f"this installation is missing the '{missing}' package; run `uv sync`"
    elif spec.requires_base_url and not (
        saved is not None and saved.provider == spec.key and saved.base_url
    ):
        usable = False
        reason = "a base URL is required"

    return ProviderOut(
        key=spec.key,
        label=spec.label,
        requires_api_key=spec.requires_api_key,
        requires_base_url=spec.requires_base_url,
        api_key_env=spec.api_key_env,
        default_model=spec.default_model,
        models=list(spec.models),
        note=spec.note,
        usable=usable,
        unusable_reason=reason,
    )


def _resolved_default() -> ResolvedDefaultOut:
    """What a compile would spend with the current configuration."""
    effective = resolve_effective_model(get_dsh_home())
    return ResolvedDefaultOut(
        provider=effective.provider, model=effective.model, source=effective.source
    )


def _settings_response(cfg: AppConfig | None) -> SettingsResponse:
    """Build the shared GET/PUT response body from one config read."""
    saved = cfg.model if cfg is not None else None

    inherited_routes = 0
    settings = read_settings(get_dsh_home())
    if settings is not None and settings.error is None:
        inherited_routes = len(settings.routes)

    # One field for "why is what I saved not in effect": a read failure or an
    # entry that cannot be used. Resolution ignores either, so the screen has
    # to say so rather than let a fallthrough look like success.
    problems = config_problems(cfg)
    return SettingsResponse(
        config_path=str(get_app_config_path()),
        config_error="; ".join(problems) if problems else None,
        model=_saved_model_out(saved),
        dry_run=runtime.dry_run_active(cfg),
        dry_run_forced_by_env=runtime.dry_run_forced_by_env(),
        dry_run_env_vars=runtime.dry_run_forced_env_vars(),
        providers=[_provider_out(spec, saved) for spec in PROVIDERS],
        resolved_default=_resolved_default(),
        inherited_routes=inherited_routes,
        workspace_writable=_path_writable(get_workspace_dir()),
    )


def _scrub(text: str, secrets: list[str | None]) -> str:
    """Remove anything key-shaped from a message we are about to return.

    Providers do not normally echo a credential, but a proxy in front of one
    can, and this text is rendered in a browser and often pasted into a bug
    report. Two passes: every credential actually in play is replaced
    verbatim, then a regex sweeps up token-shaped strings the exact match
    missed (a provider that truncates an echoed key, for instance).
    """
    import re

    cleaned = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            cleaned = cleaned.replace(secret, "***")
    cleaned = re.sub(r"\b(?:sk|xai|gsk|AIza)[-_A-Za-z0-9]{8,}\b", "***", cleaned)
    if len(cleaned) > _MAX_DETAIL_CHARS:
        cleaned = cleaned[:_MAX_DETAIL_CHARS] + " …"
    return cleaned


def _credentials_in_play(candidate: AppModelConfig, spec: ProviderSpec | None) -> list[str | None]:
    """Every secret value that must not survive into a response.

    A key pasted into the form is the obvious one, but the documented
    advanced workflow leaves the field empty and relies on the provider's
    variable in the server's environment -- and a provider or an HTTP client
    error can echo the request it sent. Both are collected here so `_scrub`
    has them.
    """
    values: list[str | None] = [candidate.api_key]
    if spec is not None and spec.api_key_env:
        values.append(os.environ.get(spec.api_key_env))
    return values


# ---------------------------------------------------------------------------
# GET / PUT
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=SettingsResponse)
def get_settings() -> SettingsResponse:
    """Everything the model-settings screen renders.

    Reads the file fresh: a user can edit ``workspace/settings.json`` (or flip
    the dry-run switch from another tab) between two requests.
    """
    return _settings_response(load_config())


@router.put(
    "/settings",
    response_model=SettingsResponse,
    responses={
        422: {"description": "the submitted provider/model/key combination is not usable"},
        500: {"description": "the settings file could not be written"},
    },
)
def put_settings(body: SettingsUpdateRequest) -> SettingsResponse:
    """Save the in-app model configuration and/or the dry-run switch.

    Validation happens before anything is written, so a rejected submission
    leaves the previous configuration exactly as it was. On success the
    credential is published into this server's environment immediately (see
    :func:`swarm_builder.runtime.publish_secrets`), which is what makes the
    change take effect for the next generate/compile/run without a restart.

    Raises:
        HTTPException: 422 with every problem found, or 500 when the settings
            file cannot be written (an unwritable workspace).
    """
    with _SETTINGS_LOCK:
        return _put_settings_locked(body)


def _put_settings_locked(body: SettingsUpdateRequest) -> SettingsResponse:
    """The body of :func:`put_settings`, under the settings lock."""
    current = load_config()
    previous_model = current.model if current is not None else None
    previous_dry_run = current.dry_run if current is not None else False

    if body.clear_model is True:
        new_model: AppModelConfig | None = None
    elif body.model is not None:
        new_model = apply_update(
            previous_model,
            body.model.provider,
            body.model.model,
            base_url=body.model.base_url,
            api_key=body.model.api_key,
            clear_api_key=body.model.clear_api_key is True,
            reasoning_effort=body.model.reasoning_effort,
        )
        problems = validate_model_config(new_model)
        if problems:
            raise HTTPException(status_code=422, detail=problems)
    else:
        new_model = previous_model

    dry_run = previous_dry_run if body.dry_run is None else body.dry_run

    try:
        from swarm_builder.appconfig import save_config

        save_config(AppConfig(model=new_model, dry_run=dry_run))
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Swarm Builder could not write its model settings to "
                f"{get_app_config_path()}: {exc}. Check that the workspace "
                f"directory is writable."
            ),
        ) from exc

    runtime.publish_secrets()
    return _settings_response(load_config())


# ---------------------------------------------------------------------------
# POST /api/settings/test
# ---------------------------------------------------------------------------


@router.post("/settings/test", response_model=TestConnectionResponse)
async def test_connection(body: TestConnectionRequest) -> TestConnectionResponse:
    """Make one minimal model call and report whether it worked.

    A brand-new user's first question is "did I paste the key correctly?", and
    the honest answer requires a real call -- the keyless validation gate a
    compile runs cannot answer it, because it injects a test model on purpose.

    Every provider-side outcome (a refused key, a timeout, an unmappable
    route, nothing configured at all) is a ``200`` with ``ok: false`` and a
    human-readable ``detail``; only a structurally invalid request is a
    ``422``. Nothing here is persisted: testing an unsaved form must not
    change the saved configuration.
    """
    cfg = load_config()
    stored = cfg.model if cfg is not None else None

    if body.provider is not None:
        # The screen is testing an unsaved form: merge it onto the stored
        # configuration so an omitted key still means "the one I saved".
        candidate = apply_update(
            stored,
            body.provider,
            body.model or "",
            base_url=body.base_url,
            api_key=body.api_key,
            reasoning_effort=None,
        )
    else:
        candidate = stored

    if candidate is None:
        # Nothing saved here, but an inherited settings file or SWARM_MODEL may
        # still describe a route. Testing *that* is more useful than refusing,
        # and the advanced-path user gets the same button as everyone else.
        resolved = resolve_effective_model(get_dsh_home())
        if resolved.source == "bundle-default":
            return TestConnectionResponse(
                ok=False,
                detail=(
                    "No model is configured yet. Choose a provider and add your API "
                    "key, then test the connection."
                ),
                latency_ms=None,
                model="",
            )
        candidate = AppModelConfig(
            provider=resolved.provider,
            model=resolved.model,
            base_url=resolved.base_url,
            api_key=resolved.api_key,
        )

    if runtime.dry_run_active(cfg):
        return TestConnectionResponse(
            ok=False,
            detail=(
                "Dry run mode is on: the pipeline uses built-in stub models, so "
                "there is no live provider to test. Turn dry run off to test a "
                "connection."
            ),
            latency_ms=None,
            model=candidate.label,
        )

    problems = validate_model_config(candidate)
    if problems:
        return TestConnectionResponse(
            ok=False,
            detail=" ".join(problems),
            latency_ms=None,
            model=candidate.label,
        )

    spec = spec_for(candidate)
    if (
        candidate.api_key is None
        and spec is not None
        and spec.api_key_env is not None
        and not os.environ.get(spec.api_key_env)
    ):
        # Nothing to authenticate with: a *result* to show the user, not an
        # error to raise, and it names where the fix belongs.
        return TestConnectionResponse(
            ok=False,
            detail=(
                f"No API key for {spec.label!r} yet. Paste one above, or set "
                f"{spec.api_key_env} in the environment that starts the server."
            ),
            latency_ms=None,
            model=candidate.label,
        )

    # PydanticAI resolves a known-name model by reading the provider's own
    # environment variable, so the key under test is published for the
    # duration of this call and restored afterwards -- a rejected key must not
    # be left live in the process.
    overrides: dict[str, str] = {}
    if candidate.api_key and spec is not None and spec.api_key_env:
        overrides[spec.api_key_env] = candidate.api_key

    effective = resolve_effective_model(
        get_dsh_home(), app_config=AppConfig(model=candidate)
    )
    started = time.monotonic()

    try:
        # Both the model construction and the call happen inside the override
        # window: a known-name provider resolves its credential lazily, and a
        # structural one is handed an explicit key at construction.
        with runtime.temporary_secret_env(overrides):
            try:
                live = build_live_model(effective)
            except UnmappableRouteError as exc:
                return TestConnectionResponse(
                    ok=False,
                    detail=_scrub(str(exc), _credentials_in_play(candidate, spec)),
                    latency_ms=None,
                    model=candidate.label,
                )
            await _ping(live.model)
    except TimeoutError:
        return TestConnectionResponse(
            ok=False,
            detail=f"the provider did not answer within {int(TEST_TIMEOUT_SECONDS)} seconds",
            latency_ms=None,
            model=candidate.label,
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised: see docstring
        return TestConnectionResponse(
            ok=False,
            detail=_scrub(
                f"{type(exc).__name__}: {exc}", _credentials_in_play(candidate, spec)
            ),
            latency_ms=None,
            model=candidate.label,
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return TestConnectionResponse(
        ok=True,
        detail=f"the provider answered with {candidate.model!r}",
        latency_ms=elapsed_ms,
        model=candidate.label,
    )


async def _ping(model: object) -> None:
    """Send one tiny request to ``model`` and require a reply.

    Lazily imported: ``pydantic_ai`` (and the provider package behind the
    chosen model) is heavy, and this module is imported by ``main.py`` at
    startup for its router.
    """
    from pydantic_ai import Agent

    agent = Agent(model, instructions="Answer with one word.")  # type: ignore[arg-type]
    await asyncio.wait_for(agent.run(_TEST_PROMPT), timeout=TEST_TIMEOUT_SECONDS)
