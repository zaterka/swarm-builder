"""Tests for ``GET /api/graphs/:id/export``."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_builder.main import create_app
from swarm_builder.store.projects import ensure_project_dir


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    return TestClient(create_app())


def test_export_without_project_is_404_naming_expected_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)

    resp = client.get("/api/graphs/never-compiled/export")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "never-compiled" in detail
    assert str(tmp_path / "workspace" / "projects" / "never-compiled") in detail


def test_export_with_project_returns_path_and_run_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    workspace_dir = tmp_path / "workspace"
    project_path = ensure_project_dir(workspace_dir, "g1")

    resp = client.get("/api/graphs/g1/export")
    assert resp.status_code == 200
    body = resp.json()

    assert body["projectPath"] == str(project_path)
    assert "UV_CACHE_DIR=" in body["runCommand"]
    assert "uv sync" in body["runCommand"]
    assert "uv run" in body["runCommand"]
    assert str(project_path) in body["runCommand"]
