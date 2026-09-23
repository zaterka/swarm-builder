"""Unit tests for :mod:`swarm_builder.inherit.settings` (PLAN.md facts
3-4, 21; the "Model inheritance" section; "No agent-default-model
section at all" and "A harness route settings cannot enumerate" edge
cases).

Every test uses ``tmp_path`` for its ``dsh_home`` -- the real
``~/.dsh`` is never read or written here. Env vars are managed through
``monkeypatch.setenv``/``monkeypatch.delenv`` exclusively, never through
direct ``os.environ`` mutation, so nothing leaks across tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_builder.appconfig import AppConfig, AppModelConfig
from swarm_builder.inherit.settings import (
    AgentDefaultModel,
    EffectiveModel,
    ModelInfo,
    RouteConfig,
    Settings,
    read_settings,
    resolve_effective_model,
    route_for_app_model,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "settings"


def _write_settings(dsh_home: Path, fixture_name: str) -> None:
    """Copy one fixture's text into ``<dsh_home>/settings.yaml``."""
    dsh_home.mkdir(parents=True, exist_ok=True)
    text = (FIXTURES_DIR / fixture_name).read_text(encoding="utf-8")
    (dsh_home / "settings.yaml").write_text(text, encoding="utf-8")


def _clear_swarm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every env var this module's fallback path reads, so a
    test that wants "no env configuration at all" gets exactly that
    regardless of what the outer environment happens to have set."""
    for name in ("SWARM_MODEL", "SWARM_BASE_URL", "SWARM_API_KEY_ENV"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# 1. Explicit `models:` list -> RouteConfig.models populated correctly.
# ---------------------------------------------------------------------------


def test_route_with_explicit_models_list(tmp_path: Path) -> None:
    _write_settings(tmp_path, "full.yaml")
    settings = read_settings(tmp_path)
    assert settings is not None
    assert settings.error is None

    kornerstone = next(r for r in settings.routes if r.key == "kornerstone")
    assert kornerstone.models == (
        ModelInfo(id="qwen38-27b-fp8", name="Qwen 3.8-27B"),
        ModelInfo(id="deepseek-v4-flash", name="Deepseek-v4-Flash"),
    )
    assert kornerstone.api == "openai-completions"
    assert kornerstone.base_url == "http://localhost:8000/v1"
    assert kornerstone.api_key_env == "KORNERSTONE_API_KEY"


# ---------------------------------------------------------------------------
# 2. No `models:` key at all -> RouteConfig.models == () (fact 21).
# ---------------------------------------------------------------------------


def test_route_without_models_key_is_empty_tuple(tmp_path: Path) -> None:
    _write_settings(tmp_path, "no_explicit_models.yaml")
    settings = read_settings(tmp_path)
    assert settings is not None
    assert settings.error is None

    kornerstone = next(r for r in settings.routes if r.key == "kornerstone")
    assert kornerstone.models == ()


# ---------------------------------------------------------------------------
# 3. awsProfile/awsRegion present, no explicit `api:` -> inferred bedrock.
# ---------------------------------------------------------------------------


def test_route_infers_bedrock_api_from_aws_fields(tmp_path: Path) -> None:
    _write_settings(tmp_path, "full.yaml")
    settings = read_settings(tmp_path)
    assert settings is not None

    bedrock = next(r for r in settings.routes if r.key == "amazon-bedrock")
    assert bedrock.api == "bedrock-converse-stream"
    assert bedrock.aws_profile == "factored-dev-profile"
    assert bedrock.aws_region == "us-east-1"
    # Tolerates unknown per-model keys (contextWindow/maxTokens) without
    # raising, and without leaking them into ModelInfo.
    assert bedrock.models == (
        ModelInfo(id="us.anthropic.claude-opus-5", name="Claude Opus 5 (US)"),
        ModelInfo(id="us.anthropic.claude-sonnet-5", name="Claude Sonnet 5 (US)"),
    )


# ---------------------------------------------------------------------------
# 4. Neither `api` nor AWS fields present -> RouteConfig.api is None.
# ---------------------------------------------------------------------------


def test_route_with_no_api_and_no_aws_fields_is_none(tmp_path: Path) -> None:
    _write_settings(tmp_path, "no_explicit_models.yaml")
    settings = read_settings(tmp_path)
    assert settings is not None

    plain = next(r for r in settings.routes if r.key == "plain-route")
    assert plain.api is None
    assert plain.aws_profile is None
    assert plain.aws_region is None
    assert plain.base_url == "http://localhost:9000/v1"


# ---------------------------------------------------------------------------
# 5. Full precedence chain.
# ---------------------------------------------------------------------------


def test_app_config_outranks_settings_file_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route a user chose in this application's own model settings wins.

    Precedence is source-wise: the *whole* selection (provider, model, key
    variable, key) comes from the winning source, so a pair no single source
    declares can never be resolved.
    """
    _write_settings(tmp_path, "full.yaml")
    monkeypatch.setenv("SWARM_MODEL", "bedrock:should-not-be-used")

    app_config = AppConfig(
        model=AppModelConfig(provider="openai", model="gpt-5.4-mini", api_key="sk-app")
    )
    result = resolve_effective_model(tmp_path, app_config=app_config)

    assert result.source == "app-config"
    assert result.provider == "openai"
    assert result.model == "gpt-5.4-mini"
    assert result.api_key_env == "OPENAI_API_KEY"
    assert result.api_key == "sk-app"
    assert result.route is not None
    assert result.route.key == "openai"
    assert result.route.base_url is None


