"""The curated list of model providers the in-app model picker offers.

**Why this module exists.** Before this module, a model route could only come
from an inherited harness ``settings.yaml`` or from ``SWARM_MODEL`` -- both of
which require the user to know about variables and files that live outside
this application. The picker needs a *closed*, app-owned vocabulary: a set of
provider keys the app can actually build a live model for, with the credential
variable each one reads and the protocol each one speaks.

**The single source of truth.** Every other module that needs to know "which
providers exist" (``appconfig`` validation, ``runtime`` secret publication,
``routes/settings.py``'s response, ``routes/health.py``'s credential checks)
reads it from :data:`PROVIDERS` here. Adding a provider is a change to this
file plus (when it is not already pinned) an extra in ``pyproject.toml`` --
never a change spread across the routes.

**Why ``known_name_prefix`` and ``api_key_env`` are separate from ``key``.**
The picker's provider key is *this app's* identifier (``google``, ``custom``),
while PydanticAI's known-name prefix is *the library's* (``google``) and the
credential variable is *the vendor's* (``GOOGLE_API_KEY``). Collapsing them
into one string is what makes a UI label leak into a model id, so all three
are declared explicitly.

**Why the curated model lists are drawn from the installed union.** PydanticAI
resolves a known-name string against a union pinned to the installed version.
A suggestion that is not in that union would compile fine (the keyless gate
injects ``TestModel``) and then fail only when a user ran the exported project
with real credentials -- the exact silent failure ``compile/pipeline.py``'s
``unknown_model_name`` warning exists to catch. :func:`models_are_known`
asserts membership so a test can hold every suggestion to it.

**Why ``bedrock`` has no ``api_key_env``.** ``routes/health.py``'s
``credential_blocker`` short-circuits the moment a route names an
``api_key_env``, so declaring ``AWS_BEARER_TOKEN_BEDROCK`` here would report
"Run disabled" to a user whose ``AWS_PROFILE`` is perfectly configured. Bedrock
keeps exactly the behaviour it has today for harness routes: an AWS profile
satisfies the check, and its credentials otherwise come from the ambient AWS
environment. The picker therefore shows bedrock's credentials as a note, not a
field.
"""

from __future__ import annotations

from dataclasses import dataclass

from swarm_builder.known_models import is_known_model_name


@dataclass(frozen=True)
class ProviderSpec:
    """One provider the in-app model picker can configure.

    ``key`` is this application's stable identifier: it is what
    ``workspace/settings.json`` stores, what the HTTP API accepts and
    returns, and what :func:`swarm_builder.appconfig.route_config_for` turns
    into a :class:`~swarm_builder.inherit.settings.RouteConfig` key. It is
    never shown to a user (``label`` is).
    """

    key: str
    label: str
    #: PydanticAI's known-name prefix for this provider (``openai:gpt-5``),
    #: or ``None`` for a provider that is only reachable structurally -- the
    #: custom OpenAI-compatible endpoint, which requires a ``base_url``.
    known_name_prefix: str | None
    #: The wire protocol recorded on the synthesized route. ``None`` means
    #: "let the prefix-based extras fallback decide", which is correct for
    #: the known-name providers and required for the custom endpoint (whose
    #: structural path demands a protocol that is either unset or
    #: OpenAI-compatible).
    api: str | None
    #: The environment variable this provider's SDK reads for its key, or
    #: ``None`` when the provider does not take one (bedrock).
    api_key_env: str | None
    requires_api_key: bool
    requires_base_url: bool
    #: Curated suggestions, in picker order. Every entry must be a member of
    #: the installed ``KnownModelName`` union under ``known_name_prefix``.
    models: tuple[str, ...]
    default_model: str | None
    note: str | None


