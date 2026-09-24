"""Whether the resolved model can actually read an image.

**Why a table and not a guess.** An image attachment is the one file kind whose
content is not text: it reaches the model as image content or not at all. Sending
bytes to a text-only model produces a provider error in the middle of a
generation -- after the user has waited, after the retries, with a message
written for a developer. Deciding up front turns that into a sentence naming the
model and the two ways out.

**Why conservative.** The accepted set is closed and sourced from vendor
documentation: ids the docs name as vision-capable are accepted, the family
prefixes whose whole line is multimodal are accepted, and everything else is
refused with a clear message. A false negative therefore costs a user one
message and one click; a false positive costs them a failed generation and a raw
provider error. When a vendor marks another id visual, this table (plus its test
row in ``tests/test_vision.py``) is the single edit.

**Exact ids, not prefixes, for the exception.** ``deepseek-v4-flash-vision-exp``
begins with ``deepseek-v4-flash``, which is a *different*, text-only id -- so the
accepted DeepSeek names are matched exactly, and the family prefixes are the only
substring rules.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Model ids documented by their vendor as accepting image input, matched
#: exactly (lowercased, after prefix normalization). DeepSeek's Vision guide
#: states that ``deepseek-flash`` accepts images alongside text and that the
#: retired ``deepseek-v4-flash-vision-exp`` name is still accepted and served by
#: the latest Flash model; ``deepseek-v4-1-flash`` is the same model under its
#: versioned name.
_VISION_MODEL_IDS: frozenset[str] = frozenset(
    {
        "deepseek-flash",
        "deepseek-v4-flash-vision-exp",
        "deepseek-v4-1-flash",
        "deepseek-v4.1-flash",
    }
)

#: Model-id prefixes whose whole line accepts images.
_VISION_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-",
    "gpt-",
    "o3",
    "o4",
    "gemini-",
    "pixtral-",
)

#: Bedrock prefixes a model id may carry before the actual model name.
_REGION_PREFIXES: tuple[str, ...] = ("us-gov.", "us.", "eu.", "apac.", "global.")

#: Bedrock keeps the vendor as a further segment (``us.anthropic.claude-...``),
#: so it is stripped after the region. An explicit list rather than "split on the
#: first dot", because versioned ids legitimately contain dots
#: (``deepseek-v4.1-flash``).
_VENDOR_PREFIXES: tuple[str, ...] = (
    "anthropic.",
    "amazon.",
    "meta.",
    "mistral.",
    "cohere.",
    "ai21.",
    "stability.",
    "openai.",
    "writer.",
    "deepseek.",
    "qwen.",
)


@dataclass(frozen=True)
class ImageSupport:
    """The verdict, plus the sentence to show the user either way."""

    supported: bool
    reason: str


def normalize_model_id(model: str) -> str:
    """Reduce a model reference to the bare, lowercased model name.

    Strips a ``provider:`` prefix (``deepseek:deepseek-flash``) and a Bedrock
    region prefix (``us.anthropic.claude-sonnet-4-6``), both of which are routing
    detail rather than model identity.

    Args:
        model: The model id as it appears in a route.

    Returns:
        The normalized id, possibly empty for an empty input.
    """
    text = (model or "").strip().lower()
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    for prefix in _REGION_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    for prefix in _VENDOR_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def supports_image_input(provider: str, model: str) -> ImageSupport:
    """Decide whether ``provider``/``model`` can be sent image content.

    Called for every attachment of kind ``image`` -- at upload time, so the user
    hears about it immediately, and again when a generation or analysis request
    names attachments, because the resolved model may have changed in between.

    Args:
        provider: The provider (or route key) the model resolved from, used only
            to name the model in the message.
        model: The model id.

    Returns:
        Whether image input is accepted, and a user-facing sentence explaining it.
    """
    normalized = normalize_model_id(model)
    capable = normalized in _VISION_MODEL_IDS or normalized.startswith(_VISION_MODEL_PREFIXES)
    if capable:
        return ImageSupport(
            supported=True,
            reason=f"{provider} / {model} accepts image input.",
        )
    return ImageSupport(
        supported=False,
        reason=(
            f"{provider} / {model} does not accept image input, so an attached image "
            "could not be read. Remove the image, or pick a vision-capable model in "
            "Model settings — for example Anthropic Claude, OpenAI GPT, Google Gemini, "
            "or DeepSeek Flash."
        ),
    )


__all__ = ["ImageSupport", "normalize_model_id", "supports_image_input"]