def test_app_config_route_carries_the_protocol_the_registry_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A curated provider's synthesized route declares its protocol, so
    classification and the Phase-5 extras assertion see what the picker
    promised."""
    _clear_swarm_env(monkeypatch)
    app_config = AppConfig(
        model=AppModelConfig(provider="anthropic", model="claude-sonnet-4-6", api_key="sk-app")
    )
    result = resolve_effective_model(tmp_path, app_config=app_config)
    assert result.route is not None
    assert result.route.api == "anthropic-messages"
    assert result.route.api_key_env == "ANTHROPIC_API_KEY"


def test_app_config_base_url_is_kept_only_for_the_custom_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_swarm_env(monkeypatch)
    app_config = AppConfig(
        model=AppModelConfig(
            provider="custom",
            model="qwen3",
            base_url="http://127.0.0.1:8000/v1",
            api_key="sk-app",
        )
    )
    result = resolve_effective_model(tmp_path, app_config=app_config)
    assert result.source == "app-config"
    assert result.base_url == "http://127.0.0.1:8000/v1"
    assert result.api_key_env == "SWARM_API_KEY"
    assert result.route is not None and result.route.api is None


def test_an_invalid_app_model_never_shadows_a_working_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A base URL carrying credentials is rejected on save, so one on disk was
    hand-edited. It must not win the precedence chain: an entry that cannot
    build a model would fail every compile with an unmappable-route error while
    ignoring a perfectly good inherited/environment configuration.

    The screen reports the problem in its own right, so the user still learns
    what is wrong.
    """
    monkeypatch.setenv("SWARM_MODEL", "bedrock:us.anthropic.claude-sonnet-5")
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)
    app_config = AppConfig(
        model=AppModelConfig(
            provider="custom",
            model="qwen3",
            base_url="https://user:sk-leak@host/v1",
            api_key="sk-app",
        )
    )

    result = resolve_effective_model(tmp_path, app_config=app_config)

    assert result.source == "env-fallback"
    assert "sk-leak" not in repr(result)


def test_route_for_app_model_strips_embedded_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth for the renderers: even if such a value reached disk,
    the synthesized route (which `to_resolved_model` splices into a generated
    project's deps.py and .env.example) never carries it."""
    _clear_swarm_env(monkeypatch)
    route = route_for_app_model(
        AppModelConfig(
            provider="custom",
            model="qwen3",
            base_url="https://user:sk-leak@host/v1",
            api_key="sk-app",
        )
    )
    assert route.base_url == "https://host/v1"


def test_an_unknown_app_provider_never_shadows_a_working_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWARM_MODEL", "bedrock:us.anthropic.claude-sonnet-5")
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)
    app_config = AppConfig(model=AppModelConfig(provider="OpenAI ", model="gpt-5"))

    result = resolve_effective_model(tmp_path, app_config=app_config)

    assert result.source == "env-fallback"


