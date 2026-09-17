"""Unit tests for slugification (PLAN.md "Edge cases", codegen contract
rule 11, "Tests" -> Unit -> Slugification)."""

from __future__ import annotations

import keyword

from swarm_builder.slugify import IDENTIFIER_RE, slugify, slugify_titles


def test_simple_title() -> None:
    assert slugify("Summarize results") == "summarize_results"


def test_duplicate_titles_get_numeric_suffix() -> None:
    slugs = slugify_titles(["Research", "Research", "Research"])
    assert slugs == ["research", "research_2", "research_3"]
    assert len(set(slugs)) == 3


def test_duplicate_titles_mapping_is_stable_and_order_dependent() -> None:
    titles = ["Fetch", "Fetch", "Summarize"]
    first = slugify_titles(titles)
    second = slugify_titles(list(titles))
    assert first == second == ["fetch", "fetch_2", "summarize"]


def test_dedup_does_not_silently_collide_with_a_later_organic_match() -> None:
    # "Fetch" then "Fetch" (-> fetch_2) then a literal "Fetch 2" title
    # must not collide with the dedup-generated "fetch_2".
    slugs = slugify_titles(["Fetch", "Fetch", "Fetch 2"])
    assert len(slugs) == len(set(slugs)) == 3
    assert slugs[0] == "fetch"
    assert slugs[1] == "fetch_2"
    assert slugs[2] not in {slugs[0], slugs[1]}


def test_non_ascii_title_falls_back_to_step_index() -> None:
    assert slugify("日本語のタイトル", index=3) == "step_3"


def test_punctuation_only_title_falls_back_to_step_index() -> None:
    assert slugify("!!!???", index=5) == "step_5"


def test_python_keyword_title_gets_trailing_underscore() -> None:
    slug = slugify("import")
    assert slug == "import_"
    assert not keyword.iskeyword(slug)


def test_leading_digit_title_gets_prefixed_underscore() -> None:
    slug = slugify("123 Go")
    assert slug == "_123_go"
    assert IDENTIFIER_RE.match(slug)


def test_every_produced_slug_is_a_valid_python_identifier() -> None:
    titles = [
        "Summarize results",
        "日本語",
        "!!!",
        "import",
        "class",
        "123 kickoff",
        "Summarize results",  # duplicate
        "",
    ]
    slugs = slugify_titles(titles)
    assert len(slugs) == len(titles)
    assert len(set(slugs)) == len(slugs)
    for slug in slugs:
        assert IDENTIFIER_RE.match(slug), slug
        assert not keyword.iskeyword(slug)


def test_empty_title_falls_back_to_step_index() -> None:
    assert slugify("", index=7) == "step_7"
