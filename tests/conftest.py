"""Shared pytest configuration.

**Why there is an autouse fixture here at all.** Two pieces of state this
suite exercises are process-global or filesystem-global and must never be the
developer's real one:

- ``SWARM_WORKSPACE`` decides where graphs *and* the application's own model
  settings live. A test that forgot to point it at ``tmp_path`` would read or
  overwrite the checkout's real ``workspace/`` directory.
- ``SWARM_CONFIG`` names the application's settings file. Pinning it under the
  same temporary root makes every test deterministic no matter what the
  developer has configured locally -- a saved provider in their real workspace
  must not be able to change what a test resolves.

**Why the environment is snapshotted and restored wholesale.** Publishing a
credential into ``os.environ`` is a documented, deliberate part of the design
(:func:`swarm_builder.runtime.publish_secrets`), and the compile pipeline's
keyless gate exists to prove a project imports and dry-runs with *no*
credential present. Without a restore, a settings test that published
``OPENAI_API_KEY`` would silently weaken every later ``slow`` test that
asserts keylessness. The fixture therefore snapshots the environment before
the test and puts it back exactly afterwards.

The ``slow`` marker is registered in ``pyproject.toml`` under
``[tool.pytest.ini_options]``, which is where a reader expects to find it.
It lived here originally only because two subsystems were being
built concurrently and neither owned ``pyproject.toml``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_swarm_state(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Point Swarm Builder's writable state at a temporary root, then restore.

    Applied before every test. A test that wants its own location still wins:
    its own ``monkeypatch.setenv`` runs after this fixture, and pytest applies
    monkeypatch changes in reverse order, so both are undone correctly.
    """
    root = tmp_path_factory.mktemp("swarm-state")
    monkeypatch.setenv("SWARM_WORKSPACE", str(root / "workspace"))
    monkeypatch.setenv("SWARM_CONFIG", str(root / "settings.json"))

    saved = dict(os.environ)
    try:
        yield
    finally:
        # Undo anything a test (or the code under test) set directly, so a
        # published API key can never leak into a later test.
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture
def settings_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A path for the application's settings file, wired up as ``SWARM_CONFIG``.

    The environment variable is re-pointed at ``tmp_path`` as well, so a test
    can write with an explicit path and read with :func:`config_path`'s
    default and have both mean the same file -- which is exactly the
    agreement the application relies on.
    """
    target = tmp_path / "settings.json"
    monkeypatch.setenv("SWARM_CONFIG", str(target))
    return target
