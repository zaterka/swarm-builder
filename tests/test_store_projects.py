"""Tests for :mod:`swarm_builder.store.projects`.

Every test uses ``tmp_path`` as the workspace root -- never the real
repository ``workspace/`` directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_builder.store._ids import InvalidGraphIdError
from swarm_builder.store.projects import (
    claim_langgraph_project_dir,
    claim_project_dir,
    clear_project_dir,
    delete_langgraph_project_for_graph,
    delete_project_dir,
    delete_project_for_graph,
    ensure_project_dir,
    langgraph_project_dir_for,
    project_dir,
    project_dir_for,
    project_dir_name,
    project_exists,
    rename_project_dirs,
)


class TestProjectDir:
    def test_project_dir_does_not_create_it(self, tmp_path: Path) -> None:
        path = project_dir(tmp_path, "g1")

        assert path == tmp_path / "projects" / "g1"
        assert not path.exists()


class TestProjectExists:
    def test_false_before_ensure_true_after(self, tmp_path: Path) -> None:
        assert project_exists(tmp_path, "g1") is False

        ensure_project_dir(tmp_path, "g1")

        assert project_exists(tmp_path, "g1") is True


class TestEnsureProjectDir:
    def test_idempotent(self, tmp_path: Path) -> None:
        first = ensure_project_dir(tmp_path, "g1")
        second = ensure_project_dir(tmp_path, "g1")

        assert first == second
        assert first.is_dir()


class TestClearProjectDir:
    def test_removes_contents_but_keeps_directory(self, tmp_path: Path) -> None:
        path = ensure_project_dir(tmp_path, "g1")
        marker = path / "marker.txt"
        marker.write_text("stale content from a previous compile", encoding="utf-8")

        clear_project_dir(tmp_path, "g1")

        assert path.is_dir()
        assert not marker.exists()
        assert list(path.iterdir()) == []

    def test_noop_when_never_created(self, tmp_path: Path) -> None:
        clear_project_dir(tmp_path, "g1")  # must not raise

        assert not project_dir(tmp_path, "g1").exists()


class TestDeleteProjectDir:
    def test_removes_directory_entirely(self, tmp_path: Path) -> None:
        path = ensure_project_dir(tmp_path, "g1")
        (path / "marker.txt").write_text("data", encoding="utf-8")

        delete_project_dir(tmp_path, "g1")

        assert not project_dir(tmp_path, "g1").exists()

    def test_second_delete_is_noop(self, tmp_path: Path) -> None:
        ensure_project_dir(tmp_path, "g1")
        delete_project_dir(tmp_path, "g1")

        delete_project_dir(tmp_path, "g1")  # must not raise

        assert not project_dir(tmp_path, "g1").exists()


class TestPathSafety:
    """Malicious ids must be rejected before any filesystem access."""

    @pytest.mark.parametrize("bad_id", ["../escape", "a/b"])
    def test_project_dir_rejects_malicious_id(self, tmp_path: Path, bad_id: str) -> None:
        with pytest.raises(InvalidGraphIdError):
            project_dir(tmp_path, bad_id)
        assert list(tmp_path.rglob("*")) == []

    @pytest.mark.parametrize("bad_id", ["../escape", "a/b"])
    def test_project_exists_rejects_malicious_id(self, tmp_path: Path, bad_id: str) -> None:
        with pytest.raises(InvalidGraphIdError):
            project_exists(tmp_path, bad_id)
        assert list(tmp_path.rglob("*")) == []

    @pytest.mark.parametrize("bad_id", ["../escape", "a/b"])
    def test_ensure_project_dir_rejects_malicious_id(self, tmp_path: Path, bad_id: str) -> None:
        with pytest.raises(InvalidGraphIdError):
            ensure_project_dir(tmp_path, bad_id)
        assert list(tmp_path.rglob("*")) == []

    @pytest.mark.parametrize("bad_id", ["../escape", "a/b"])
    def test_clear_project_dir_rejects_malicious_id(self, tmp_path: Path, bad_id: str) -> None:
        with pytest.raises(InvalidGraphIdError):
            clear_project_dir(tmp_path, bad_id)
        assert list(tmp_path.rglob("*")) == []

    @pytest.mark.parametrize("bad_id", ["../escape", "a/b"])
    def test_delete_project_dir_rejects_malicious_id(self, tmp_path: Path, bad_id: str) -> None:
        with pytest.raises(InvalidGraphIdError):
            delete_project_dir(tmp_path, bad_id)
        assert list(tmp_path.rglob("*")) == []


class TestProjectDirName:
    def test_slugifies_and_bounds_the_name(self) -> None:
        assert project_dir_name("Support Triage", "abc123") == "support-triage"
        assert project_dir_name("Straße Ωmega", "abc123") == "strae-mega"
        assert project_dir_name("全是中文", "abc123") == "project-abc123"

    def test_long_names_are_capped(self) -> None:
        slug = project_dir_name("x" * 200, "abc123")
        assert len(slug) <= 48
        assert slug.endswith("x")

    def test_result_always_matches_the_path_safety_charset(self) -> None:
        slug = project_dir_name("A/B\\C..D", "abc123")
        assert slug == "a-b-c-d"


class TestNameKeyedLifecycle:
    def test_claim_creates_slug_dir_and_marker(self, tmp_path: Path) -> None:
        path = claim_project_dir(tmp_path, "g1", "Support Triage")
        assert path == tmp_path / "projects" / "support-triage"
        assert (path / ".swarm-project.json").is_file()
        assert project_dir_for(tmp_path, "g1") == path

    def test_second_graph_with_the_same_name_gets_a_suffix(self, tmp_path: Path) -> None:
        claim_project_dir(tmp_path, "g1", "Triage")
        second = claim_project_dir(tmp_path, "g2", "Triage")
        assert second == tmp_path / "projects" / "triage-2"
        assert project_dir_for(tmp_path, "g1") != second
        assert project_dir_for(tmp_path, "g2") == second

    def test_reclaim_is_idempotent_for_the_same_graph(self, tmp_path: Path) -> None:
        first = claim_project_dir(tmp_path, "g1", "Triage")
        again = claim_project_dir(tmp_path, "g1", "Triage")
        assert again == first

    def test_rename_moves_the_marked_directory(self, tmp_path: Path) -> None:
        claim_project_dir(tmp_path, "g1", "Old name")
        rename_project_dirs(tmp_path, "g1", "New name")
        assert project_dir_for(tmp_path, "g1") == tmp_path / "projects" / "new-name"

    def test_delete_is_marker_verified_and_never_cross_graph(self, tmp_path: Path) -> None:
        claim_project_dir(tmp_path, "g1", "Triage")
        other = claim_project_dir(tmp_path, "g2", "Triage")

        # Deleting g1 must leave g2's suffixed directory untouched.
        delete_project_for_graph(tmp_path, "g1", "Triage")
        assert project_dir_for(tmp_path, "g1") is None
        assert project_dir_for(tmp_path, "g2") == other

    def test_legacy_id_keyed_directory_still_resolves(self, tmp_path: Path) -> None:
        ensure_project_dir(tmp_path, "g1")
        assert project_dir_for(tmp_path, "g1") == project_dir(tmp_path, "g1")


class TestLanggraphNameKeyedLifecycle:
    def test_claim_and_delete_round_trip(self, tmp_path: Path) -> None:
        lg = claim_langgraph_project_dir(tmp_path, "g1", "Triage")
        assert lg == tmp_path / "projects-langgraph" / "triage"
        assert langgraph_project_dir_for(tmp_path, "g1") == lg

        delete_langgraph_project_for_graph(tmp_path, "g1", "Triage")
        assert langgraph_project_dir_for(tmp_path, "g1") is None
