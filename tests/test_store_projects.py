"""Tests for :mod:`swarm_builder.store.projects`.

Every test uses ``tmp_path`` as the workspace root -- never the real
repository ``workspace/`` directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_builder.store._ids import InvalidGraphIdError
from swarm_builder.store.projects import (
    clear_project_dir,
    delete_project_dir,
    ensure_project_dir,
    project_dir,
    project_exists,
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
