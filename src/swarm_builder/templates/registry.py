"""Template catalog plus keyword-based template inference.

Each template is a directory of literal files plus a manifest declaring
what it needs. This module is that manifest/catalog; the literal files
live in ``templates/<id>/agent.py.tmpl`` and are rendered by
``scaffold.py``.

**The inference rule is duplicated on the client, deliberately.**
:func:`infer_template` is the authoritative implementation, and the
frontend carries its own copy so the Inspector can preselect a template
without a round trip. The two must stay in exact parity, which is why the
scoring rule is documented on :func:`infer_template` rather than left to
be inferred from the code.
"""

from __future__ import annotations

from dataclasses import dataclass

from swarm_builder.models import TemplateId

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateEntry:
    """One template's catalog entry, as served by ``GET /api/templates``.

    ``label``, ``description``, ``default_tools`` and ``required_env`` are
    the picker-facing fields; ``deps`` and ``file_manifest`` are internal
    scaffolding concerns the picker does not see.
    """

    id: TemplateId
    label: str
    description: str
    default_tools: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    #: Extra plain PyPI dependency strings this template needs, unioned
    #: into the generated project's pyproject.toml by scaffold.py --
    #: never bracketed extras (those come from ResolvedModel.pyproject_extras).
    deps: tuple[str, ...] = ()
    #: Relative file paths under templates/<id>/ this template ships.
    file_manifest: tuple[str, ...] = ("agent.py.tmpl",)


TEMPLATE_CATALOG: dict[TemplateId, TemplateEntry] = {
    "chat": TemplateEntry(
        id="chat",
        label="Chat",
        description="A single conversational agent.",
        default_tools=(),
        required_env=(),
        deps=(),
    ),
    "orchestrator": TemplateEntry(
        id="orchestrator",
        label="Orchestrator",
        description=(
            "Delegates to child agents, wrapped as tool functions "
            "-- children are never graph steps."
        ),
        default_tools=(),
        required_env=(),
        deps=(),
    ),
    "websearch": TemplateEntry(
        id="websearch",
        label="Web search",
        description=(
            "A research step using WebSearchTool passed via toolsets=, "
            "never builtin_tools= or tools=."
        ),
        default_tools=("web_search",),
        required_env=(),
        deps=(),
    ),
    # 'rag' is deferred out of v1: its embedder and cache bring dependency
    # and sandbox friction no other template has. This catalog keeps the
    # seam -- a future entry here plus a templates/rag/ directory is all
    # that landing it needs.
}


def get_template(template_id: TemplateId) -> TemplateEntry:
    """Look up one template's catalog entry by id.

    Args:
        template_id: The template to look up. Typed as a ``TemplateId``
            literal, so an unknown id is a type error before it can be a
            ``KeyError`` at run time.

    Returns:
        The template's catalog entry.
    """
    return TEMPLATE_CATALOG[template_id]


# ---------------------------------------------------------------------------
# Template inference: a pure function, no I/O, no model call.
# ---------------------------------------------------------------------------

#: template -> its keywords, in priority order. Multi-word keywords
#: ("plan and assign") match as a plain substring, so no tokenization is
#: needed and a partial phrase never scores.
_KEYWORDS: dict[TemplateId, tuple[str, ...]] = {
    "websearch": ("search", "browse", "news", "latest"),
    "orchestrator": ("delegate", "coordinate", "route", "sub-agent", "plan and assign"),
}


@dataclass(frozen=True)
class InferenceResult:
    """A suggestion, never a binding.

    The Inspector preselects ``suggestion`` and shows the
    ``matched_keywords`` that produced it, so the suggestion is
    explainable; the user can always override it.
    """

    suggestion: TemplateId
    matched_keywords: tuple[str, ...]


def infer_template(intent: str) -> InferenceResult:
    """Suggest a template for one node's intent text.

    Scoring is count-based, not first-match-wins: the intent is lowercased
    and each template's keywords are counted as substrings, and the
    highest count wins. Ties -- including the 0-0 tie of an intent that
    matches nothing -- resolve to ``chat``, which is the correct default
    because every other template's behaviour is a strict superset of a
    plain conversation.

    Args:
        intent: The node's free-text intent.

    Returns:
        The suggested template and the keywords that matched it, so the
        Inspector can explain the suggestion.

    Note:
        The frontend carries a copy of this scoring rule so the Inspector
        can preselect without a round trip. Any change here must be
        mirrored there.
    """
    lowered = intent.lower()

    matches_by_template: dict[TemplateId, tuple[str, ...]] = {}
    for template_id, keywords in _KEYWORDS.items():
        matched = tuple(kw for kw in keywords if kw in lowered)
        matches_by_template[template_id] = matched

    best_template: TemplateId = "chat"
    best_count = 0
    for template_id, matched in matches_by_template.items():
        if len(matched) > best_count:
            best_count = len(matched)
            best_template = template_id

    if best_count == 0:
        return InferenceResult(suggestion="chat", matched_keywords=())
    return InferenceResult(
        suggestion=best_template, matched_keywords=matches_by_template[best_template]
    )


__all__ = [
    "TEMPLATE_CATALOG",
    "InferenceResult",
    "TemplateEntry",
    "get_template",
    "infer_template",
]
