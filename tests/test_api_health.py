"""Tests for ``GET /api/health``.

Each test constructs a fresh app/client via ``create_app()`` after
setting ``DSH_HOME``/``SWARM_WORKSPACE`` via ``monkeypatch.setenv``, so
no test can ever read the real ``~/.dsh`` or the repo's own
``workspace/`` directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_builder.main import create_app


def _client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dsh_home: Path | None = None
) -> TestClient:
    default_home = tmp_path / "dsh_home"
    monkeypatch.setenv("DSH_HOME", str(dsh_home if dsh_home is not None else default_home))
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.delenv("SWARM_MODEL", raising=False)
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)
    monkeypatch.delenv("SWARM_API_KEY_ENV", raising=False)
    return TestClient(create_app())


def test_no_settings_file_reports_bundle_default_and_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)

    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()

    assert body["resolvedModel"]["source"] == "bundle-default"
    assert body["compileReady"] is False
    assert body["blockers"]
    joined = " ".join(body["blockers"])
    assert "agent-default-model" in joined
    assert "SWARM_MODEL" in joined


def test_valid_settings_file_resolves_settings_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsh_home = tmp_path / "dsh_home"
    dsh_home.mkdir(parents=True)
    (dsh_home / "settings.yaml").write_text(
        "llm-pi-ai:\n"
        "  providers:\n"
        "    amazon-bedrock:\n"
        "      awsProfile: some-profile\n"
        "      awsRegion: us-east-1\n"
        "agent-default-model:\n"
        "  provider: amazon-bedrock\n"
        "  model: us.anthropic.claude-opus-5\n",
        encoding="utf-8",
    )
    client = _client(tmp_path, monkeypatch, dsh_home=dsh_home)

    resp = client.get("/api/health")
    body = resp.json()

    assert body["resolvedModel"]["source"] == "settings-default"
    assert body["resolvedModel"]["provider"] == "amazon-bedrock"
    assert body["resolvedModel"]["model"] == "us.anthropic.claude-opus-5"

    # Robust regardless of whether `uv` happens to be on this machine's
    # PATH: only assert the bundle-default blocker is specifically gone.
    joined = " ".join(body["blockers"])
    assert "No model route configured" not in joined

    if shutil.which("uv") is not None:
        assert body["compileReady"] is True
        assert body["blockers"] == []


def test_malformed_settings_yaml_reports_settings_error_and_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsh_home = tmp_path / "dsh_home"
    dsh_home.mkdir(parents=True)
    (dsh_home / "settings.yaml").write_text(
        "llm-pi-ai:\n  providers:\n    kornerstone:\n      models: [\n        - id: x\n",
        encoding="utf-8",
    )
    client = _client(tmp_path, monkeypatch, dsh_home=dsh_home)

    resp = client.get("/api/health")
    body = resp.json()

    assert body["settingsError"] is not None
    assert body["compileReady"] is False
    joined = " ".join(body["blockers"])
    assert "settings.yaml could not be read" in joined


def test_uv_cache_dir_and_workspace_dir_reflect_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)

    resp = client.get("/api/health")
    body = resp.json()

    assert body["uvCacheDir"] == str(tmp_path / "uv-cache")
    assert body["workspaceDir"] == str(tmp_path / "workspace")


def test_no_caching_across_requests_on_same_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing regression guard: rewriting settings.yaml
    between two requests on the SAME running app/client must change
    the second response -- proving no app.state/module-level cache."""
    dsh_home = tmp_path / "dsh_home"
    dsh_home.mkdir(parents=True)
    client = _client(tmp_path, monkeypatch, dsh_home=dsh_home)

    first = client.get("/api/health").json()
    assert first["resolvedModel"]["source"] == "bundle-default"

    (dsh_home / "settings.yaml").write_text(
        "agent-default-model:\n  provider: amazon-bedrock\n  model: us.anthropic.claude-opus-5\n",
        encoding="utf-8",
    )

    second = client.get("/api/health").json()
    assert second["resolvedModel"]["source"] == "settings-default"
    assert second != first
