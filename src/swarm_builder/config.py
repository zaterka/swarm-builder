"""Environment resolution with explicit defaults.

Every setting Swarm Builder reads from the environment is resolved here,
once, so the rest of the codebase never calls ``os.environ.get`` directly
for one of these names. Each default is chosen so the app runs with a
completely empty environment (see ``PLAN.md`` "Assumptions": the harness
is optional and a missing ``SWARM_MODEL`` is a normal, reportable state,
not a crash).

This module deliberately does nothing beyond env resolution: no I/O, no
imports of ``inherit/`` or ``compile/`` (those land in later groups), so
importing it has no side effects and requires no provider key.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Repository root -- the directory containing pyproject.toml. Used to
#: anchor the SWARM_WORKSPACE and UV_CACHE_DIR defaults so they resolve
#: consistently regardless of the process's current working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Where the harness's settings.yaml is looked for when ``DSH_HOME`` is
#: unset. Left unexpanded until read, so ``~`` resolves against the user
#: running the server rather than whoever wrote this file.
DEFAULT_DSH_HOME: Path = Path("~/.dsh")

#: Port the server binds when ``PORT`` is unset. High and uncommon, to
#: avoid colliding with the usual dev-server ports.
DEFAULT_PORT: int = 8420

#: Interface the server binds when ``SWARM_HOST`` is unset.
#:
#: Loopback is the deliberate default, not merely a convenience: PLAN.md
#: assumption 8 makes this an unauthenticated, trusted-input, single-user
#: tool, so binding anything wider would expose it to the local network.
#: A container is the one case that needs ``0.0.0.0`` internally -- a
#: process bound to loopback inside a container is unreachable through a
#: published port -- and the compose file keeps the outer restriction by
#: publishing to host loopback (``127.0.0.1:8420:8420``).
DEFAULT_HOST: str = "127.0.0.1"


def get_dsh_home() -> Path:
    """Directory to look for ``settings.yaml`` when inheriting model
    routes (facts 3-4, 21-23). Defaults to the user's real ``~/.dsh``,
    per PLAN.md Assumption 2. Absent entirely is a normal state."""
    raw = os.environ.get("DSH_HOME", str(DEFAULT_DSH_HOME))
    return Path(raw).expanduser()


def get_workspace_dir() -> Path:
    """Directory holding saved graphs and generated projects."""
    raw = os.environ.get("SWARM_WORKSPACE")
    if raw:
        return Path(raw).expanduser()
    return REPO_ROOT / "workspace"


def get_port() -> int:
    """Port the FastAPI server binds on. See :data:`DEFAULT_HOST` for the
    interface it binds."""
    raw = os.environ.get("PORT")
    if raw:
        return int(raw)
    return DEFAULT_PORT


def get_host() -> str:
    """Interface the FastAPI server binds on.

    Defaults to loopback; set ``SWARM_HOST=0.0.0.0`` when running in a
    container, where a loopback bind cannot be reached through a
    published port. See :data:`DEFAULT_HOST` for why loopback is the
    default rather than only the traditional one.
    """
    return os.environ.get("SWARM_HOST") or DEFAULT_HOST


def get_uv_cache_dir() -> Path:
    """Cache directory for every ``uv`` invocation this app makes,
    including the compile pipeline's validation gate (fact 10)."""
    raw = os.environ.get("UV_CACHE_DIR")
    if raw:
        return Path(raw).expanduser()
    return REPO_ROOT / ".uv-cache"


def get_swarm_model() -> str | None:
    """Fallback model when no harness route is inherited. ``None`` when
    unset -- callers decide how to report/handle the absence."""
    return os.environ.get("SWARM_MODEL") or None


def get_swarm_base_url() -> str | None:
    """Custom OpenAI-compatible endpoint base URL for a ``SWARM_MODEL``
    that is not a PydanticAI known model name."""
    return os.environ.get("SWARM_BASE_URL") or None


def get_swarm_api_key_env() -> str | None:
    """Name of the environment variable holding the API key for a
    ``SWARM_BASE_URL`` route (never the key value itself)."""
    return os.environ.get("SWARM_API_KEY_ENV") or None
