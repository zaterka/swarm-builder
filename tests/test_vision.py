"""``swarm_builder.vision``: which models may be sent image content.

A table, deliberately: this is the one decision that turns "an image you
attached will be read" into either a working generation or a provider error, and
the value of a table is that adding a vendor-documented model is a one-line
change with a test row next to it.

The rows naming the live route matter most: the app resolves to
``deepseek-official / deepseek-flash`` today, and DeepSeek's own docs say that id
accepts images. A regression there would silently turn image support into a
refusal for every existing user.
"""

from __future__ import annotations

import pytest

from swarm_builder.vision import ImageSupport, normalize_model_id, supports_image_input

#: (provider, model, expected) rows. The provider never decides on its own -- it
#: is only named in the message -- so the same model id under a different
#: provider string must keep the same verdict.
VISION_TABLE: list[tuple[str, str, bool]] = [
    # The route this app resolves to today, plus the ids the docs name.
    ("deepseek-official", "deepseek-flash", True),
    ("deepseek", "deepseek-flash", True),
    ("deepseek", "deepseek-v4-flash-vision-exp", True),
    ("deepseek", "deepseek-v4-1-flash", True),
    ("deepseek", "deepseek-v4.1-flash", True),
    # Other DeepSeek ids the picker offers: never documented as visual, so the
    # app refuses up front rather than sending bytes a text-only model rejects.
    ("deepseek", "deepseek-v4-flash", False),
    ("deepseek", "deepseek-v4-pro", False),
    ("deepseek", "deepseek-chat", False),
    ("deepseek", "deepseek-reasoner", False),
    # Family prefixes: multimodal across the whole line.
    ("anthropic", "claude-sonnet-5", True),
    ("bedrock", "us.anthropic.claude-sonnet-4-6", True),
    ("bedrock", "eu.anthropic.claude-opus-5", True),
    ("openai", "gpt-5.6-sol", True),
    ("openai", "gpt-5.4-mini", True),
    ("openai", "o3-mini", True),
    ("google", "gemini-2.5-flash", True),
    ("mistral", "pixtral-large-latest", True),
    # Text-only curated picks.
    ("groq", "llama-3.3-70b-versatile", False),
    ("mistral", "mistral-large-latest", False),
    # A custom OpenAI-compatible endpoint is judged by the model id alone: it
    # commonly proxies a vision model under its real name.
    ("custom", "gpt-4o", True),
    ("custom", "my-local-llama", False),
    # Anything unrecognised is refused, which can only ever cost one message.
    ("nonsense-provider", "mystery-1", False),
    ("openai", "", False),
]


@pytest.mark.parametrize(("provider", "model", "expected"), VISION_TABLE)
def test_supports_image_input_matches_the_table(provider: str, model: str, expected: bool) -> None:
    support = supports_image_input(provider, model)
    assert support.supported is expected, support.reason
    assert provider in support.reason
    if not expected:
        # The message must name both ways out, otherwise a user is stuck.
        assert "Remove the image" in support.reason
        assert "Model settings" in support.reason


def test_a_refusal_is_not_silent_degradation() -> None:
    """The refusal names the model and a vision-capable alternative."""
    support = supports_image_input("deepseek-official", "deepseek-v4-flash")
    assert isinstance(support, ImageSupport)
    assert support.supported is False
    assert "deepseek-official / deepseek-v4-flash" in support.reason
    for alternative in ("Claude", "GPT", "Gemini", "DeepSeek Flash"):
        assert alternative in support.reason


def test_exact_ids_win_over_a_longer_prefix() -> None:
    """``deepseek-v4-flash-vision-exp`` must not be read as ``deepseek-v4-flash``."""
    assert supports_image_input("deepseek", "deepseek-v4-flash-vision-exp").supported is True
    assert supports_image_input("deepseek", "deepseek-v4-flash").supported is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("deepseek:deepseek-flash", "deepseek-flash"),
        # Bedrock carries a region *and* a vendor segment; both go.
        ("us.anthropic.claude-sonnet-4-6", "claude-sonnet-4-6"),
        ("US-GOV.anthropic.claude-sonnet-4-6", "claude-sonnet-4-6"),
        ("  OpenAI:Gpt-5.6-Sol  ", "gpt-5.6-sol"),
        ("gemini-2.5-flash", "gemini-2.5-flash"),
        # A versioned id keeps its internal dot: only a known vendor prefix goes.
        ("deepseek-v4.1-flash", "deepseek-v4.1-flash"),
        ("", ""),
    ],
)
def test_normalize_model_id_strips_routing_prefixes(raw: str, expected: str) -> None:
    assert normalize_model_id(raw) == expected