def test_a_graph_override_to_the_in_app_provider_finds_its_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override wins the provider/model, but the app config still supplies
    the endpoint and credential for a provider the inherited file does not
    describe -- otherwise a graph override to a configured custom endpoint
    would resolve with no base URL and be refused as unmappable."""
    _clear_swarm_env(monkeypatch)
    app_config = AppConfig(
        model=AppModelConfig(
            provider="custom",
            model="qwen3",
            base_url="http://127.0.0.1:8000/v1",
            api_key="sk-app",
        )
    )

    result = resolve_effective_model(
        tmp_path, graph_override=("custom", "other-model", None), app_config=app_config
    )

    assert result.source == "graph-override"
    assert result.model == "other-model"
    assert result.base_url == "http://127.0.0.1:8000/v1"
    assert result.api_key == "sk-app"


def test_broken_app_config_falls_through_to_the_next_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mangled settings file must never crash resolution; the health check
    is what reports it."""
    monkeypatch.setenv("SWARM_MODEL", "bedrock:us.anthropic.claude-sonnet-5")
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)

    result = resolve_effective_model(
        tmp_path, app_config=AppConfig(error="failed to parse /x/settings.json")
    )
    assert result.source == "env-fallback"


def test_empty_app_config_means_no_in_app_route(tmp_path: Path) -> None:
    result = resolve_effective_model(tmp_path, app_config=AppConfig())
    assert result.source == "bundle-default"


def test_effective_model_repr_never_shows_the_key(tmp_path: Path) -> None:
    """``api_key`` is a secret that travels through compile and run paths, so
    it must be invisible to every incidental ``repr`` (a log line, a
    traceback, a failure report)."""
    app_config = AppConfig(
        model=AppModelConfig(provider="openai", model="gpt-5", api_key="sk-do-not-print")
    )
    result = resolve_effective_model(tmp_path, app_config=app_config)
    assert "sk-do-not-print" not in repr(result)
    assert "sk-do-not-print" not in str(result)


def test_graph_override_wins_over_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_settings(tmp_path, "full.yaml")
    monkeypatch.setenv("SWARM_MODEL", "bedrock:should-not-be-used")

    result = resolve_effective_model(
        tmp_path,
        graph_override=("kornerstone", "qwen38-27b-fp8", "high"),
    )

    assert result.source == "graph-override"
    assert result.provider == "kornerstone"
    assert result.model == "qwen38-27b-fp8"
    assert result.reasoning_effort == "high"
    assert result.route is not None
    assert result.route.key == "kornerstone"
    assert result.base_url == "http://localhost:8000/v1"
    assert result.api_key_env == "KORNERSTONE_API_KEY"


def test_graph_override_also_beats_the_app_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_swarm_env(monkeypatch)
    app_config = AppConfig(
        model=AppModelConfig(provider="openai", model="gpt-5", api_key="sk-app")
    )
    result = resolve_effective_model(
        tmp_path, graph_override=("bedrock", "us.anthropic.claude-sonnet-5", None),
        app_config=app_config,
    )
    assert result.source == "graph-override"
    assert result.provider == "bedrock"
    assert result.api_key is None


def test_settings_default_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_settings(tmp_path, "full.yaml")
    monkeypatch.setenv("SWARM_MODEL", "bedrock:should-not-be-used")

    result = resolve_effective_model(tmp_path)

    assert result.source == "settings-default"
    assert result.provider == "amazon-bedrock"
    assert result.model == "us.anthropic.claude-opus-5"
    assert result.route is not None
    assert result.route.key == "amazon-bedrock"


def test_env_fallback_used_when_no_settings_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tmp_path is an empty directory: no settings.yaml written into it.
    monkeypatch.setenv("SWARM_MODEL", "bedrock:us.anthropic.claude-sonnet-5")
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)

    result = resolve_effective_model(tmp_path)

    assert result.source == "env-fallback"
    assert result.provider == "bedrock"
    assert result.model == "us.anthropic.claude-sonnet-5"
    assert result.route is None
    assert result.base_url is None


