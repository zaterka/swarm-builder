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
    assert body["modelConfigured"] is False
    assert body["dryRun"] is False

    # The blocker must be actionable *inside this application*: a brand-new
    # user has no settings.yaml, no DSH_HOME and no reason to know either
    # name. It may not name a component outside Swarm Builder.
    joined = " ".join(body["blockers"])
    assert "Model settings" in joined
    assert "Dry run" in joined
    for foreign in ("settings.yaml", "DSH_HOME", "agent-default-model", "SWARM_MODEL", "harness"):
        assert foreign not in joined, foreign


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
    # A user who *has* an inherited file is told about it in plain words --
    # it is a file they own -- but it is framed as the advanced path, and the
    # first-time-user blocker above is what a fresh install sees.
    assert "inherited model settings file could not be read" in joined


def test_app_config_model_makes_a_fresh_install_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saving a provider and key in the app's own settings is enough: no
    settings.yaml, no DSH_HOME, no environment variable."""
    from swarm_builder.appconfig import AppConfig, AppModelConfig, save_config

    client = _client(tmp_path, monkeypatch)
    save_config(
        AppConfig(
            model=AppModelConfig(provider="openai", model="gpt-5.4-mini", api_key="sk-x")
        )
    )

    body = client.get("/api/health").json()

    assert body["resolvedModel"]["source"] == "app-config"
    assert body["resolvedModel"]["provider"] == "openai"
    assert body["modelConfigured"] is True
    assert body["compileReady"] is True
    assert body["blockers"] == []


def test_dry_run_makes_a_fresh_install_ready_with_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swarm_builder.appconfig import AppConfig, save_config

    client = _client(tmp_path, monkeypatch)
    save_config(AppConfig(dry_run=True))

    body = client.get("/api/health").json()

    assert body["dryRun"] is True
    assert body["dryRunForcedByEnv"] is False
    assert body["modelConfigured"] is False
    # Nothing to configure and nothing to authenticate: both gates are open.
    assert body["compileReady"] is True
    assert body["runReady"] is True
    assert body["blockers"] == []
    assert body["runBlockers"] == []


def test_an_exported_fake_fill_variable_reports_as_forced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-existing offline switch keeps working, and the UI is told the
    environment is what turned it on (so it can lock its own switch)."""
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")

    body = client.get("/api/health").json()

    assert body["dryRun"] is True
    assert body["dryRunForcedByEnv"] is True
    assert body["compileReady"] is True
    assert body["runReady"] is True


def test_a_configured_model_with_no_credential_blocks_compile_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 3's fill spends the model, so enabling Compile and then failing on
    an authentication error would be a worse experience than being told up
    front. The blocker is shared with Run."""
    from swarm_builder.appconfig import AppConfig, AppModelConfig, save_config

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client(tmp_path, monkeypatch)
    save_config(AppConfig(model=AppModelConfig(provider="openai", model="gpt-5.4-mini")))

    body = client.get("/api/health").json()

    assert body["modelConfigured"] is True
    assert body["compileReady"] is False
    assert body["runReady"] is False
    joined = " ".join(body["blockers"])
    assert "Model settings" in joined
    assert "Dry run mode" in joined


def test_a_broken_in_app_settings_file_does_not_block_a_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With dry run forced on by the environment, neither settings file changes
    what the pipeline does -- so blocking on them would disable work that would
    succeed. Both remain visible in their own fields."""
    from swarm_builder.config import get_app_config_path

    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")
    path = get_app_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")

    body = client.get("/api/health").json()

    assert body["dryRun"] is True
    assert body["appConfigError"] is not None
    assert body["compileReady"] is True
    assert body["blockers"] == []


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
