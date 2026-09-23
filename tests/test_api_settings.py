"""Tests for ``/api/settings`` -- the in-app model configuration.

These are the tests that hold the three promises the settings screen makes to
a brand-new user:

- **the key is never handed back** -- ``GET`` reports only that one is stored
  and its last four characters, and no endpoint's body contains the value;
- **a save takes effect immediately** -- ``GET /api/health`` flips to
  ``modelConfigured`` with no restart, because every read is fresh by design;
- **dry run is honest** -- switching it on makes a fresh install compilable
  and runnable with no credential at all, and an environment variable forces
  it on in a way the UI cannot undo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_builder.main import create_app
from swarm_builder.runtime import DRY_RUN_ENV_VARS

_SECRET = "sk-unit-test-secret-value-4321"


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client whose app config and workspace both live under ``tmp_path``."""
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("SWARM_CONFIG", str(tmp_path / "workspace" / "settings.json"))
    for name in ("SWARM_MODEL", "SWARM_BASE_URL", "SWARM_API_KEY_ENV", *DRY_RUN_ENV_VARS):
        monkeypatch.delenv(name, raising=False)
    return TestClient(create_app())


def _config_file(tmp_path: Path) -> Path:
    return tmp_path / "workspace" / "settings.json"


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


def test_get_settings_on_a_fresh_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)

    body = client.get("/api/settings").json()

    assert body["model"] is None
    assert body["configError"] is None
    assert body["dryRun"] is False
    assert body["dryRunForcedByEnv"] is False
    assert body["dryRunEnvVars"] == []
    assert body["inheritedRoutes"] == 0
    # The catalog is what the screen renders the provider picker from.
    keys = [p["key"] for p in body["providers"]]
    assert keys[:3] == ["openai", "anthropic", "deepseek"]
    assert "custom" in keys
    assert body["resolvedDefault"]["source"] == "bundle-default"


def test_provider_catalog_flags_match_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _client(tmp_path, monkeypatch).get("/api/settings").json()
    by_key = {p["key"]: p for p in body["providers"]}

    assert by_key["openai"]["requiresApiKey"] is True
    assert by_key["openai"]["apiKeyEnv"] == "OPENAI_API_KEY"
    assert by_key["openai"]["defaultModel"] in by_key["openai"]["models"]
    assert by_key["custom"]["requiresBaseUrl"] is True
    assert by_key["custom"]["models"] == []
    # A custom endpoint is unusable until it has a base URL, and the screen
    # says why rather than letting a compile fail later.
    assert by_key["custom"]["usable"] is False
    assert "base URL" in by_key["custom"]["unusableReason"]
    assert by_key["bedrock"]["requiresApiKey"] is False
    assert by_key["bedrock"]["note"]


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------


