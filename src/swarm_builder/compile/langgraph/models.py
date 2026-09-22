"""Map the inherited model route to a LangChain chat model.

The pydantic-graph project gets its model through
:func:`swarm_builder.inherit.routes.to_resolved_model`. A LangGraph
project needs the *LangChain* spelling of the same route: a
``model_provider`` string for ``init_chat_model`` plus the partner
package that provides it, or -- for a custom OpenAI-compatible endpoint --
``ChatOpenAI(base_url=...)``.

The decision procedure (base URL wins; otherwise the known-name prefix)
is reused from ``inherit/routes.py`` so the two targets can never
disagree about which route a compile spends. Only the rendering differs.
Provider strings and packages come from the LangChain 1.x docs
(``init_chat_model`` accepts ``model_provider`` values such as ``openai``,
``anthropic``, ``deepseek``, ``bedrock_converse``), and each package
version is the one ``uv`` resolved on Python 3.14 on 2026-09-22.
"""

from __future__ import annotations

from dataclasses import dataclass

from swarm_builder.inherit.routes import UnmappableRouteError, _resolve_emission
from swarm_builder.inherit.settings import EffectiveModel

#: Known-name prefix -> (``init_chat_model`` provider string, pinned partner package).
_LANGCHAIN_PROVIDERS: dict[str, tuple[str, str]] = {
    "openai": ("openai", "langchain-openai==1.6.3"),
    "anthropic": ("anthropic", "langchain-anthropic==1.7.2"),
    "deepseek": ("deepseek", "langchain-deepseek==1.1.1"),
    "bedrock": ("bedrock_converse", "langchain-aws==1.7.9"),
}

#: The structural (custom base URL) path always goes through ChatOpenAI.
_OPENAI_PACKAGE = "langchain-openai==1.6.3"

PLACEHOLDER_API_KEY = "unset-placeholder-key"


@dataclass(frozen=True)
class LangChainModelSource:
    """Pre-rendered source fragments for the generated ``context.py``.

    Attributes:
        helper_source: The ``default_model()`` function body, complete.
        dependency: The pinned partner package to add to ``pyproject.toml``.
        env_lines: ``.env.example`` lines naming the inherited route.
        readme_note: One sentence for the generated README.
        description: Human-readable route description for logs.
    """

    helper_source: str
    dependency: str
    env_lines: tuple[str, ...]
    readme_note: str
    description: str


def to_langchain_model_source(effective: EffectiveModel) -> LangChainModelSource:
    """Render the resolved route as LangChain model-construction source.

    Args:
        effective: The route this compile spends (already resolved).

    Returns:
        The fragments ``compile/langgraph/scaffold.py`` splices in.

    Raises:
        UnmappableRouteError: If the route has no LangChain counterpart
            among the providers this target pins.
    """
    emission = _resolve_emission(effective)

    if emission.kind == "structural":
        api_key_env = effective.api_key_env or "SWARM_API_KEY"
        helper_source = (
            f"DEFAULT_MODEL_ID: str = {effective.model!r}\n"
            f"DEFAULT_BASE_URL: str = {emission.base_url!r}\n"
            f"DEFAULT_API_KEY_ENV: str = {api_key_env!r}\n"
            "\n"
            "\n"
            "def default_model() -> BaseChatModel:\n"
            '    """The inherited route: an OpenAI-compatible endpoint via ChatOpenAI.\n'
            "\n"
            "    Provider packages are imported here, not at module level, so the\n"
            "    keyless import of the graph needs no provider package installed.\n"
            '    """\n'
            "    from langchain_openai import ChatOpenAI\n"
            "\n"
            '    api_key_env = os.environ.get("SWARM_API_KEY_ENV") or DEFAULT_API_KEY_ENV\n'
            "    return ChatOpenAI(\n"
            '        model=os.environ.get("SWARM_MODEL") or DEFAULT_MODEL_ID,\n'
            '        base_url=os.environ.get("SWARM_BASE_URL") or DEFAULT_BASE_URL,\n'
            f"        api_key=os.environ.get(api_key_env, {PLACEHOLDER_API_KEY!r}),\n"
            "    )\n"
        )
        return LangChainModelSource(
            helper_source=helper_source,
            dependency=_OPENAI_PACKAGE,
            env_lines=(
                f"SWARM_MODEL={effective.model}",
                f"SWARM_BASE_URL={emission.base_url}",
                f"SWARM_API_KEY_ENV={api_key_env}",
            ),
            readme_note=(
                f"Inherited default model: {effective.model} via custom endpoint "
                f"{emission.base_url} (source: {effective.source}), constructed with "
                "`langchain_openai.ChatOpenAI`; override with SWARM_MODEL/SWARM_BASE_URL/"
                "SWARM_API_KEY_ENV."
            ),
            description=f"{effective.model} @ {emission.base_url} (ChatOpenAI)",
        )

    mapping = _LANGCHAIN_PROVIDERS.get(emission.prefix or "")
    if mapping is None:
        raise UnmappableRouteError(
            effective.provider,
            effective.route.api if effective.route is not None else None,
            reason=(
                f"the LangGraph target has no LangChain provider mapping for the "
                f"{emission.prefix!r} prefix (supported: {', '.join(sorted(_LANGCHAIN_PROVIDERS))})"
            ),
        )
    provider, dependency = mapping
    helper_source = (
        f"DEFAULT_MODEL: str = {effective.model!r}\n"
        f"DEFAULT_MODEL_PROVIDER: str = {provider!r}\n"
        "\n"
        "\n"
        "def default_model() -> BaseChatModel:\n"
        '    """The inherited route, via LangChain\'s init_chat_model.\n'
        "\n"
        "    Imported here, not at module level, so the keyless import of the\n"
        "    graph needs no provider credentials and no provider package.\n"
        '    """\n'
        "    from langchain.chat_models import init_chat_model\n"
        "\n"
        "    return init_chat_model(\n"
        '        os.environ.get("SWARM_MODEL") or DEFAULT_MODEL,\n'
        '        model_provider=os.environ.get("SWARM_MODEL_PROVIDER") or DEFAULT_MODEL_PROVIDER,\n'
        "    )\n"
    )
    return LangChainModelSource(
        helper_source=helper_source,
        dependency=dependency,
        env_lines=(f"SWARM_MODEL={effective.model}", f"SWARM_MODEL_PROVIDER={provider}"),
        readme_note=(
            f"Inherited default model: {effective.model} (LangChain provider {provider!r}, "
            f"source: {effective.source}); override with SWARM_MODEL/SWARM_MODEL_PROVIDER."
        ),
        description=f"{provider}:{effective.model} (init_chat_model)",
    )


__all__ = ["PLACEHOLDER_API_KEY", "LangChainModelSource", "to_langchain_model_source"]
