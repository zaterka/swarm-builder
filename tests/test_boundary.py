"""Unit tests for boundary.py (PLAN.md "The compile pipeline" Phase 4,
"Tests" -> Boundary): rejects a mutated `graph.py`, an emptied marker
region, an edit outside the markers, and an unexpected new file; and
accepts a legitimate in-region body fill (the true-negative case
proving the check is not just always-fail).

Fixture projects are produced by the real `scaffold.py` against the
existing `linear_chat` codegen fixture (tests/fixtures/graphs.py) --
PLAN.md's suggested route for building the marker layout, and the one
that keeps this suite from drifting out of sync with scaffold.py's
actual output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fixtures.graphs import POSITIVE_FIXTURES
from swarm_builder.compile import (
    body_marker_begin,
    body_marker_end,
    default_scaffold_model,
)
from swarm_builder.compile.boundary import (
    BoundaryViolationError,
    capture_baseline,
    check_boundary,
)
from swarm_builder.compile.scaffold import scaffold

#: `linear_chat` has one agent node (`chat_step`, marker-bearing
#: steps/agents pair) and two programmatic nodes (`intake`, `summarize`,
#: marker-bearing steps only) -- enough surface for every violation kind
#: below without a slow `uv sync`.
_FIXTURE_NAME = "linear_chat"


def _scaffold_project(project_dir: Path) -> None:
    graph = POSITIVE_FIXTURES[_FIXTURE_NAME]()
    scaffold(graph, project_dir, default_scaffold_model())


def _step_path(project_dir: Path, node_id: str) -> Path:
    return project_dir / "src" / "swarm_workflow" / "steps" / f"{node_id}.py"


def test_clean_project_has_no_violations(tmp_path: Path) -> None:
    project_dir = tmp_path / "clean"
    _scaffold_project(project_dir)

    baseline = capture_baseline(project_dir)
    result = check_boundary(project_dir, baseline)

    assert result.ok
    assert result.violations == ()


def test_accepts_a_legitimate_in_region_body_fill(tmp_path: Path) -> None:
    """The true-negative case: a real fill-shaped edit -- replacing the
    body region's placeholder with type-correct code, and adding an
    import line inside the imports region -- must pass cleanly."""
    project_dir = tmp_path / "filled"
    _scaffold_project(project_dir)
    baseline = capture_baseline(project_dir)

    step_path = _step_path(project_dir, "intake")
    text = step_path.read_text()
    text = text.replace(
        f"{body_marker_begin('intake')}\n"
        '    raise NotImplementedError("swarm_builder: unfilled step body")\n'
        f"    {body_marker_end('intake')}",
        f"{body_marker_begin('intake')}\n"
        "    topic = ctx.inputs.strip()\n"
        "    ctx.state.topic = topic\n"
        "    return topic\n"
        f"    {body_marker_end('intake')}",
    )
    step_path.write_text(text)

    result = check_boundary(project_dir, baseline)

    assert result.ok


def test_rejects_a_mutated_graph_py(tmp_path: Path) -> None:
    project_dir = tmp_path / "mutated_graph"
    _scaffold_project(project_dir)
    baseline = capture_baseline(project_dir)

    graph_path = project_dir / "src" / "swarm_workflow" / "graph.py"
    graph_path.write_text(graph_path.read_text() + "\n# tampered\n")

    with pytest.raises(BoundaryViolationError) as exc_info:
        check_boundary(project_dir, baseline)

    violations = exc_info.value.result.violations
    assert any(
        v.code == "forbidden_file_changed" and v.path == "src/swarm_workflow/graph.py"
        for v in violations
    )


def test_rejects_an_emptied_marker_region(tmp_path: Path) -> None:
    project_dir = tmp_path / "emptied"
    _scaffold_project(project_dir)
    baseline = capture_baseline(project_dir)

    step_path = _step_path(project_dir, "intake")
    text = step_path.read_text()
    text = text.replace(
        f"{body_marker_begin('intake')}\n"
        '    raise NotImplementedError("swarm_builder: unfilled step body")\n'
        f"    {body_marker_end('intake')}",
        f"{body_marker_begin('intake')}\n" f"    {body_marker_end('intake')}",
    )
    step_path.write_text(text)

    with pytest.raises(BoundaryViolationError) as exc_info:
        check_boundary(project_dir, baseline)

    violations = exc_info.value.result.violations
    assert any(
        v.code == "empty_body_region" and v.path == "src/swarm_workflow/steps/intake.py"
        for v in violations
    )


def test_rejects_an_edit_outside_the_markers(tmp_path: Path) -> None:
    project_dir = tmp_path / "outside_edit"
    _scaffold_project(project_dir)
    baseline = capture_baseline(project_dir)

    step_path = _step_path(project_dir, "intake")
    text = step_path.read_text()
    # Mutate the module docstring -- well outside either marker region. The
    # replacement is driven off the emitted first line rather than a
    # hard-coded literal, so this keeps testing the boundary rule (not
    # scaffold.py's choice of docstring delimiters) if that ever changes.
    docstring_line = text.splitlines()[0]
    assert "Normalize the raw input topic string." in docstring_line
    text = text.replace(docstring_line, "# tampered outside the markers", 1)
    step_path.write_text(text)

    with pytest.raises(BoundaryViolationError) as exc_info:
        check_boundary(project_dir, baseline)

    violations = exc_info.value.result.violations
    assert any(
        v.code == "outside_marker_text_changed"
        and v.path == "src/swarm_workflow/steps/intake.py"
        for v in violations
    )


def test_rejects_an_unexpected_new_file(tmp_path: Path) -> None:
    project_dir = tmp_path / "new_file"
    _scaffold_project(project_dir)
    baseline = capture_baseline(project_dir)

    (project_dir / "src" / "swarm_workflow" / "steps" / "sneaky.py").write_text(
        "# not part of the scaffolded set\n"
    )

    with pytest.raises(BoundaryViolationError) as exc_info:
        check_boundary(project_dir, baseline)

    violations = exc_info.value.result.violations
    assert any(
        v.code == "unexpected_new_file"
        and v.path == "src/swarm_workflow/steps/sneaky.py"
        for v in violations
    )


def test_reports_all_violations_at_once_not_just_the_first(tmp_path: Path) -> None:
    project_dir = tmp_path / "multi"
    _scaffold_project(project_dir)
    baseline = capture_baseline(project_dir)

    graph_path = project_dir / "src" / "swarm_workflow" / "graph.py"
    graph_path.write_text(graph_path.read_text() + "\n# tampered\n")
    (project_dir / "src" / "swarm_workflow" / "steps" / "sneaky.py").write_text("# sneaky\n")

    with pytest.raises(BoundaryViolationError) as exc_info:
        check_boundary(project_dir, baseline)

    codes = {v.code for v in exc_info.value.result.violations}
    assert "forbidden_file_changed" in codes
    assert "unexpected_new_file" in codes


def test_capture_baseline_raises_on_a_missing_marker(tmp_path: Path) -> None:
    """A step file missing a marker at baseline time is a scaffolding
    bug, not a fill-time violation -- capture_baseline must refuse to
    build a baseline it cannot trust."""
    project_dir = tmp_path / "broken_scaffold"
    _scaffold_project(project_dir)

    step_path = _step_path(project_dir, "intake")
    text = step_path.read_text().replace(body_marker_end("intake"), "")
    step_path.write_text(text)

    from swarm_builder.compile.boundary import BoundaryScaffoldError

    with pytest.raises(BoundaryScaffoldError):
        capture_baseline(project_dir)


def test_check_boundary_reports_a_deleted_forbidden_file(tmp_path: Path) -> None:
    project_dir = tmp_path / "deleted"
    _scaffold_project(project_dir)
    baseline = capture_baseline(project_dir)

    (project_dir / "src" / "swarm_workflow" / "graph.py").unlink()

    with pytest.raises(BoundaryViolationError) as exc_info:
        check_boundary(project_dir, baseline)

    violations = exc_info.value.result.violations
    assert any(
        v.code == "missing_scaffolded_file" and v.path == "src/swarm_workflow/graph.py"
        for v in violations
    )
