"""Unit tests for :mod:`swarm_builder.providers`.

The registry is the single source of truth for "which providers exist", so
these tests hold the properties every other module relies on:

- a curated model suggestion must be a name the *installed* PydanticAI
  actually knows (otherwise the app would offer a suggestion that only fails
  once a user runs the exported project with real credentials);
- the flags that decide whether a key or a base URL is required must stay
  consistent with the fields the validator and the credential check read;
- every credential variable a provider can publish must be one the compile
  pipeline's keyless gate strips.
"""

from __future__ import annotations

import pytest

from swarm_builder.compile.validate import CREDENTIAL_ENV_VARS_TO_STRIP
from swarm_builder.providers import PROVIDERS, PROVIDERS_BY_KEY, ProviderSpec, models_are_known


def _spec(key: str) -> ProviderSpec:
    return PROVIDERS_BY_KEY[key]


def test_every_key_unique_and_lookup_consistent() -> None:
    keys = [spec.key for spec in PROVIDERS]
    assert len(keys) == len(set(keys))
    assert set(PROVIDERS_BY_KEY) == set(keys)


def test_every_curated_model_suggestion_is_a_known_model_name() -> None:
    """A suggestion that PydanticAI does not know would compile, pass the
    keyless gate, and fail only when the exported project ran for real."""
    for spec in PROVIDERS:
        assert models_are_known(spec), spec.key


@pytest.mark.parametrize("key", [spec.key for spec in PROVIDERS])
def test_flags_agree_with_declared_fields(key: str) -> None:
    spec = _spec(key)

    if spec.requires_api_key:
        assert spec.api_key_env, f"{key} requires a key but names no variable"
    if spec.requires_base_url:
        # The structural OpenAIChatModel path is what a base URL implies, so
        # the provider must not also claim a known-name prefix.
        assert spec.known_name_prefix is None
        assert spec.api is None
    else:
        assert spec.known_name_prefix is not None, f"{key} has no way to name a model"


def test_custom_endpoint_is_the_only_base_url_provider() -> None:
    base_url_providers = {spec.key for spec in PROVIDERS if spec.requires_base_url}
    assert base_url_providers == {"custom"}
    assert _spec("custom").api_key_env == "SWARM_API_KEY"


def test_bedrock_declares_no_api_key_env() -> None:
    """Bedrock's credentials come from the ambient AWS environment, and
    ``credential_blocker`` short-circuits the moment a route names an
    ``api_key_env`` -- declaring one would disable Run for a user whose
    ``AWS_PROFILE`` is perfectly configured."""
    bedrock = _spec("bedrock")
    assert bedrock.api_key_env is None
    assert bedrock.requires_api_key is False
    assert bedrock.note, "bedrock must explain where its credentials come from"


def test_publishable_variables_are_stripped_by_the_keyless_gate() -> None:
    """The validation gate proves a generated project imports and dry-runs
    with no credential present, so every variable the app can publish has to
    be in the strip list -- otherwise the gate would pass on the developer's
    own exported key rather than on the project's merits."""
    publishable = {spec.api_key_env for spec in PROVIDERS if spec.api_key_env}
    assert publishable <= set(CREDENTIAL_ENV_VARS_TO_STRIP)


def test_publishable_variables_match_the_credential_check() -> None:
    """``credential_blocker`` resolves an unknown prefix from its own table;
    a provider whose variable it did not know would report no blocker while a
    run silently failed to authenticate."""
    from swarm_builder.routes.health import _PROVIDER_CREDENTIAL_ENV_VARS

    for spec in PROVIDERS:
        if spec.api_key_env is None or spec.known_name_prefix is None:
            continue
        known = _PROVIDER_CREDENTIAL_ENV_VARS.get(spec.known_name_prefix)
        assert known, f"{spec.key}: health.py does not know {spec.known_name_prefix!r}"
        assert spec.api_key_env in known, f"{spec.key}: {spec.api_key_env} not in {known}"


def test_google_prefix_matches_the_installed_union() -> None:
    """PydanticAI shortened the Gemini developer-API prefix to ``google``;
    the legacy ``google-gla`` spelling must stay resolvable in
    ``inherit.routes`` so an older inherited route does not become
    unmappable, but the picker writes the current one."""
    assert _spec("google").known_name_prefix == "google"
    assert models_are_known(_spec("google"))
