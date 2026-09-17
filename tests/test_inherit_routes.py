"""Unit tests for :mod:`swarm_builder.inherit.routes` (PLAN.md facts 22,
23, 30; "Harness route -> PydanticAI emission").

Every :class:`RouteConfig`/:class:`EffectiveModel` fixture is built
directly in this file with explicit keyword arguments -- there is no
dependency on any real ``settings.yaml`` or on
``swarm_builder.inherit.settings``'s own parsing/precedence tests,
which already live in ``tests/test_inherit_settings.py``.

**The trap test (test 3 below) is the load-bearing one in this file**:
the ``kornerstone`` route's model id ``deepseek-v4-flash`` happens to
ALSO be a member of PydanticAI's ``deepseek:`` known-name family, but
``kornerstone`` declares an explicit ``base_url`` pointing at a
self-hosted endpoint. If :func:`build_live_model` ever picked the
known-name-string path merely because the id matched, it would silently
route this traffic to the official DeepSeek API instead of the user's
self-hosted server. The test asserts the structural path is used
regardless, and that the base_url actually wired into the constructed
``OpenAIChatModel`` is the ``kornerstone`` one.
"""

from __future__ import annotations

import ast

import pytest
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from swarm_builder.inherit.routes import (
    LiveModel,
    RouteEmission,
    UnmappableRouteError,
    build_live_model,
    classify_route,
    to_resolved_model,
)
from swarm_builder.inherit.settings import EffectiveModel, RouteConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _route(
    key: str,
    *,
    api: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    aws_profile: str | None = None,
    aws_region: str | None = None,
) -> RouteConfig:
    return RouteConfig(
        key=key,
        api=api,
        base_url=base_url,
        api_key_env=api_key_env,
        aws_profile=aws_profile,
        aws_region=aws_region,
        models=(),
    )


def _effective(
    *,
    provider: str,
    model: str,
    route: RouteConfig | None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    reasoning_effort: str | None = None,
    source: str = "settings-default",
) -> EffectiveModel:
    return EffectiveModel(
        provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,
        base_url=base_url,
        api_key_env=api_key_env,
        source=source,  # type: ignore[arg-type]
        route=route,
    )


def _anthropic_available() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


ANTHROPIC_AVAILABLE = _anthropic_available()


# ---------------------------------------------------------------------------
# 1. Known-name route (bedrock).
# ---------------------------------------------------------------------------


def test_known_name_bedrock_route() -> None:
    route = _route("amazon-bedrock", api="bedrock-converse-stream", base_url=None)
    effective = _effective(
        provider="amazon-bedrock",
        model="us.anthropic.claude-opus-5",
        route=route,
    )

    live = build_live_model(effective)

    assert live.model == "bedrock:us.anthropic.claude-opus-5"
    assert isinstance(live.model, str)
    assert live.pyproject_extras == ("bedrock",)


# ---------------------------------------------------------------------------
# 2. Custom baseURL route (kornerstone / qwen38-27b-fp8).
# ---------------------------------------------------------------------------


def test_custom_base_url_route_is_structural() -> None:
    route = _route(
        "kornerstone",
        api="openai-completions",
        base_url="http://localhost:8000/v1",
        api_key_env="KORNERSTONE_API_KEY",
    )
    effective = _effective(
        provider="kornerstone",
        model="qwen38-27b-fp8",
        route=route,
    )

    live = build_live_model(effective)

    assert isinstance(live.model, OpenAIChatModel)
    assert live.pyproject_extras == ("openai",)


# ---------------------------------------------------------------------------
# 3. THE TRAP TEST (mandatory): kornerstone's model id collides with a
#    real KnownModelName from an unrelated provider (deepseek).
# ---------------------------------------------------------------------------


def test_base_url_wins_over_known_name_collision() -> None:
    route = _route(
        "kornerstone",
        api="openai-completions",
        base_url="http://localhost:8000/v1",
        api_key_env="KORNERSTONE_API_KEY",
    )
    effective = _effective(
        provider="kornerstone",
        model="deepseek-v4-flash",
        route=route,
    )

    live = build_live_model(effective)

    # Must be the structural path, NOT the string "deepseek:deepseek-v4-flash".
    assert isinstance(live.model, OpenAIChatModel)
    assert live.model != "deepseek:deepseek-v4-flash"

    # The base_url actually wired in must be kornerstone's, not any
    # DeepSeek default.
    reference = OpenAIChatModel(
        "deepseek-v4-flash",
        provider=OpenAIProvider(
            base_url="http://localhost:8000/v1",
            api_key="unset-placeholder-key",
        ),
    )
    # Compare the effective base_url through each model's own client,
    # which is the only reliably-introspectable attribute across
    # pydantic-ai versions.
    live_base_url = str(live.model.client.base_url)
    reference_base_url = str(reference.client.base_url)
    assert live_base_url == reference_base_url
    assert "localhost:8000" in live_base_url
    assert "deepseek.com" not in live_base_url


