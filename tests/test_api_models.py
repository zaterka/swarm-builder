"""Tests for ``GET /api/models``."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_builder.main import create_app

_FULL_SETTINGS = """\
llm-pi-ai:
  providers:
    kornerstone:
      api: openai-completions
      baseURL: http://localhost:8000/v1
      apiKeyEnv: KORNERSTONE_API_KEY
      models:
        - id: qwen38-27b-fp8
          name: Qwen 3.8-27B
        - id: deepseek-v4-flash
          name: Deepseek-v4-Flash
    amazon-bedrock:
      awsProfile: factored-dev-profile
      awsRegion: us-east-1
      models:
        - id: us.anthropic.claude-opus-5
          name: Claude Opus 5 (US)
agent-default-model:
  provider: amazon-bedrock
  model: us.anthropic.claude-opus-5
"""

_ALT_SETTINGS = """\
llm-pi-ai:
  providers:
    anthropic:
      api: anthropic-messages
agent-default-model:
  provider: anthropic
  model: claude-3-opus
"""


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dsh_home: Path) -> TestClient:
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.delenv("SWARM_MODEL", raising=False)
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)
    monkeypatch.delenv("SWARM_API_KEY_ENV", raising=False)
    return TestClient(create_app())


def test_app_route_reports_the_in_app_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route a user configured in this application is reported separately
    from the inherited ones, and classified the same way."""
    from swarm_builder.appconfig import AppConfig, AppModelConfig, save_config

    dsh_home = tmp_path / "dsh_home"
    dsh_home.mkdir()
    client = _client(tmp_path, monkeypatch, dsh_home)
    save_config(
        AppConfig(
            model=AppModelConfig(provider="anthropic", model="claude-sonnet-4-6", api_key="sk-x")
        )
    )

    body = client.get("/api/models").json()

    assert body["appRoute"]["key"] == "anthropic"
    assert body["appRoute"]["emission"] == "known-name"
    assert body["appRoute"]["requiredExtra"] == "anthropic"
    assert body["resolvedDefault"]["source"] == "app-config"
    assert body["resolvedDefault"]["provider"] == "anthropic"
    # Inherited routes are unaffected.
    assert body["routes"] == []


def test_no_app_route_when_nothing_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsh_home = tmp_path / "dsh_home"
    dsh_home.mkdir()
    body = _client(tmp_path, monkeypatch, dsh_home).get("/api/models").json()
    assert body["appRoute"] is None


def test_routes_and_has_explicit_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dsh_home = tmp_path / "dsh_home"
    dsh_home.mkdir()
    (dsh_home / "settings.yaml").write_text(_FULL_SETTINGS, encoding="utf-8")
    client = _client(tmp_path, monkeypatch, dsh_home)

    body = client.get("/api/models").json()
    routes_by_key = {r["key"]: r for r in body["routes"]}

    assert routes_by_key["kornerstone"]["hasExplicitModels"] is True
    assert routes_by_key["amazon-bedrock"]["hasExplicitModels"] is True
    assert len(routes_by_key["kornerstone"]["models"]) == 2
    assert routes_by_key["kornerstone"]["models"][0]["id"] == "qwen38-27b-fp8"


def test_no_explicit_models_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dsh_home = tmp_path / "dsh_home"
    dsh_home.mkdir()
    (dsh_home / "settings.yaml").write_text(
        "llm-pi-ai:\n  providers:\n    plain-route:\n      baseURL: http://localhost:9000/v1\n",
        encoding="utf-8",
    )
    client = _client(tmp_path, monkeypatch, dsh_home)

    body = client.get("/api/models").json()
    route = next(r for r in body["routes"] if r["key"] == "plain-route")
    assert route["hasExplicitModels"] is False
    assert route["models"] == []


def test_emission_known_name_and_structural(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsh_home = tmp_path / "dsh_home"
    dsh_home.mkdir()
    (dsh_home / "settings.yaml").write_text(_FULL_SETTINGS, encoding="utf-8")
    client = _client(tmp_path, monkeypatch, dsh_home)

    body = client.get("/api/models").json()
    routes_by_key = {r["key"]: r for r in body["routes"]}

    bedrock = routes_by_key["amazon-bedrock"]
    assert bedrock["emission"] == "known-name"
    assert bedrock["requiredExtra"] == "bedrock"
    assert bedrock["unmappableReason"] is None

    kornerstone = routes_by_key["kornerstone"]
    assert kornerstone["emission"] == "structural"
    assert kornerstone["requiredExtra"] == "openai"
    assert kornerstone["unmappableReason"] is None


def test_no_caching_across_requests_after_rewriting_settings_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsh_home = tmp_path / "dsh_home"
    dsh_home.mkdir()
    settings_path = dsh_home / "settings.yaml"
    settings_path.write_text(_FULL_SETTINGS, encoding="utf-8")
    client = _client(tmp_path, monkeypatch, dsh_home)

    first = client.get("/api/models").json()
    assert first["resolvedDefault"]["provider"] == "amazon-bedrock"

    settings_path.write_text(_ALT_SETTINGS, encoding="utf-8")

    second = client.get("/api/models").json()
    assert second["resolvedDefault"]["provider"] == "anthropic"
    assert second != first


def test_no_caching_across_dsh_home_env_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    client = _client(tmp_path, monkeypatch, empty_home)

    first = client.get("/api/models").json()
    assert first["resolvedDefault"]["source"] == "bundle-default"

    other_home = tmp_path / "other_home"
    other_home.mkdir()
    (other_home / "settings.yaml").write_text(_ALT_SETTINGS, encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(other_home))

    second = client.get("/api/models").json()
    assert second["resolvedDefault"]["source"] == "settings-default"
    assert second["resolvedDefault"]["provider"] == "anthropic"
    assert second != first
