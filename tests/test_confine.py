"""Unit tests for confine.py (PLAN.md fact 26, "Tests" -> Unit ->
confine.py): allows an in-project path; rejects `..` traversal, an
absolute path outside the root, and a symlink escaping the root."""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_builder.compile.confine import PathEscapesRootError, confine_path


def test_allows_an_in_project_relative_path(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    resolved = confine_path(root, "steps/step.py")

    assert resolved == (root / "steps" / "step.py").resolve()


def test_allows_an_absolute_path_already_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    absolute_inside = root / "steps" / "step.py"

    resolved = confine_path(root, absolute_inside)

    assert resolved == absolute_inside.resolve()


def test_rejects_dotdot_traversal(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with pytest.raises(PathEscapesRootError):
        confine_path(root, "../../etc/passwd")


def test_rejects_an_absolute_path_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside" / "secret.txt"
    outside.parent.mkdir()
    outside.write_text("secret")

    with pytest.raises(PathEscapesRootError):
        confine_path(root, outside)


def test_rejects_a_symlink_whose_target_escapes_the_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_target = outside_dir / "secret.txt"
    outside_target.write_text("secret")

    # A symlink physically INSIDE root, whose target resolves outside
    # it -- the case a lexical ".."-count check on the candidate string
    # alone cannot see (fact 26 / module docstring).
    escaping_symlink = root / "escape.txt"
    escaping_symlink.symlink_to(outside_target)

    with pytest.raises(PathEscapesRootError):
        confine_path(root, "escape.txt")


def test_error_names_the_offending_candidate(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with pytest.raises(PathEscapesRootError) as exc_info:
        confine_path(root, "../secret.txt")

    assert exc_info.value.candidate == "../secret.txt"
    assert exc_info.value.root == root.resolve()
