"""Unit tests for templates/registry.py (PLAN.md "Tests" -> Unit ->
"Template inference: one case per template plus an ambiguous intent
defaulting to chat")."""

from __future__ import annotations

from swarm_builder.templates.registry import (
    TEMPLATE_CATALOG,
    get_template,
    infer_template,
)


def test_catalog_has_exactly_the_three_v1_templates() -> None:
    assert set(TEMPLATE_CATALOG) == {"chat", "orchestrator", "websearch"}


def test_catalog_entries_expose_required_fields() -> None:
    for template_id, entry in TEMPLATE_CATALOG.items():
        assert entry.id == template_id
        assert entry.label
        assert entry.description
        assert isinstance(entry.default_tools, tuple)
        assert isinstance(entry.required_env, tuple)
        assert isinstance(entry.deps, tuple)


def test_get_template_returns_the_catalog_entry() -> None:
    assert get_template("chat").id == "chat"


def test_infer_websearch_from_search_keyword() -> None:
    result = infer_template("Search the web for the latest news on this topic.")
    assert result.suggestion == "websearch"
    assert "search" in result.matched_keywords
    assert "latest" in result.matched_keywords
    assert "news" in result.matched_keywords


def test_infer_orchestrator_from_delegate_keyword() -> None:
    result = infer_template("Delegate to a sub-agent and coordinate the answer.")
    assert result.suggestion == "orchestrator"
    assert "delegate" in result.matched_keywords
    assert "coordinate" in result.matched_keywords
    assert "sub-agent" in result.matched_keywords


def test_infer_orchestrator_from_multiword_keyword() -> None:
    result = infer_template("Plan and assign work to the right specialist.")
    assert result.suggestion == "orchestrator"
    assert "plan and assign" in result.matched_keywords


def test_ambiguous_intent_defaults_to_chat() -> None:
    result = infer_template("Have a friendly conversation about cooking.")
    assert result.suggestion == "chat"
    assert result.matched_keywords == ()


def test_higher_keyword_count_wins_over_first_match() -> None:
    # "route" (orchestrator, 1 match) vs "search"+"latest" (websearch, 2
    # matches) -- scoring, not first-match-wins, must pick websearch.
    result = infer_template("Route this to search for the latest updates.")
    assert result.suggestion == "websearch"
