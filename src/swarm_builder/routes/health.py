"""``GET /api/health`` -- server version, resolved settings, and whether
a compile can currently succeed.

**Never cached.** Every ``config.get_*`` call and every
``inherit.settings`` call below happens fresh inside
:func:`get_health`, on every request. See ``routes/__init__.py``'s
module docstring for why: ``settings.yaml`` and the process environment
are both hot-reloadable while the server runs, and a snapshot taken at
app-construction time (or stashed on ``app.state``) would silently stop
reflecting a developer's edit until the process restarted.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from swarm_builder import __version__, runtime
from swarm_builder.appconfig import config_problems, load_config
from swarm_builder.config import (
    REPO_ROOT,
    get_app_config_path,
    get_dsh_home,
    get_uv_cache_dir,
    get_workspace_dir,
)
from swarm_builder.inherit.settings import (
    EffectiveModel,
    read_settings,
    resolve_effective_model,
)

router = APIRouter(tags=["health"])


class _CamelModel(BaseModel):
    """Local replica of ``models.py``'s ``SwarmBaseModel`` two-line
    config (camelCase JSON aliases, both spellings accepted on input).

    Deliberately NOT imported from ``models.py``: this file is not
    allowed to edit ``models.py``, and importing its base class would
    couple this response shape (a health report, not a graph document)
    to that module's ``extra="forbid"`` graph-document contract for no
    real benefit. Replicating two lines of ``ConfigDict`` here is
    cheaper than that coupling.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ResolvedModelSummary(_CamelModel):
    """The subset of :class:`~swarm_builder.inherit.settings.EffectiveModel`
    that is meaningful to a health check. ``reasoning_effort``,
    ``base_url``, ``api_key_env``, and ``route`` are internal resolution
    detail and are intentionally dropped here (the picker-facing detail
    lives in ``/api/models`` instead)."""

    provider: str
    model: str
    source: str


class HealthResponse(_CamelModel):
    """The full ``GET /api/health`` response body."""

    version: str
    dsh_home: str
    settings_error: str | None
    resolved_model: ResolvedModelSummary
    uv_available: bool
    uv_cache_dir: str
    uv_cache_writable: bool
    workspace_dir: str
    workspace_writable: bool
    web_dist_present: bool
    compile_ready: bool
    blockers: list[str]
    #: Whether a *run* (executing the compiled workflow with real
    #: credentials) is expected to work: every compile blocker, plus the
    #: resolved route's credential being present in this server's
    #: environment. ``run_blockers`` names each reason.
    run_ready: bool
    run_blockers: list[str]
    #: Dry run: the pipeline uses built-in stubs and keyless test models, so
    #: no credential is needed at all. ``dry_run_forced_by_env`` says the
    #: environment locked it on (the switch cannot turn it off).
    dry_run: bool
    dry_run_forced_by_env: bool
    #: Whether a model route is configured by the user (in this application's
    #: own model settings, an inherited settings file, or the environment).
    #: ``False`` means the resolved model is the internal offline stand-in
    #: that nobody chose.
    model_configured: bool
    #: Where this application's own model settings live, and why they could
    #: not be read when they exist but are broken.
    app_config_path: str
    app_config_error: str | None