def test_to_resolved_model_also_respects_base_url_wins_on_the_same_collision() -> None:
    """The generated-project rendering path (`to_resolved_model`) shares
    `_resolve_emission` with `build_live_model` precisely so this
    collision cannot resolve differently between the two -- pin that
    explicitly, since `build_live_model`'s own collision test does not
    exercise `to_resolved_model` at all, and a regression there would
    silently ship a WRONG endpoint into every compiled project's
    `deps.py` while the live in-process compile agent stayed correct."""
    route = _route(
        "kornerstone",
        api="openai-completions",
        base_url="http://localhost:8000/v1",
        api_key_env="KORNERSTONE_API_KEY",
    )
    effective = _effective(
        provider="kornerstone",
        model="deepseek-v4-flash",
        route=route,
    )

    resolved = to_resolved_model(effective)

    assert resolved.pyproject_extras == ("openai",)
    assert "deepseek:deepseek-v4-flash" not in resolved.helper_source
    assert "http://localhost:8000/v1" in resolved.helper_source
    assert "OpenAIChatModel(" in resolved.helper_source
    assert "OpenAIProvider(" in resolved.helper_source


# ---------------------------------------------------------------------------
# 4. Each api protocol, no baseURL -> correct pyproject_extras.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("api", "expected_extras"),
    [
        ("openai-completions", ("openai",)),
        ("openai-responses", ("openai",)),
        ("bedrock-converse-stream", ("bedrock",)),
    ],
)
def test_protocol_extras_no_base_url(api: str, expected_extras: tuple[str, ...]) -> None:
    route = _route("openai", api=api, base_url=None) if api.startswith("openai") else _route(
        "amazon-bedrock", api=api, base_url=None
    )
    provider = route.key

    classification = classify_route(route)
    assert classification.emission == "known-name"
    assert classification.required_extra == expected_extras[0]

    effective = _effective(
        provider=provider,
        model="some-model-id" if provider != "amazon-bedrock" else "us.anthropic.claude-opus-5",
        route=route,
    )
    live = build_live_model(effective)
    assert live.pyproject_extras == expected_extras


def test_anthropic_protocol_extras_no_base_url() -> None:
    route = _route("anthropic", api="anthropic-messages", base_url=None)
    effective = _effective(provider="anthropic", model="claude-3-5-sonnet-latest", route=route)

    classification = classify_route(route)
    assert classification.emission == "known-name"
    assert classification.required_extra == "anthropic"

    if ANTHROPIC_AVAILABLE:
        live = build_live_model(effective)
        assert live.pyproject_extras == ("anthropic",)
    else:
        with pytest.raises(UnmappableRouteError) as exc_info:
            build_live_model(effective)
        assert "anthropic" in str(exc_info.value)
        assert "anthropic" in exc_info.value.provider


# ---------------------------------------------------------------------------
# 5. Unmappable protocol.
# ---------------------------------------------------------------------------


def test_unmappable_protocol_raises() -> None:
    route = _route("some-provider", api="some-future-protocol", base_url=None)
    effective = _effective(provider="some-provider", model="whatever", route=route)

    with pytest.raises(UnmappableRouteError) as exc_info:
        build_live_model(effective)

    assert exc_info.value.provider == "some-provider"
    assert exc_info.value.api == "some-future-protocol"
    assert "some-provider" in str(exc_info.value)


def test_unmappable_protocol_via_classify_route() -> None:
    route = _route("some-provider", api="some-future-protocol", base_url=None)
    classification = classify_route(route)
    assert classification.emission == "unmappable"
    assert classification.unmappable_reason is not None


# ---------------------------------------------------------------------------
# 6. Unknown provider, no baseURL.
# ---------------------------------------------------------------------------


def test_unknown_provider_no_base_url_raises() -> None:
    route = _route("totally-unknown-provider", api=None, base_url=None)
    effective = _effective(provider="totally-unknown-provider", model="some-model", route=route)

    with pytest.raises(UnmappableRouteError) as exc_info:
        build_live_model(effective)

    assert exc_info.value.provider == "totally-unknown-provider"


def test_unknown_provider_via_classify_route() -> None:
    route = _route("totally-unknown-provider", api=None, base_url=None)
    classification = classify_route(route)
    assert classification.emission == "unmappable"


# ---------------------------------------------------------------------------
# 7. route=None, env-fallback, known prefix ("bedrock").
# ---------------------------------------------------------------------------


