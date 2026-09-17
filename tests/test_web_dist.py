"""The committed frontend bundle, and the host the server binds.

``web/dist`` is shipped so a fresh clone can serve the canvas from
``uv run swarm-builder`` alone (see ``scripts/check_dist.py`` for why).
A stale or missing bundle produces a confusing "the UI is broken"
symptom, so its existence is asserted here while its freshness is only
warned about: a contributor editing Python must not see the suite go red
because someone else's frontend edit was not rebuilt.
"""

from __future__ import annotations

import importlib.util
import sys
import warnings
from pathlib import Path
from types import ModuleType

import pytest

from swarm_builder.config import DEFAULT_HOST, get_host

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_check_dist() -> ModuleType:
    """Import ``scripts/check_dist.py``, which is not an installed module."""
    script = REPO_ROOT / "scripts" / "check_dist.py"
    spec = importlib.util.spec_from_file_location("check_dist", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_dist"] = module
    spec.loader.exec_module(module)
    return module


def test_the_frontend_bundle_is_committed() -> None:
    """A clone that has never run pnpm must still serve the canvas.

    A hard requirement rather than a warning: without ``web/dist`` the
    server registers a fallback that answers ``/`` with "frontend has not
    been built yet", so the app looks broken to everyone but a maintainer
    who knows to run the frontend build.
    """
    dist = REPO_ROOT / "web" / "dist"

    assert (dist / "index.html").is_file(), (
        "web/dist/index.html is missing. Build it with `pnpm --dir web build` and "
        "commit web/dist; .gitignore carries a `!web/dist/**` negation that keeps it "
        "tracked despite the blanket `dist/` rule."
    )

    assets = list((dist / "assets").iterdir())
    assert any(path.suffix == ".js" for path in assets), "web/dist has no JS bundle"
    assert any(path.suffix == ".css" for path in assets), "web/dist has no CSS bundle"


def test_the_committed_bundle_is_not_stale() -> None:
    """Report a bundle that predates its sources, without failing."""
    is_stale, message = _load_check_dist().check()

    if is_stale:
        warnings.warn(
            f"web/dist is stale: {message}",
            UserWarning,
            stacklevel=2,
        )


def test_host_defaults_to_loopback() -> None:
    """The default bind must stay loopback.

    ``PLAN.md`` assumption 8 makes this an unauthenticated,
    trusted-input, single-user tool, so a wider default would silently
    expose it to the local network. Only the container image opts into
    ``0.0.0.0``, and compose re-establishes the restriction at the
    publish layer.
    """
    assert DEFAULT_HOST == "127.0.0.1"
    assert get_host() == "127.0.0.1"


def test_host_can_be_overridden_for_a_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SWARM_HOST=0.0.0.0`` is what makes a published port reachable."""
    monkeypatch.setenv("SWARM_HOST", "0.0.0.0")
    assert get_host() == "0.0.0.0"