def _path_writable(path: Path) -> bool:
    """Report whether a directory can be created and written to.

    Creates the directory if it is missing, then checks write access,
    converting any ``OSError`` (permission denied, read-only filesystem, a
    parent that is actually a file, ...) into ``False``: a health check
    must never itself raise just because a directory is unwritable, since
    that unwritability is the very fact being reported.

    Args:
        path: The directory to test.

    Returns:
        Whether the directory exists and is writable.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        return os.access(path, os.W_OK)
    except OSError:
        return False


@router.get("/health", response_model=HealthResponse)
def get_health() -> HealthResponse:
    """Report server version, resolved model/settings state, and whether
    a compile is currently expected to succeed.

    ``compile_ready`` is ``False`` -- with a human-readable string
    appended to ``blockers`` for each contributing reason -- whenever
    any of: no model has been configured *and* dry run is off; ``uv`` is
    not on ``PATH``; the resolved workspace directory is not writable;
    this application's own settings file exists but could not be read;
    or an inherited settings file exists but could not be parsed.
    Otherwise ``compile_ready`` is ``True`` and ``blockers`` is empty.

    Every message here is written for someone who has only ever seen this
    application: they name this application's own controls ("Model settings",
    "Dry run mode") rather than a file or variable belonging to something
    else. The inherited configuration is still read -- a user who has one
    keeps working exactly as before -- but it is never presented as a
    requirement.
    """
    dsh_home = get_dsh_home()
    settings = read_settings(dsh_home)
    settings_error = settings.error if settings is not None else None

    effective = resolve_effective_model(dsh_home)
    resolved_model = ResolvedModelSummary(
        provider=effective.provider, model=effective.model, source=effective.source
    )

    app_config = load_config()
    app_config_error = app_config.error if app_config is not None else None
    # An entry that parsed but cannot be used is reported too: resolution
    # ignores it in favour of the next source, which must not be silent.
    app_config_problems = config_problems(app_config)
    dry_run = runtime.dry_run_active(app_config)
    dry_run_forced = runtime.dry_run_forced_by_env()
    # "bundle-default" is the internal offline stand-in: it means nothing the
    # user chose, so it is reported as "not configured" rather than as a model.
    model_configured = effective.source != "bundle-default"

    uv_available = shutil.which("uv") is not None

    uv_cache_dir = get_uv_cache_dir()
    uv_cache_writable = _path_writable(uv_cache_dir)

    workspace_dir = get_workspace_dir()
    workspace_writable = _path_writable(workspace_dir)

    web_dist_present = (REPO_ROOT / "web" / "dist" / "index.html").exists()

    # A credential is needed by Phase 3's fill, not only by Run, so a missing
    # one is a compile blocker as well: enabling Compile and then failing on an
    # authentication error is a worse experience than being told up front. In
    # dry run nothing needs a credential, so neither check applies.
    missing_credential = None if dry_run else credential_blocker(effective)

    blockers: list[str] = []
    if not model_configured and not dry_run:
        blockers.append(
            "No model is configured yet. Choose a provider and add your API key "
            "in Model settings, or turn on Dry run mode to build and run offline."
        )
    if missing_credential is not None:
        blockers.append(missing_credential)
    if not uv_available:
        blockers.append("'uv' is not on PATH; the compile validation gate requires it.")
    if not workspace_writable:
        blockers.append(f"workspace directory is not writable: {workspace_dir}")

    # The two configuration-file problems are reported only when they actually
    # stand in the way. In dry run neither a broken app settings file nor a
    # broken inherited one changes what the pipeline does (the switch itself is
    # read from the environment when the file cannot be parsed), so blocking on
    # them would disable work that would succeed. They remain visible in
    # ``appConfigError``/``settingsError`` for the screen to show.
    if not dry_run:
        if app_config_error is not None:
            blockers.append(
                f"Swarm Builder's model settings could not be read: {app_config_error}"
            )
        elif app_config_problems:
            blockers.append(
                "Swarm Builder's model settings are not usable ("
                + "; ".join(app_config_problems)
                + ") — open Model settings to fix them, or turn on Dry run mode."
            )
        if settings_error is not None:
            # The advanced path only: this names a file the user owns, and it
            # is reported because a model they configured there is ignored.
            blockers.append(
                f"the inherited model settings file could not be read: {settings_error}"
            )

    # Every compile blocker is also a run blocker; a credential one is already
    # included above.
    run_blockers = list(blockers)

    return HealthResponse(
        version=__version__,
        dsh_home=str(dsh_home),
        settings_error=settings_error,
        resolved_model=resolved_model,
        uv_available=uv_available,
        uv_cache_dir=str(uv_cache_dir),
        uv_cache_writable=uv_cache_writable,
        workspace_dir=str(workspace_dir),
        workspace_writable=workspace_writable,
        web_dist_present=web_dist_present,
        compile_ready=not blockers,
        blockers=blockers,
        run_ready=not run_blockers,
        run_blockers=run_blockers,
        dry_run=dry_run,
        dry_run_forced_by_env=dry_run_forced,
        model_configured=model_configured,
        app_config_path=str(get_app_config_path()),
        app_config_error=app_config_error,
    )


#: Known-name model prefixes -> the env vars PydanticAI reads for them (any
#: one present counts). Only consulted when the route names no explicit
#: ``api_key_env``; a prefix absent here yields no blocker, because this
#: check must never wrongly disable Run for a provider it does not know.
#:
#: Kept in step with :data:`swarm_builder.providers.PROVIDERS` -- the in-app
#: model picker's registry, which the tests cross-check against this table --
#: so a provider a user can select in the UI is also one whose credential
#: state Run can report.
_PROVIDER_CREDENTIAL_ENV_VARS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    # The legacy spelling of the Gemini developer-API prefix, still accepted
    # for a route inherited from an older settings file.
    "google-gla": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "google-vertex": ("GOOGLE_APPLICATION_CREDENTIALS",),
    "bedrock": (
        "AWS_ACCESS_KEY_ID",
        "AWS_PROFILE",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    ),
}


def credential_blocker(effective: EffectiveModel) -> str | None:
    """Name the missing credential for the resolved route, or ``None``.

    A route with an explicit ``api_key_env`` needs exactly that variable. A
    known-name route (``<prefix>:<id>``) needs one of PydanticAI's own
    variables for that prefix. A bedrock-protocol route with an
    ``aws_profile`` is treated as configured.

    The message names the fix in this application's own terms -- "add one in
    Model settings" -- because that is where a user can actually act; the
    shell-export and ``.env`` alternatives are documented in the guide for
    people who chose the advanced path.
    """
    if effective.api_key:
        # A key configured in the app's own model settings: present by
        # construction (runtime.publish_secrets has already published it).
        return None

    if effective.api_key_env:
        if os.environ.get(effective.api_key_env):
            return None
        return (
            f"No API key for the {effective.provider!r} model yet. Add one in Model "
            f"settings, or turn on Dry run mode to work offline."
        )

    route = effective.route
    if route is not None and route.aws_profile:
        return None

    prefix: str | None = None
    if route is not None and route.api:
        prefix = route.api
    elif ":" in effective.model:
        prefix = effective.model.split(":", 1)[0]
    else:
        prefix = effective.provider

    candidates = _PROVIDER_CREDENTIAL_ENV_VARS.get(prefix or "")
    if not candidates:
        return None
    if any(os.environ.get(name) for name in candidates):
        return None
    return (
        f"No credential for the {effective.provider!r} model in this server's "
        f"environment. Add an API key in Model settings, or turn on Dry run mode to "
        f"work offline."
    )