def test_bundle_default_used_when_nothing_else_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_swarm_env(monkeypatch)

    result = resolve_effective_model(tmp_path)

    assert result.source == "bundle-default"
    assert result.provider == "deepseek-official"
    assert result.model == "deepseek-v4-flash"
    assert result.reasoning_effort is None
    assert result.route is None
    assert result.base_url is None
    assert result.api_key_env is None


# ---------------------------------------------------------------------------
# 6. SWARM_MODEL + SWARM_BASE_URL both set -> "custom" provider, unsplit.
# ---------------------------------------------------------------------------


def test_env_fallback_with_base_url_reports_custom_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWARM_MODEL", "bedrock:us.anthropic.claude-sonnet-5")
    monkeypatch.setenv("SWARM_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setenv("SWARM_API_KEY_ENV", "MY_API_KEY")

    result = resolve_effective_model(tmp_path)

    assert result.source == "env-fallback"
    assert result.provider == "custom"
    # The raw SWARM_MODEL value, unsplit -- not treated as provider:model.
    assert result.model == "bedrock:us.anthropic.claude-sonnet-5"
    assert result.base_url == "http://localhost:9999/v1"
    assert result.api_key_env == "MY_API_KEY"
    assert result.route is None


# ---------------------------------------------------------------------------
# 7. read_settings on a directory with no settings.yaml -> None.
# ---------------------------------------------------------------------------


def test_read_settings_returns_none_when_file_absent(tmp_path: Path) -> None:
    assert read_settings(tmp_path) is None


# ---------------------------------------------------------------------------
# 8. read_settings on syntactically invalid YAML -> Settings with .error.
# ---------------------------------------------------------------------------


def test_read_settings_returns_error_on_malformed_yaml(tmp_path: Path) -> None:
    _write_settings(tmp_path, "malformed.yaml")

    result = read_settings(tmp_path)

    assert result is not None
    assert isinstance(result, Settings)
    assert result.error is not None
    assert result.error != ""
    assert result.routes == ()
    assert result.agent_default_model is None


def test_error_message_names_the_settings_path(tmp_path: Path) -> None:
    _write_settings(tmp_path, "malformed.yaml")

    result = read_settings(tmp_path)

    assert result is not None
    assert result.error is not None
    assert "settings.yaml" in result.error


# ---------------------------------------------------------------------------
# 9. Per-call re-read, no caching.
# ---------------------------------------------------------------------------


def test_read_settings_rereads_on_every_call(tmp_path: Path) -> None:
    _write_settings(tmp_path, "no_explicit_models.yaml")
    first = read_settings(tmp_path)
    assert first is not None
    first_keys = {r.key for r in first.routes}
    assert first_keys == {"kornerstone", "plain-route"}

    _write_settings(tmp_path, "full.yaml")
    second = read_settings(tmp_path)
    assert second is not None
    second_keys = {r.key for r in second.routes}
    assert second_keys == {"kornerstone", "amazon-bedrock"}
    assert second.agent_default_model is not None
    assert second.agent_default_model.provider == "amazon-bedrock"


def test_resolve_effective_model_rereads_on_every_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_swarm_env(monkeypatch)
    _write_settings(tmp_path, "no_explicit_models.yaml")

    first = resolve_effective_model(tmp_path)
    assert first.source == "bundle-default"

    _write_settings(tmp_path, "full.yaml")
    second = resolve_effective_model(tmp_path)
    assert second.source == "settings-default"
    assert second.provider == "amazon-bedrock"
    assert second.model == "us.anthropic.claude-opus-5"


# ---------------------------------------------------------------------------
# Review-driven additions.
# ---------------------------------------------------------------------------


def test_read_settings_empty_file_is_not_an_error(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("", encoding="utf-8")

    result = read_settings(tmp_path)

    assert result is not None
    assert result.error is None
    assert result.routes == ()
    assert result.agent_default_model is None


def test_read_settings_mis_shaped_providers_is_not_an_error(tmp_path: Path) -> None:
    _write_settings(tmp_path, "odd_shapes.yaml")

    result = read_settings(tmp_path)

    assert result is not None
    assert result.error is None
    assert result.routes == ()
    # agent-default-model is a normally-shaped mapping in this fixture,
    # so it still parses even though `providers` did not.
    assert result.agent_default_model == AgentDefaultModel(
        provider="amazon-bedrock",
        model="us.anthropic.claude-opus-5",
        reasoning_effort=None,
    )


def test_model_entry_with_extra_unknown_keys_is_tolerated(tmp_path: Path) -> None:
    _write_settings(tmp_path, "full.yaml")

    result = read_settings(tmp_path)

    assert result is not None
    assert result.error is None
    bedrock = next(r for r in result.routes if r.key == "amazon-bedrock")
    # contextWindow/maxTokens on each model entry must not raise and
    # must not appear anywhere on ModelInfo (it only has id/name).
    assert all(isinstance(m, ModelInfo) for m in bedrock.models)
    assert not hasattr(bedrock.models[0], "contextWindow")


def test_graph_override_with_unmatched_provider_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_settings(tmp_path, "full.yaml")
    _clear_swarm_env(monkeypatch)

    result = resolve_effective_model(
        tmp_path,
        graph_override=("totally-unconfigured-provider", "some-model", None),
    )

    assert result.source == "graph-override"
    assert result.provider == "totally-unconfigured-provider"
    assert result.model == "some-model"
    assert result.route is None
    assert result.base_url is None
    assert result.api_key_env is None


def test_malformed_settings_falls_through_to_env_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_settings(tmp_path, "malformed.yaml")
    monkeypatch.setenv("SWARM_MODEL", "deepseek:deepseek-v4-flash")
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)

    result = resolve_effective_model(tmp_path)

    assert result.source == "env-fallback"
    assert result.provider == "deepseek"
    assert result.model == "deepseek-v4-flash"


def test_env_fallback_no_colon_reports_env_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWARM_MODEL", "bare-model-id")
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)

    result = resolve_effective_model(tmp_path)

    assert result.source == "env-fallback"
    assert result.provider == "env"
    assert result.model == "bare-model-id"


