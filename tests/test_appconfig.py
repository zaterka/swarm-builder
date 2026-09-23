"""Unit tests for :mod:`swarm_builder.appconfig`.

The application's own settings file holds a credential, so the properties
these tests hold are the ones that keep it safe and predictable:

- it is written atomically and owner-readable only, from before its first byte;
- a missing file is a normal state (``None``), while a broken one is reported
  as an ``error`` rather than raising -- a request that merely wanted to know
  the current model must never 500 because a hand-edit mangled the JSON;
- unknown keys are tolerated (the file is hand-editable), and
- validation rejects every shape that would otherwise fail later, deeper, and
  less clearly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from swarm_builder.appconfig import (
    AppConfig,
    AppModelConfig,
    apply_update,
    config_path,
    load_config,
    save_config,
    spec_for,
    strip_userinfo,
    validate_model_config,
)


def test_missing_file_is_none(settings_path: Path) -> None:
    assert load_config(settings_path) is None


def test_round_trip_and_permissions(settings_path: Path) -> None:
    model = AppModelConfig(
        provider="openai", model="gpt-5.4-mini", api_key="sk-secret-value", reasoning_effort="low"
    )
    save_config(AppConfig(model=model, dry_run=True), settings_path)

    loaded = load_config(settings_path)
    assert loaded is not None
    assert loaded.error is None
    assert loaded.dry_run is True
    assert loaded.model == model
    assert loaded.has_api_key is True

    if os.name == "posix":  # pragma: no branch - the mode is POSIX-only
        assert (settings_path.stat().st_mode & 0o777) == 0o600
        assert not list(settings_path.parent.glob(f".{settings_path.name}.*"))


def test_document_shape_is_stable(settings_path: Path) -> None:
    save_config(
        AppConfig(model=AppModelConfig(provider="groq", model="llama-3.3-70b-versatile")),
        settings_path,
    )
    document = json.loads(settings_path.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["dryRun"] is False
    assert document["model"] == {
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
        "baseUrl": None,
        "apiKey": None,
        "reasoningEffort": None,
    }


def test_unknown_keys_are_tolerated(settings_path: Path) -> None:
    settings_path.write_text(
        json.dumps(
            {
                "version": 1,
                "futureSection": {"anything": [1, 2, 3]},
                "dryRun": True,
                "model": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "apiKey": "sk-x",
                    "someFutureField": 7,
                },
            }
        ),
        encoding="utf-8",
    )
    loaded = load_config(settings_path)
    assert loaded is not None
    assert loaded.error is None
    assert loaded.dry_run is True
    assert loaded.model is not None and loaded.model.provider == "deepseek"


def test_non_boolean_dry_run_is_off(settings_path: Path) -> None:
    """An ambiguous value must not silently switch the pipeline to stubs."""
    settings_path.write_text(json.dumps({"dryRun": "yes"}), encoding="utf-8")
    loaded = load_config(settings_path)
    assert loaded is not None
    assert loaded.dry_run is False


def test_broken_json_reports_error_without_raising(settings_path: Path) -> None:
    settings_path.write_text("{not json", encoding="utf-8")
    loaded = load_config(settings_path)
    assert loaded is not None
    assert loaded.error is not None
    assert "failed to parse" in loaded.error
    assert loaded.model is None


def test_non_object_document_reports_error(settings_path: Path) -> None:
    settings_path.write_text("[1, 2, 3]", encoding="utf-8")
    loaded = load_config(settings_path)
    assert loaded is not None
    assert loaded.error is not None


def test_unreadable_file_reports_error(settings_path: Path) -> None:
    """A directory where the file should be is an ``OSError``, not a
    ``FileNotFoundError`` -- it must surface as a reported error."""
    settings_path.mkdir(parents=True)
    loaded = load_config(settings_path)
    assert loaded is not None
    assert loaded.error is not None
    assert "failed to read" in loaded.error


def test_config_path_prefers_explicit_then_env_then_workspace(
    settings_path: Path, monkeypatch, tmp_path: Path
) -> None:
    assert config_path(settings_path) == settings_path

    monkeypatch.setenv("SWARM_CONFIG", str(tmp_path / "from-env.json"))
    assert config_path() == tmp_path / "from-env.json"

    monkeypatch.delenv("SWARM_CONFIG")
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "ws"))
    assert config_path() == tmp_path / "ws" / "settings.json"


def test_save_creates_the_workspace_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "workspace" / "settings.json"
    save_config(AppConfig(model=AppModelConfig(provider="openai", model="gpt-5")), target)
    assert target.is_file()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_valid_configs_have_no_problems() -> None:
    assert (
        validate_model_config(
            AppModelConfig(provider="openai", model="gpt-5", api_key="sk-x")
        )
        == []
    )
    assert (
        validate_model_config(
            AppModelConfig(provider="bedrock", model="us.anthropic.claude-sonnet-4-6")
        )
        == []
    )
    assert (
        validate_model_config(
            AppModelConfig(
                provider="custom",
                model="qwen3",
                base_url="http://127.0.0.1:8000/v1",
                api_key="sk-x",
            )
        )
        == []
    )


def test_unknown_provider_is_rejected_alone() -> None:
    problems = validate_model_config(AppModelConfig(provider="gemini", model="x"))
    assert len(problems) == 1
    assert "unknown provider" in problems[0]
    assert "openai" in problems[0]


def test_blank_model_id_is_rejected() -> None:
    problems = validate_model_config(AppModelConfig(provider="openai", model="   "))
    assert any("model id" in problem for problem in problems)


def test_custom_requires_a_base_url() -> None:
    problems = validate_model_config(
        AppModelConfig(provider="custom", model="qwen3", api_key="sk-x")
    )
    assert any("base URL is required" in problem for problem in problems)


def test_base_url_is_rejected_for_curated_providers() -> None:
    """A base URL on a curated provider would silently take the structural
    OpenAI path, which is not what a user picking 'Anthropic' asked for."""
    problems = validate_model_config(
        AppModelConfig(
            provider="anthropic", model="claude-sonnet-4-6", base_url="https://proxy/v1"
        )
    )
    assert any("only supported for the" in problem for problem in problems)


def test_base_url_must_be_http_and_carry_no_credentials() -> None:
    problems = validate_model_config(
        AppModelConfig(provider="custom", model="m", base_url="ftp://host/v1", api_key="k")
    )
    assert any("must start with http" in problem for problem in problems)

    problems = validate_model_config(
        AppModelConfig(
            provider="custom", model="m", base_url="https://user:sk-leak@host/v1", api_key="k"
        )
    )
    assert any("must not contain credentials" in problem for problem in problems)


def test_a_missing_api_key_is_not_a_save_time_problem() -> None:
    """Saving provider+model with no key is legitimate: the key may already be
    in the server's environment (an exported ``OPENAI_API_KEY``, a ``.env``
    file, a container secret). Whether a usable credential exists *now* is a
    runtime fact, reported by ``/api/health`` and by Test connection."""
    assert validate_model_config(AppModelConfig(provider="openai", model="gpt-5")) == []
    assert (
        validate_model_config(
            AppModelConfig(provider="bedrock", model="us.anthropic.claude-sonnet-4-6")
        )
        == []
    )


def test_strip_userinfo_only_touches_credentialled_urls() -> None:
    assert strip_userinfo(None) is None
    assert strip_userinfo("https://api.example.com/v1") == "https://api.example.com/v1"
    assert strip_userinfo("https://user:sk@api.example.com:8443/v1") == (
        "https://api.example.com:8443/v1"
    )


def test_spec_for_returns_none_for_an_unknown_provider() -> None:
    assert spec_for(AppModelConfig(provider="nope", model="x")) is None
    assert spec_for(AppModelConfig(provider="openai", model="x")) is not None


# ---------------------------------------------------------------------------
# apply_update: the merge rule the settings screen depends on
# ---------------------------------------------------------------------------


def test_update_without_a_key_keeps_the_stored_one() -> None:
    existing = AppModelConfig(provider="openai", model="gpt-5", api_key="sk-stored")
    updated = apply_update(existing, "openai", "gpt-5.4-mini")
    assert updated.model == "gpt-5.4-mini"
    assert updated.api_key == "sk-stored"


def test_update_replaces_a_key_when_one_is_typed() -> None:
    existing = AppModelConfig(provider="openai", model="gpt-5", api_key="sk-stored")
    updated = apply_update(existing, "openai", "gpt-5", api_key="  sk-new  ")
    assert updated.api_key == "sk-new"


def test_update_clears_a_key_on_request() -> None:
    existing = AppModelConfig(provider="openai", model="gpt-5", api_key="sk-stored")
    updated = apply_update(existing, "openai", "gpt-5", clear_api_key=True)
    assert updated.api_key is None


def test_changing_the_base_url_never_carries_a_key_across() -> None:
    """The custom provider is one key standing for *any* endpoint, so
    comparing the provider alone would let a stored key follow a changed base
    URL to a different host -- and the Test button would then send that host
    the credential."""
    existing = AppModelConfig(
        provider="custom",
        model="qwen3",
        base_url="https://api.mycorp.example/v1",
        api_key="sk-live",
    )

    updated = apply_update(existing, "custom", "qwen3", base_url="https://elsewhere.example/v1")

    assert updated.base_url == "https://elsewhere.example/v1"
    assert updated.api_key is None


def test_an_unchanged_endpoint_keeps_the_stored_key() -> None:
    existing = AppModelConfig(
        provider="custom",
        model="qwen3",
        base_url="http://127.0.0.1:8000/v1",
        api_key="sk-live",
    )

    updated = apply_update(
        existing, "custom", "qwen4", base_url="http://127.0.0.1:8000/v1"
    )

    assert updated.model == "qwen4"
    assert updated.api_key == "sk-live"


def test_a_blank_key_never_deletes_the_stored_one() -> None:
    """An untouched password field posts ``""``, and a stray space is one
    keystroke away from it: neither is a request to delete a credential."""
    existing = AppModelConfig(provider="openai", model="gpt-5", api_key="sk-stored")

    assert apply_update(existing, "openai", "gpt-5", api_key="").api_key == "sk-stored"
    assert apply_update(existing, "openai", "gpt-5", api_key="   ").api_key == "sk-stored"


def test_control_characters_are_rejected() -> None:
    """A model id is spliced into a generated project's .env.example as a
    whole line, so a newline in it would forge a credential line in an artifact
    the user ships."""
    smuggled = AppModelConfig(
        provider="openai", model="gpt-4o\nANTHROPIC_API_KEY=sk-attacker", api_key="sk-x"
    )
    problems = validate_model_config(smuggled)
    assert any("control characters" in problem for problem in problems)

    # …and the same value arriving from a hand-edited file is dropped at the
    # parse boundary, so it cannot win the precedence chain either.
    _write_raw({"model": {"provider": "openai", "model": "gpt-4o\nKEY=v"}})
    stored = load_config()
    assert stored is not None and stored.model is None


def test_a_control_character_in_a_base_url_is_rejected() -> None:
    problems = validate_model_config(
        AppModelConfig(
            provider="custom",
            model="qwen3",
            base_url="http://127.0.0.1:8000/v1\nANTHROPIC_API_KEY=sk-x",
            api_key="sk-y",
        )
    )
    assert any("control characters" in problem for problem in problems)


def _write_raw(document: object) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_switching_provider_never_carries_a_key_across() -> None:
    """A DeepSeek key must not end up published as ``OPENAI_API_KEY``."""
    existing = AppModelConfig(provider="deepseek", model="deepseek-v4-flash", api_key="sk-deep")
    updated = apply_update(existing, "openai", "gpt-5")
    assert updated.api_key is None


def test_update_trims_and_treats_blank_values_as_absent() -> None:
    updated = apply_update(None, "custom", "  qwen3  ", base_url="  ", api_key="   ")
    assert updated.model == "qwen3"
    assert updated.base_url is None
    assert updated.api_key is None
