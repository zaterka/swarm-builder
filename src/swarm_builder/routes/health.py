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

from swarm_builder import __version__
from swarm_builder.config import (
    REPO_ROOT,
    get_dsh_home,
    get_uv_cache_dir,
    get_workspace_dir,
)
from swarm_builder.inherit.settings import read_settings, resolve_effective_model

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
    """Report server version, resolved model/settings state, and
    whether a compile is currently expected to succeed.

    ``compile_ready`` is ``False`` -- with a human-readable string
    appended to ``blockers`` for each contributing reason -- whenever
    any of: the resolved default model came from the bundle fallback
    (nothing the user actually configured); ``uv`` is not on ``PATH``;
    the resolved workspace directory is not writable; or
    ``settings.yaml`` exists but failed to parse/read. Otherwise
    ``compile_ready`` is ``True`` and ``blockers`` is empty.
    """
    dsh_home = get_dsh_home()
    settings = read_settings(dsh_home)
    settings_error = settings.error if settings is not None else None

    effective = resolve_effective_model(dsh_home)
    resolved_model = ResolvedModelSummary(
        provider=effective.provider, model=effective.model, source=effective.source
    )

    uv_available = shutil.which("uv") is not None

    uv_cache_dir = get_uv_cache_dir()
    uv_cache_writable = _path_writable(uv_cache_dir)

    workspace_dir = get_workspace_dir()
    workspace_writable = _path_writable(workspace_dir)

    web_dist_present = (REPO_ROOT / "web" / "dist" / "index.html").exists()

    blockers: list[str] = []
    if effective.source == "bundle-default":
        blockers.append(
            "No model route configured: no agent-default-model in "
            "settings.yaml and no SWARM_MODEL set. Configure "
            "$DSH_HOME/settings.yaml's agent-default-model section, or "
            "set SWARM_MODEL."
        )
    if not uv_available:
        blockers.append("'uv' is not on PATH; the compile validation gate requires it.")
    if not workspace_writable:
        blockers.append(f"workspace directory is not writable: {workspace_dir}")
    if settings_error is not None:
        blockers.append(f"settings.yaml could not be read: {settings_error}")

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
    )