def test_env_fallback_route_none_known_prefix() -> None:
    effective = _effective(
        provider="bedrock",
        model="us.anthropic.claude-sonnet-5",
        route=None,
        base_url=None,
        source="env-fallback",
    )

    live = build_live_model(effective)

    assert live.model == "bedrock:us.anthropic.claude-sonnet-5"
    assert isinstance(live.model, str)
    assert live.pyproject_extras == ("bedrock",)


# ---------------------------------------------------------------------------
# 8. route=None, bundle-default -> to_resolved_model known-name divergence pin.
# ---------------------------------------------------------------------------


def test_bundle_default_to_resolved_model() -> None:
    effective = _effective(
        provider="deepseek-official",
        model="deepseek-v4-flash",
        route=None,
        base_url=None,
        source="bundle-default",
    )

    resolved = to_resolved_model(effective)

    assert resolved.pyproject_extras == ("openai",)
    assert "deepseek:deepseek-v4-flash" in resolved.helper_source


# ---------------------------------------------------------------------------
# 9. to_resolved_model structural path: literal source shape + syntax validity.
# ---------------------------------------------------------------------------


def test_to_resolved_model_structural_shape_and_syntax() -> None:
    route = _route(
        "kornerstone",
        api="openai-completions",
        base_url="http://localhost:8000/v1",
        api_key_env="KORNERSTONE_API_KEY",
    )
    effective = _effective(provider="kornerstone", model="qwen38-27b-fp8", route=route)

    resolved = to_resolved_model(effective)

    assert "OpenAIChatModel(" in resolved.helper_source
    assert "OpenAIProvider(" in resolved.helper_source
    assert (
        "from pydantic_ai.models.openai import OpenAIChatModel" in resolved.extra_imports
    )
    assert (
        "from pydantic_ai.providers.openai import OpenAIProvider" in resolved.extra_imports
    )
    assert resolved.pyproject_extras == ("openai",)

    full_source = "\n".join(resolved.extra_imports) + "\n\n" + resolved.helper_source
    ast.parse(full_source)  # raises SyntaxError if invalid


def test_to_resolved_model_known_name_shape_and_syntax() -> None:
    effective = _effective(
        provider="amazon-bedrock",
        model="us.anthropic.claude-opus-5",
        route=_route("amazon-bedrock", api="bedrock-converse-stream", base_url=None),
    )

    resolved = to_resolved_model(effective)

    assert "bedrock:us.anthropic.claude-opus-5" in resolved.helper_source
    assert resolved.extra_imports == ()
    assert resolved.pyproject_extras == ("bedrock",)

    ast.parse(resolved.helper_source)  # raises SyntaxError if invalid


def test_to_resolved_model_model_id_with_quote_is_safely_repr_d() -> None:
    """A model id containing a quote character must never be raw-
    interpolated into the generated source -- it must go through
    ``repr`` so the emitted Python stays syntactically valid."""
    effective = _effective(
        provider="amazon-bedrock",
        model='weird"model',
        route=_route("amazon-bedrock", api="bedrock-converse-stream", base_url=None),
    )

    resolved = to_resolved_model(effective)

    # Must not blow up, and must parse as valid Python.
    ast.parse(resolved.helper_source)
    expected_literal = repr("bedrock:weird\"model")
    assert expected_literal in resolved.helper_source or "weird" in resolved.helper_source


# ---------------------------------------------------------------------------
# 10. Env-fallback with explicit SWARM_BASE_URL, unknown provider ("custom").
# ---------------------------------------------------------------------------


def test_env_fallback_explicit_base_url_unknown_provider() -> None:
    effective = EffectiveModel(
        provider="custom",
        model="my-model-id",
        reasoning_effort=None,
        base_url="https://example.invalid/v1",
        api_key_env="MY_KEY",
        route=None,
        source="env-fallback",
    )

    live = build_live_model(effective)

    assert isinstance(live.model, OpenAIChatModel)
    assert live.pyproject_extras == ("openai",)
    live_base_url = str(live.model.client.base_url)
    assert "example.invalid" in live_base_url


# ---------------------------------------------------------------------------
# Additional coverage: RouteEmission/LiveModel dataclass shape sanity.
# ---------------------------------------------------------------------------


def test_route_emission_and_live_model_are_frozen_dataclasses() -> None:
    emission = RouteEmission(emission="known-name", required_extra="openai", unmappable_reason=None)
    with pytest.raises(AttributeError):
        emission.emission = "structural"  # type: ignore[misc]

    live = LiveModel(
        model="bedrock:x",
        pyproject_extras=("bedrock",),
        reasoning_effort=None,
        source_description="x",
    )
    with pytest.raises(AttributeError):
        live.model = "other"  # type: ignore[misc]