def test_put_saves_a_model_and_never_returns_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.put(
        "/api/settings",
        json={"model": {"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model"]["provider"] == "openai"
    assert body["model"]["hasApiKey"] is True
    assert body["model"]["apiKeyHint"] == f"…{_SECRET[-4:]}"
    assert _SECRET not in response.text

    # Stored on disk, owner-only, inside the (git-ignored) workspace.
    stored = json.loads(_config_file(tmp_path).read_text(encoding="utf-8"))
    assert stored["model"]["apiKey"] == _SECRET
    assert (stored["version"], stored["dryRun"]) == (1, False)
    assert (_config_file(tmp_path).stat().st_mode & 0o777) == 0o600

    # No read path in the API echoes the value back.
    for path in ("/api/settings", "/api/health", "/api/models"):
        assert _SECRET not in client.get(path).text, path


def test_no_endpoint_returns_the_stored_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep, not a spot check: the key travels through compile and run
    paths, so every JSON endpoint is asked and its whole body searched.

    A regression here would be a credential disclosure, which is exactly the
    kind of thing a single-endpoint assertion misses.
    """
    client = _client(tmp_path, monkeypatch)
    client.put(
        "/api/settings",
        json={"model": {"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET}},
    )

    reads = [
        ("GET", "/api/health"),
        ("GET", "/api/models"),
        ("GET", "/api/settings"),
        ("GET", "/api/graphs"),
        ("GET", "/api/templates"),
    ]
    bodies = []
    for method, path in reads:
        response = client.request(method, path)
        bodies.append((path, response.text))

    # Endpoints that answer with an error still must not quote the key back.
    bodies.append(("/api/graphs/nope/export", client.get("/api/graphs/nope/export").text))
    bodies.append(
        (
            "/api/settings/test (dry run)",
            client.post("/api/settings/test", json={"dryRun": True}).text,
        )
    )

    for path, text in bodies:
        assert _SECRET not in text, f"{path} returned the stored key"


def test_saved_model_takes_effect_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/health").json()["compileReady"] is False

    client.put(
        "/api/settings",
        json={"model": {"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET}},
    )

    health = client.get("/api/health").json()
    assert health["modelConfigured"] is True
    assert health["compileReady"] is True
    assert health["resolvedModel"]["source"] == "app-config"
    assert health["resolvedModel"]["provider"] == "openai"
    # The credential is live in this process, so the run path can use it.
    import os

    assert os.environ["OPENAI_API_KEY"] == _SECRET


def test_put_keeps_the_stored_key_when_only_the_model_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.put(
        "/api/settings",
        json={"model": {"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET}},
    )

    body = client.put(
        "/api/settings", json={"model": {"provider": "openai", "model": "gpt-5.5"}}
    ).json()

    assert body["model"]["model"] == "gpt-5.5"
    assert body["model"]["hasApiKey"] is True


def test_a_short_key_is_never_partially_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`apiKeyHint` exists so a user can tell two long keys apart. For a short
    token (a local gateway, a test fixture) four characters are a meaningful
    part of the secret, so no hint is shown -- `hasApiKey` is enough."""
    client = _client(tmp_path, monkeypatch)
    short_key = "shrtok3n"
    body = client.put(
        "/api/settings",
        json={"model": {"provider": "custom", "model": "qwen3",
                        "baseUrl": "http://127.0.0.1:8000/v1", "apiKey": short_key}},
    ).json()

    assert body["model"]["hasApiKey"] is True
    assert body["model"]["apiKeyHint"] is None
    assert short_key not in json.dumps(body)


def test_put_rejects_a_model_id_carrying_a_line_break(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The id is spliced into a generated project's .env.example as a whole
    line, so a newline would forge a credential line in an artifact."""
    client = _client(tmp_path, monkeypatch)
    response = client.put(
        "/api/settings",
        json={
            "model": {
                "provider": "openai",
                "model": "gpt-4o\nANTHROPIC_API_KEY=sk-attacker",
                "apiKey": "sk-x",
            }
        },
    )
    assert response.status_code == 422
    assert any("control characters" in problem for problem in response.json()["detail"])
    assert not _config_file(tmp_path).exists()


def test_clearing_the_key_retracts_it_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The credential is published into this process's environment, which every
    run subprocess inherits. Removing it in the UI must remove it there too,
    or the app keeps authenticating with a key the user revoked."""
    import os

    client = _client(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client.put(
        "/api/settings",
        json={"model": {"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET}},
    )
    assert os.environ["OPENAI_API_KEY"] == _SECRET

    client.put("/api/settings", json={"clearModel": True})

    assert "OPENAI_API_KEY" not in os.environ


def test_the_test_button_also_works_for_an_inherited_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing saved here, but a settings.yaml default exists: the button must
    test that route rather than claiming nothing is configured."""
    dsh_home = tmp_path / "dsh_home"
    dsh_home.mkdir(parents=True)
    (dsh_home / "settings.yaml").write_text(
        "llm-pi-ai:\n  providers:\n    openai:\n      api: openai-completions\n"
        "agent-default-model:\n  provider: openai\n  model: gpt-5.4-mini\n",
        encoding="utf-8",
    )
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-environment")

    called: dict[str, object] = {}

    async def _ping(model: object) -> None:
        called["model"] = model

    monkeypatch.setattr("swarm_builder.routes.settings._ping", _ping)

    body = client.post("/api/settings/test", json={}).json()

    assert body["ok"] is True
    assert body["model"] == "openai/gpt-5.4-mini"
    assert called, "the inherited route should have been exercised"


def test_put_clears_the_key_on_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Removing a stored key must be possible without also discarding the
    provider: the key may be supplied by the environment instead."""
    client = _client(tmp_path, monkeypatch)
    client.put(
        "/api/settings",
        json={"model": {"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET}},
    )

    body = client.put(
        "/api/settings",
        json={"model": {"provider": "openai", "model": "gpt-5.4-mini", "clearApiKey": True}},
    ).json()

    assert body["model"]["hasApiKey"] is False
    assert body["model"]["apiKeyHint"] is None


def test_put_never_carries_a_key_across_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DeepSeek key must not be published as ``OPENAI_API_KEY``.

    The switch is allowed -- the new provider's key may come from the
    environment -- but the stored key must not travel with it.
    """
    client = _client(tmp_path, monkeypatch)
    client.put(
        "/api/settings",
        json={"model": {"provider": "deepseek", "model": "deepseek-v4-flash", "apiKey": _SECRET}},
    )

    body = client.put(
        "/api/settings", json={"model": {"provider": "openai", "model": "gpt-5.4-mini"}}
    ).json()

    assert body["model"]["provider"] == "openai"
    assert body["model"]["hasApiKey"] is False
    assert _SECRET not in json.dumps(body)


def test_put_reports_every_problem_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.put(
        "/api/settings",
        json={"model": {"provider": "custom", "model": "", "apiKey": ""}},
    )

    assert response.status_code == 422
    problems = response.json()["detail"]
    assert any("model id" in problem for problem in problems)
    assert any("base URL is required" in problem for problem in problems)
    # Nothing was written by a rejected submission.
    assert not _config_file(tmp_path).exists()


def test_put_rejects_a_base_url_for_a_curated_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    response = client.put(
        "/api/settings",
        json={
            "model": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "baseUrl": "https://proxy.internal/v1",
                "apiKey": _SECRET,
            }
        },
    )
    assert response.status_code == 422
    assert "only supported for the" in " ".join(response.json()["detail"])


def test_put_rejects_a_base_url_carrying_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The URL is rendered verbatim into a generated project, so a secret
    inside it would end up in an artifact."""
    client = _client(tmp_path, monkeypatch)
    response = client.put(
        "/api/settings",
        json={
            "model": {
                "provider": "custom",
                "model": "qwen3",
                "baseUrl": f"https://user:{_SECRET}@gw.internal/v1",
                "apiKey": "sk-other",
            }
        },
    )
    assert response.status_code == 422
    assert "must not contain credentials" in " ".join(response.json()["detail"])
    assert _SECRET not in response.text


def test_put_with_an_unknown_provider_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    response = client.put("/api/settings", json={"model": {"provider": "gemini", "model": "x"}})
    assert response.status_code == 422
    assert "unknown provider" in " ".join(response.json()["detail"])


def test_put_can_clear_the_model_and_leave_only_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.put(
        "/api/settings",
        json={"model": {"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET}},
    )

    body = client.put("/api/settings", json={"clearModel": True, "dryRun": True}).json()

    assert body["model"] is None
    assert body["dryRun"] is True
    assert body["resolvedDefault"]["source"] == "bundle-default"


def test_put_omitting_a_section_leaves_it_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.put("/api/settings", json={"dryRun": True})

    body = client.put(
        "/api/settings",
        json={"model": {"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET}},
    ).json()

    assert body["dryRun"] is True
    assert body["model"]["provider"] == "openai"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_switch_makes_a_fresh_install_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)

    body = client.put("/api/settings", json={"dryRun": True}).json()
    assert body["dryRun"] is True
    assert body["dryRunForcedByEnv"] is False

    health = client.get("/api/health").json()
    assert health["dryRun"] is True
    assert health["modelConfigured"] is False
    assert health["compileReady"] is True
    assert health["runReady"] is True
    assert health["blockers"] == []


def test_environment_forces_dry_run_and_locks_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("SWARM_FAKE_FILL", "1")

    body = client.get("/api/settings").json()
    assert body["dryRun"] is True
    assert body["dryRunForcedByEnv"] is True
    assert body["dryRunEnvVars"] == ["SWARM_FAKE_FILL"]

    # Turning the switch off in the UI cannot defeat an exported variable.
    after = client.put("/api/settings", json={"dryRun": False}).json()
    assert after["dryRun"] is True
    assert after["dryRunForcedByEnv"] is True


# ---------------------------------------------------------------------------
# Broken configuration file
# ---------------------------------------------------------------------------


def test_unparsable_config_is_reported_without_breaking_the_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    path = _config_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    body = client.get("/api/settings").json()
    assert body["configError"] is not None
    assert body["model"] is None

    health = client.get("/api/health").json()
    assert health["appConfigError"] is not None
    joined = " ".join(health["blockers"])
    assert "model settings could not be read" in joined


def test_an_unusable_entry_is_reported_and_does_not_shadow_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry that cannot build a model (an unknown provider, a malformed
    base URL) is ignored by resolution in favour of the next source -- so the
    screen and the health check have to report it, or the fallthrough is
    silent."""
    path = _config_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"model": {"provider": "gemini", "model": "gemini-2.5-flash"}}),
        encoding="utf-8",
    )
    client = _client(tmp_path, monkeypatch)

    settings_body = client.get("/api/settings").json()
    assert settings_body["configError"] is not None
    assert "unknown provider" in settings_body["configError"]

    health = client.get("/api/health").json()
    assert health["modelConfigured"] is False
    joined = " ".join(health["blockers"])
    assert "not usable" in joined
    assert "Model settings" in joined


def test_saving_over_a_broken_file_repairs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The screen offers to fix a broken file; saving must actually work."""
    client = _client(tmp_path, monkeypatch)
    path = _config_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    body = client.put(
        "/api/settings",
        json={"model": {"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET}},
    ).json()

    assert body["configError"] is None
    assert body["model"]["provider"] == "openai"


# ---------------------------------------------------------------------------
# POST /api/settings/test
# ---------------------------------------------------------------------------


def test_test_connection_reports_a_missing_key_without_calling_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def _must_not_run(model: object) -> None:  # pragma: no cover - asserted
        raise AssertionError("no network call without a credential")

    monkeypatch.setattr("swarm_builder.routes.settings._ping", _must_not_run)

    body = client.post(
        "/api/settings/test", json={"provider": "openai", "model": "gpt-5.4-mini"}
    ).json()

    assert body["ok"] is False
    assert "No API key for 'OpenAI' yet" in body["detail"]


def test_test_connection_reports_no_model_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    body = client.post("/api/settings/test", json={}).json()
    assert body["ok"] is False
    assert "No model is configured yet" in body["detail"]


def test_test_connection_makes_no_network_call_in_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.put(
        "/api/settings",
        json={
            "model": {"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET},
            "dryRun": True,
        },
    )
    body = client.post("/api/settings/test", json={}).json()
    assert body["ok"] is False
    assert "Dry run mode is on" in body["detail"]


def test_test_connection_reports_a_refused_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)

    async def _refuse(model: object) -> None:
        raise RuntimeError(f"401 Unauthorized for key {_SECRET}")

    monkeypatch.setattr("swarm_builder.routes.settings._ping", _refuse)

    body = client.post(
        "/api/settings/test",
        json={"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET},
    ).json()

    assert body["ok"] is False
    assert "401 Unauthorized" in body["detail"]
    # The key never appears in the response, not even inside a provider echo.
    assert _SECRET not in json.dumps(body)
    assert body["model"] == "openai/gpt-5.4-mini"


def test_test_connection_reports_success_and_latency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)

    async def _ok(model: object) -> None:
        return None

    monkeypatch.setattr("swarm_builder.routes.settings._ping", _ok)

    body = client.post(
        "/api/settings/test",
        json={"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET},
    ).json()

    assert body["ok"] is True
    assert body["latencyMs"] is not None
    assert _SECRET not in json.dumps(body)
    # A test does not persist anything.
    assert not _config_file(tmp_path).exists()


def test_test_connection_reports_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)

    async def _hang(model: object) -> None:
        raise TimeoutError

    monkeypatch.setattr("swarm_builder.routes.settings._ping", _hang)

    body = client.post(
        "/api/settings/test",
        json={"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET},
    ).json()

    assert body["ok"] is False
    assert "did not answer within" in body["detail"]


def test_test_connection_reports_an_unmappable_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider with no PydanticAI mapping is a *result*, not a 500."""
    client = _client(tmp_path, monkeypatch)

    body = client.post(
        "/api/settings/test", json={"provider": "custom", "model": "qwen3", "apiKey": "sk-x"}
    ).json()

    assert body["ok"] is False
    assert "base URL" in body["detail"]


def test_test_connection_does_not_publish_an_unsaved_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Testing a form must not leave the candidate key live in the process."""
    import os

    client = _client(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def _refuse(model: object) -> None:
        raise RuntimeError("nope")

    monkeypatch.setattr("swarm_builder.routes.settings._ping", _refuse)

    client.post(
        "/api/settings/test",
        json={"provider": "openai", "model": "gpt-5.4-mini", "apiKey": _SECRET},
    )

    assert os.environ.get("OPENAI_API_KEY") is None
