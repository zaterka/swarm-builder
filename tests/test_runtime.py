"""Unit tests for :mod:`swarm_builder.runtime`.

This module decides two things the rest of the application must never
disagree about: whether the pipeline is in dry run, and which environment
variables carry a configured credential. Both are asserted here directly,
because a divergence shows up as a *silent* failure -- one real API call
inside a "dry" run, or a key published under a name no SDK reads.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from swarm_builder.appconfig import AppConfig, AppModelConfig, save_config
from swarm_builder.runtime import (
    DRY_RUN_ENV_VARS,
    RUN_TEST_MODEL_ENV_VAR,
    child_env_overrides,
    current_model_config,
    dry_run_active,
    dry_run_forced_by_env,
    dry_run_forced_env_vars,
    is_published_by_us,
    publish_secrets,
    secret_env_overrides,
    spec_for_config,
    temporary_secret_env,
)


@pytest.fixture(autouse=True)
def _no_dry_run_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DRY_RUN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_dry_run_off_by_default() -> None:
    assert dry_run_active() is False
    assert dry_run_forced_by_env() is False
    assert dry_run_forced_env_vars() == []


@pytest.mark.parametrize("name", DRY_RUN_ENV_VARS)
def test_each_env_var_forces_dry_run(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name, "1")
    assert dry_run_active() is True
    assert dry_run_forced_by_env() is True
    assert dry_run_forced_env_vars() == [name]


def test_env_var_value_must_be_exactly_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-existing convention, preserved byte-for-byte: only ``1``
    counts, so ``SWARM_FAKE_FILL=0`` is off rather than truthy."""
    monkeypatch.setenv("SWARM_FAKE_FILL", "0")
    assert dry_run_active() is False


def test_config_switch_turns_dry_run_on_without_any_env_var(settings_path: Path) -> None:
    save_config(AppConfig(dry_run=True), settings_path)
    assert dry_run_active() is True
    assert dry_run_forced_by_env() is False


def test_env_cannot_be_turned_off_by_the_switch(
    settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An offline or CI run that silently spent real credentials would be a
    far worse surprise than a locked switch."""
    save_config(AppConfig(dry_run=False), settings_path)
    monkeypatch.setenv("SWARM_RUN_TEST_MODEL", "1")
    assert dry_run_active() is True


def test_child_env_overrides_only_in_dry_run(settings_path: Path) -> None:
    assert child_env_overrides() == {}

    save_config(AppConfig(dry_run=True), settings_path)
    assert child_env_overrides() == {RUN_TEST_MODEL_ENV_VAR: "1"}


def test_current_model_config_ignores_a_broken_file(settings_path: Path) -> None:
    settings_path.write_text("{broken", encoding="utf-8")
    assert current_model_config() is None
    assert dry_run_active() is False


def test_secret_overrides_for_a_curated_provider(settings_path: Path) -> None:
    save_config(
        AppConfig(
            model=AppModelConfig(
                provider="anthropic", model="claude-sonnet-4-6", api_key="k1"
            )
        ),
        settings_path,
    )
    assert secret_env_overrides() == {"ANTHROPIC_API_KEY": "k1"}
    assert spec_for_config(current_model_config()).label == "Anthropic"


def test_secret_overrides_for_the_custom_endpoint(settings_path: Path) -> None:
    save_config(
        AppConfig(
            model=AppModelConfig(
                provider="custom",
                model="qwen3",
                base_url="http://127.0.0.1:8000/v1",
                api_key="k2",
            )
        ),
        settings_path,
    )
    assert secret_env_overrides() == {"SWARM_API_KEY": "k2"}


def test_no_secret_overrides_for_bedrock_or_a_keyless_model(settings_path: Path) -> None:
    save_config(
        AppConfig(model=AppModelConfig(provider="bedrock", model="us.anthropic.claude-sonnet-4-6")),
        settings_path,
    )
    assert secret_env_overrides() == {}

    save_config(AppConfig(model=AppModelConfig(provider="openai", model="gpt-5")), settings_path)
    assert secret_env_overrides() == {}


def test_publish_secrets_sets_names_and_returns_them(
    settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    save_config(
        AppConfig(model=AppModelConfig(provider="openai", model="gpt-5", api_key="k3")),
        settings_path,
    )
    assert publish_secrets(settings_path) == ["OPENAI_API_KEY"]
    assert os.environ["OPENAI_API_KEY"] == "k3"


def test_publish_secrets_retracts_what_it_published_before(
    settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing a key, changing provider, or repairing a broken file must
    actually stop the app (and every subprocess it spawns, which inherits this
    environment) from authenticating with the old credential."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    save_config(
        AppConfig(model=AppModelConfig(provider="openai", model="gpt-5", api_key="k1")),
        settings_path,
    )
    publish_secrets(settings_path)
    assert os.environ["OPENAI_API_KEY"] == "k1"

    save_config(AppConfig(dry_run=True), settings_path)
    assert publish_secrets(settings_path) == []
    assert "OPENAI_API_KEY" not in os.environ
    assert is_published_by_us("OPENAI_API_KEY") is False


def test_publish_secrets_retracts_across_a_provider_change(
    settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    save_config(
        AppConfig(model=AppModelConfig(provider="openai", model="gpt-5", api_key="k1")),
        settings_path,
    )
    publish_secrets(settings_path)

    save_config(
        AppConfig(
            model=AppModelConfig(provider="anthropic", model="claude-sonnet-4-6", api_key="k2")
        ),
        settings_path,
    )
    assert publish_secrets(settings_path) == ["ANTHROPIC_API_KEY"]
    assert "OPENAI_API_KEY" not in os.environ
    assert os.environ["ANTHROPIC_API_KEY"] == "k2"


def test_a_broken_file_retracts_a_previously_published_key(
    settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    save_config(
        AppConfig(model=AppModelConfig(provider="openai", model="gpt-5", api_key="k1")),
        settings_path,
    )
    publish_secrets(settings_path)

    settings_path.write_text("{broken", encoding="utf-8")
    assert publish_secrets(settings_path) == []
    assert "OPENAI_API_KEY" not in os.environ


def test_publish_secrets_never_unsets_a_foreign_variable(
    settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A variable this process did not set is not this function's to retract."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "exported-by-the-user")
    save_config(
        AppConfig(model=AppModelConfig(provider="openai", model="gpt-5", api_key="k4")),
        settings_path,
    )
    publish_secrets(settings_path)
    assert os.environ["DEEPSEEK_API_KEY"] == "exported-by-the-user"


def test_temporary_secret_env_restores_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "original")

    with temporary_secret_env({"OPENAI_API_KEY": "temp", "ANTHROPIC_API_KEY": "shadow"}):
        assert os.environ["OPENAI_API_KEY"] == "temp"
        assert os.environ["ANTHROPIC_API_KEY"] == "shadow"

    assert "OPENAI_API_KEY" not in os.environ
    assert os.environ["ANTHROPIC_API_KEY"] == "original"


def test_child_env_overrides_honours_an_explicit_decision(
    settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that froze the flag at job start passes it in, so the child is
    configured exactly as the rest of the job was even though the file has
    since changed."""
    save_config(AppConfig(dry_run=False), settings_path)
    assert child_env_overrides(dry_run=True) == {RUN_TEST_MODEL_ENV_VAR: "1"}

    save_config(AppConfig(dry_run=True), settings_path)
    assert child_env_overrides(dry_run=False) == {}


def test_temporary_secret_env_restores_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        with temporary_secret_env({"GROQ_API_KEY": "temp"}):
            raise RuntimeError("provider refused the key")
    assert "GROQ_API_KEY" not in os.environ