def test_route_lookup_is_case_sensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # full.yaml's route is keyed "kornerstone" (lowercase); a
    # differently-cased override must NOT match it, per the plan's
    # explicit case-sensitive-exact-match rule.
    _write_settings(tmp_path, "full.yaml")
    _clear_swarm_env(monkeypatch)

    result = resolve_effective_model(
        tmp_path,
        graph_override=("Kornerstone", "qwen38-27b-fp8", None),
    )

    assert result.source == "graph-override"
    assert result.route is None
    assert result.base_url is None


def test_read_settings_returns_error_when_dsh_home_is_a_file(tmp_path: Path) -> None:
    # A dsh_home whose path collides with a plain file (so the
    # settings.yaml lookup hits NotADirectoryError, an OSError) must
    # report `.error`, not raise and not silently return None -- a
    # broken DSH_HOME is a different state from "no harness installed".
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")

    result = read_settings(blocker)

    assert result is not None
    assert result.error is not None
    assert result.routes == ()
    assert result.agent_default_model is None


def test_agent_default_model_dataclass_shape() -> None:
    # Cheap structural sanity check independent of any fixture, so a
    # future field rename on AgentDefaultModel/RouteConfig is caught
    # here even if every fixture-based test happens to still pass.
    default = AgentDefaultModel(provider="p", model="m", reasoning_effort="high")
    assert default.provider == "p"
    assert default.model == "m"
    assert default.reasoning_effort == "high"

    route = RouteConfig(
        key="k",
        api=None,
        base_url=None,
        api_key_env=None,
        aws_profile=None,
        aws_region=None,
        models=(),
    )
    assert route.models == ()

    effective = EffectiveModel(
        provider="p",
        model="m",
        reasoning_effort=None,
        base_url=None,
        api_key_env=None,
        source="bundle-default",
        route=None,
    )
    assert effective.source == "bundle-default"