#: Every provider the picker offers, in display order. This tuple is the
#: whole registry: nothing else in the application enumerates providers.
PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        key="openai",
        label="OpenAI",
        known_name_prefix="openai",
        api="openai-completions",
        api_key_env="OPENAI_API_KEY",
        requires_api_key=True,
        requires_base_url=False,
        models=(
            "gpt-6-astra",
            "gpt-5.6-luna",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.4-mini",
        ),
        default_model="gpt-6-astra",
        note=None,
    ),
    ProviderSpec(
        key="anthropic",
        label="Anthropic",
        known_name_prefix="anthropic",
        api="anthropic-messages",
        api_key_env="ANTHROPIC_API_KEY",
        requires_api_key=True,
        requires_base_url=False,
        models=(
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        ),
        default_model="claude-sonnet-5",
        note=None,
    ),
    ProviderSpec(
        key="deepseek",
        label="DeepSeek",
        known_name_prefix="deepseek",
        api="openai-completions",
        api_key_env="DEEPSEEK_API_KEY",
        requires_api_key=True,
        requires_base_url=False,
        models=(
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-chat",
            "deepseek-reasoner",
        ),
        default_model="deepseek-v4-flash",
        note=None,
    ),
    ProviderSpec(
        key="google",
        label="Google Gemini",
        known_name_prefix="google",
        api=None,
        api_key_env="GOOGLE_API_KEY",
        requires_api_key=True,
        requires_base_url=False,
        models=(
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-3.8-flash",
            "gemini-3.1-pro-preview",
        ),
        # The newest Gemini generation is offered in the list, but the default
        # stays on the model with the widest availability: a pre-filled id that
        # a fresh key cannot call is a bad first impression, and unlike OpenAI
        # and Anthropic this default has not been exercised against a live key
        # in this checkout.
        default_model="gemini-2.5-flash",
        note="Google's SDK also accepts GEMINI_API_KEY.",
    ),
    ProviderSpec(
        key="groq",
        label="Groq",
        known_name_prefix="groq",
        api=None,
        api_key_env="GROQ_API_KEY",
        requires_api_key=True,
        requires_base_url=False,
        models=(
            "llama-3.3-70b-versatile",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "llama-3.1-8b-instant",
        ),
        default_model="llama-3.3-70b-versatile",
        note=None,
    ),
    ProviderSpec(
        key="mistral",
        label="Mistral",
        known_name_prefix="mistral",
        api=None,
        api_key_env="MISTRAL_API_KEY",
        requires_api_key=True,
        requires_base_url=False,
        models=(
            "mistral-large-latest",
            "mistral-small-latest",
            "codestral-latest",
        ),
        default_model="mistral-large-latest",
        note=None,
    ),
    ProviderSpec(
        key="bedrock",
        label="Amazon Bedrock",
        known_name_prefix="bedrock",
        api="bedrock-converse-stream",
        # Deliberately None: see this module's docstring. Bedrock's
        # credentials come from the ambient AWS environment (AWS_PROFILE,
        # AWS_ACCESS_KEY_ID/SECRET, or a task/instance role), and the app
        # must never tell a correctly-configured AWS user that Run is
        # disabled because one specific variable is unset.
        api_key_env=None,
        requires_api_key=False,
        requires_base_url=False,
        models=(
            "us.anthropic.claude-sonnet-4-6",
            "us.anthropic.claude-sonnet-5",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us.anthropic.claude-opus-4-5-20251101-v1:0",
        ),
        # Bedrock model availability is per-region and per-account, so the
        # default stays on the long-available id and the newest is offered
        # alongside it.
        default_model="us.anthropic.claude-sonnet-4-6",
        note=(
            "Uses your AWS credentials (AWS_PROFILE, or AWS_ACCESS_KEY_ID and "
            "AWS_SECRET_ACCESS_KEY) and the region from AWS_REGION."
        ),
    ),
    ProviderSpec(
        key="custom",
        label="Custom OpenAI-compatible endpoint",
        known_name_prefix=None,
        api=None,
        api_key_env="SWARM_API_KEY",
        requires_api_key=True,
        requires_base_url=True,
        models=(),
        default_model=None,
        note=(
            "Any server that speaks the OpenAI chat-completions API (vLLM, "
            "Ollama, LM Studio, OpenRouter, a corporate gateway)."
        ),
    ),
)

#: Provider key -> spec, for the constant-time lookup every validator wants.
PROVIDERS_BY_KEY: dict[str, ProviderSpec] = {spec.key: spec for spec in PROVIDERS}


def models_are_known(spec: ProviderSpec) -> bool:
    """Report whether every curated suggestion is a real PydanticAI model name.

    A spec with no ``known_name_prefix`` (the custom endpoint) has nothing to
    check: its model ids belong to someone else's server, and membership in
    PydanticAI's union says nothing about them. An empty suggestion list is
    likewise trivially fine -- the picker falls back to free text.
    """
    if spec.known_name_prefix is None:
        return True
    return all(
        is_known_model_name(f"{spec.known_name_prefix}:{model}") for model in spec.models
    )


__all__ = [
    "PROVIDERS",
    "PROVIDERS_BY_KEY",
    "ProviderSpec",
    "models_are_known",
]
