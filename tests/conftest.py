"""Shared pytest configuration.

The ``slow`` marker is registered in ``pyproject.toml`` under
``[tool.pytest.ini_options]``, which is where a reader expects to find
it. It lived here originally only because two subsystems were being
built concurrently and neither owned ``pyproject.toml``.
"""

from __future__ import annotations
