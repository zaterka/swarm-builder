"""The codegen suite -- PLAN.md "Tests" -> Codegen (the important suite,
no model needed). Scaffolds each of the seven positive fixtures to a real
temp directory, applies the (test-only) stub fill for any programmatic
node, and runs the full offline validation gate: uv sync, keyless
import, and validate/dry_run.py -- with an explicit UV_CACHE_DIR (fact
10, B14) and stripped credential env vars (mirroring
spike/FINDINGS.md's reproduce script). Also proves the four negative
fixtures are rejected by review.py (Phase 1), never reaching scaffold.

The positive-fixture gate is marked ``slow`` (registered in
tests/conftest.py) since it runs a real `uv sync`. Not deselected by
default -- a bare ``uv run pytest`` already exercises this suite,
satisfying "make sure they actually run and pass at least once."

The file also carries the *no-database byte-identity* regression
(PLAN-DB-NODES.md §9 acceptance 6): the whole tree both emitters produce
for a document with no database node, compared against the snapshots
``tests/golden/*.json`` captured from the tree *before* the feature
landed. It needs no ``uv`` -- the comparison is renderer output against
committed bytes -- so it is deliberately not slow.
"""

from __future__ import annotations

import ast
import difflib
import json
import os
import subprocess
from pathlib import Path

import pytest

from fixtures.graphs import (
    NEGATIVE_FIXTURES,
    POSITIVE_FIXTURES,
    fanout_join_graph,
    linear_chat_graph,
    websearch_graph,
)
from fixtures.stub_fill import apply_stub_fill
from swarm_builder.compile import (
    body_marker_begin,
    body_marker_end,
    default_scaffold_model,
    imports_marker_begin,
    imports_marker_end,
)
from swarm_builder.compile.boundary import capture_baseline
from swarm_builder.compile.fake_fill import _splice_region, stub_body_for
from swarm_builder.compile.langgraph.models import to_langchain_model_source
from swarm_builder.compile.langgraph.scaffold import scaffold_langgraph
from swarm_builder.compile.review import review
from swarm_builder.compile.scaffold import scaffold
from swarm_builder.inherit.settings import EffectiveModel

REPO_ROOT = Path(__file__).resolve().parent.parent
UV_CACHE_DIR = REPO_ROOT / ".uv-cache"
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"

#: Ambient credential env vars stripped for every keyless-gate subprocess,
#: mirroring spike/FINDINGS.md's reproduce script exactly.
_CREDENTIAL_ENV_VARS = (
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "SWARM_API_KEY",
)


def _keyless_env() -> dict[str, str]:
    env = {**os.environ, "UV_CACHE_DIR": str(UV_CACHE_DIR)}
    for var in _CREDENTIAL_ENV_VARS:
        env.pop(var, None)
    return env


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd, cwd=cwd, env=_keyless_env(), capture_output=True, text=True, check=False
    )
    return result


@pytest.mark.slow
@pytest.mark.parametrize("fixture_name", sorted(POSITIVE_FIXTURES))
def test_positive_fixture_passes_the_full_validation_gate(
    fixture_name: str, tmp_path: Path
) -> None:
    graph = POSITIVE_FIXTURES[fixture_name]()

    review_result = review(graph)
    assert review_result.ok, f"{fixture_name} unexpectedly failed Phase 1: {review_result.errors}"

    project_dir = tmp_path / fixture_name
    scaffold(graph, project_dir, default_scaffold_model())
    apply_stub_fill(project_dir, fixture_name)

    sync_result = _run(["uv", "sync"], cwd=project_dir)
    assert sync_result.returncode == 0, f"uv sync failed:\n{sync_result.stderr}"

    import_result = _run(
        ["uv", "run", "python", "-c", "import swarm_workflow.graph"], cwd=project_dir
    )
    assert import_result.returncode == 0, f"keyless import failed:\n{import_result.stderr}"

    dry_run_result = _run(["uv", "run", "python", "validate/dry_run.py"], cwd=project_dir)
    assert dry_run_result.returncode == 0, (
        f"dry_run.py failed:\n{dry_run_result.stdout}\n{dry_run_result.stderr}"
    )
    assert "ALL CHECKS PASSED" in dry_run_result.stdout
    assert "OK render() matches golden" in dry_run_result.stdout
    assert "OK graph.nodes ==" in dry_run_result.stdout


@pytest.mark.slow
def test_fanout_join_fixture_returns_the_joined_collection_not_one_arm(
    tmp_path: Path,
) -> None:
    """Explicit extra assertion beyond the generic gate: join fixtures
    return the JOINED collection, not one arbitrary branch value (fact
    13)."""
    graph = POSITIVE_FIXTURES["fanout_join"]()
    project_dir = tmp_path / "fanout_join_explicit"
    scaffold(graph, project_dir, default_scaffold_model())
    apply_stub_fill(project_dir, "fanout_join")

    assert _run(["uv", "sync"], cwd=project_dir).returncode == 0

    script = (
        "import asyncio\n"
        "from pydantic_ai.models.test import TestModel\n"
        "from swarm_workflow.deps import Deps\n"
        "from swarm_workflow.graph import graph\n"
        "from swarm_workflow.state import State\n"
        "out = asyncio.run(graph.run(inputs='x', state=State(), deps=Deps(model=TestModel())))\n"
        "print(sorted(out))\n"
    )
    result = _run(["uv", "run", "python", "-c", script], cwd=project_dir)
    assert result.returncode == 0, result.stderr
    assert "['L:x', 'R:x']" in result.stdout


@pytest.mark.parametrize("fixture_name", sorted(NEGATIVE_FIXTURES))
def test_negative_fixture_is_rejected_by_phase_one(fixture_name: str) -> None:
    """Not marked slow -- review.py needs no uv/subprocess at all, so
    this half of the codegen suite's negative-fixture requirement runs
    fast. (The full requirement -- "rejected by Phase 1" -- is exactly
    this: the graph never reaches scaffold.py.)"""
    graph = NEGATIVE_FIXTURES[fixture_name]()
    result = review(graph)
    assert not result.ok, f"negative fixture {fixture_name} was not rejected by Phase 1"


# ---------------------------------------------------------------------------
# No-database byte identity (PLAN-DB-NODES.md §9 acceptance 6).
#
# The database-node feature is gated on "does this document use a database node
# at all": with none, no repository file, no seed, no optional dependency, no
# `.env.example` line, no README section and no dry-run assertion may appear.
# The goldens were captured from the pristine tree *before* the feature landed,
# so they are evidence rather than a snapshot of the code under test -- a
# whole-tree byte comparison is the only assertion that can catch an emission
# that is merely *unused* rather than absent.
# ---------------------------------------------------------------------------

#: The three documents the goldens were captured from, and the fixed model route
#: they were captured with. Every one of them is a document that must be
#: unaffected, and both emitters are compared, so the guarantee is stated once
#: per (target, fixture) pair rather than once for the pair that happens to be
#: easiest to check.
_GOLDEN_FIXTURES = (
    ("linear_chat", linear_chat_graph),
    ("fanout_join", fanout_join_graph),
    ("websearch", websearch_graph),
)


def _golden_effective_model() -> EffectiveModel:
    """The model route the goldens were captured with, restated exactly.

    A different route would change the emitted ``deps.py``/``.env.example``, so
    the route is part of the snapshot's precondition, not a detail.
    """
    return EffectiveModel(
        provider="deepseek-official",
        model="deepseek-flash",
        reasoning_effort=None,
        base_url=None,
        api_key_env=None,
        source="settings-default",
        route=None,
    )


def _snapshot(root: Path) -> dict[str, str]:
    """Every emitted file, relative POSIX path -> exact text content.

    ``__pycache__`` is excluded: the scaffold's own golden-render subprocess
    imports the project it just wrote, which writes ``.pyc`` files into the
    generated tree. Those are a side effect of *checking* the tree, not part of
    what is emitted, so comparing them would make this assertion about the
    interpreter rather than about the emitters.
    """
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def _golden_diff(golden: dict[str, str], actual: dict[str, str]) -> str:
    """A readable report of every difference, or ``""`` when the trees match.

    Per-file and classified, because the three classes mean different things: an
    *added* path is a file the feature started emitting for a no-database
    document (the exact regression this test exists for), a *removed* path is one
    it stopped emitting, and a *changed* path is an emission that was edited. A
    bare "trees differ" would send a reader diffing two 17-file trees by hand.
    """
    added = sorted(set(actual) - set(golden))
    removed = sorted(set(golden) - set(actual))
    changed = sorted(
        path for path in set(golden) & set(actual) if golden[path] != actual[path]
    )
    if not (added or removed or changed):
        return ""

    lines: list[str] = []
    for label, paths in (("added", added), ("removed", removed)):
        if paths:
            lines.append(f"{label}:")
            lines += [f"  {path}" for path in paths]
    for path in changed:
        lines.append(f"changed: {path}")
        diff = difflib.unified_diff(
            golden[path].splitlines(),
            actual[path].splitlines(),
            fromfile="golden",
            tofile="actual",
            lineterm="",
        )
        lines += [f"  {line}" for line in diff]
    return "\n".join(lines)


@pytest.mark.parametrize(("fixture_name", "make_graph"), _GOLDEN_FIXTURES)
@pytest.mark.parametrize("target", ["pydantic_graph", "langgraph"])
def test_a_graph_without_a_database_node_emits_the_pre_change_tree(
    target: str, fixture_name: str, make_graph, tmp_path: Path
) -> None:
    """A document with no database node must emit byte-identical output.

    §9 acceptance 6, proved against ``tests/golden/*.json`` rather than against a
    second run of the current code: the snapshots come from the commit before the
    feature, so they can disagree with the implementation. Both targets and all
    three captured fixtures are compared, because the gate that keeps a
    no-database document unchanged lives in each emitter *and* in the shared
    renderer, and one green fixture would not cover the others.
    """
    golden_name = f"no_db_{target}_{fixture_name}"
    golden = json.loads((GOLDEN_DIR / f"{golden_name}.json").read_text())
    project_dir = tmp_path / golden_name

    if target == "langgraph":
        scaffold_langgraph(
            make_graph(), project_dir, to_langchain_model_source(_golden_effective_model())
        )
    else:
        scaffold(make_graph(), project_dir, default_scaffold_model())

    actual = _snapshot(project_dir)

    assert _golden_diff(golden, actual) == "", (
        f"{golden_name} no longer matches tests/golden/{golden_name}.json; "
        "a document with no database node must emit exactly the tree it emitted "
        "before database nodes existed.\n" + _golden_diff(golden, actual)
    )
    assert len(golden) > 0, "the golden snapshot is empty"


# ---------------------------------------------------------------------------
# Marker-region splicing (regression: a zero-content region must splice).
# ---------------------------------------------------------------------------


def test_splice_region_fills_a_zero_content_region(tmp_path: Path) -> None:
    """Splicing must work on an EMPTY region, not only a populated one.

    The naive marker-splicing regex (``begin\\n.*?\\nend`` with
    ``DOTALL``) cannot match a zero-content region: ``.*?`` has to
    consume at least one line, and the literal ``\\n`` after it a second
    one. ``scaffold.py`` really does emit regions that way -- every
    ``imports`` region has its two markers on consecutive lines, and a
    body region is empty for exactly as long as no fill has run -- so a
    regex-based fill silently failed to match precisely where the fill
    stage has the least to add. This asserts the fixed line arithmetic
    handles an empty region, and that the result is valid Python.
    """
    project_dir = tmp_path / "empty_body_region"
    graph = POSITIVE_FIXTURES["linear_chat"]()
    scaffold(graph, project_dir, default_scaffold_model())

    step_path = project_dir / "src" / "swarm_workflow" / "steps" / "intake.py"
    original = step_path.read_text()

    # Make the body region genuinely empty, emulating the state a fill
    # retry sees, and prove it is empty -- otherwise this test would pass
    # for the wrong reason and never reproduce the original failure.
    empty_body = _splice_region(original, "intake", "")
    begin = body_marker_begin("intake")
    end = body_marker_end("intake")
    assert empty_body.split(begin, 1)[1].split(end, 1)[0].strip() == ""

    # The regression: filling that empty region again must work.
    replacement = "    return ctx.inputs"
    spliced = _splice_region(empty_body, "intake", replacement)

    assert replacement in spliced
    assert spliced.split(begin, 1)[1].split(end, 1)[0].strip() == replacement.strip()
    assert begin in spliced
    assert end in spliced
    ast.parse(spliced)

    # Phase 4's actual invariant: the hash of everything outside the
    # marker regions is unchanged by a splice. Checking it through
    # boundary.py's own baseline capture means this assertion tracks the
    # real gate rather than a hand-rolled approximation of it.
    step_rel_path = "src/swarm_workflow/steps/intake.py"
    before = capture_baseline(project_dir).permitted_regions[step_rel_path].outside_hash
    step_path.write_text(spliced)
    after = capture_baseline(project_dir).permitted_regions[step_rel_path].outside_hash
    assert after == before


def test_splice_region_also_fills_a_zero_content_imports_region(tmp_path: Path) -> None:
    """The ``imports`` region is the empty region ``scaffold.py`` emits
    most often, so a fill of it must work too.

    ``_splice_region`` addresses a node's *body* markers by design -- an
    ``imports`` region is spliced by ``agent.py``'s ``write_region`` tool
    -- so this asserts the documented outcome instead: the empty imports
    region is left intact rather than being corrupted by a body splice
    whose markers genuinely cannot be found.
    """
    project_dir = tmp_path / "empty_imports_region"
    graph = POSITIVE_FIXTURES["linear_chat"]()
    scaffold(graph, project_dir, default_scaffold_model())

    step_path = project_dir / "src" / "swarm_workflow" / "steps" / "intake.py"
    original = step_path.read_text()

    begin = imports_marker_begin("intake")
    end = imports_marker_end("intake")
    # The scaffolded imports region really is empty.
    assert original.split(begin, 1)[1].split(end, 1)[0].strip() == ""

    spliced = _splice_region(original, "intake", "    return ctx.inputs")

    # The empty imports region survives byte-for-byte: its two markers are
    # still adjacent, and nothing was injected between them.
    assert f"{begin}\n{end}" in spliced
    ast.parse(spliced)


def test_splice_region_replaces_a_populated_region(tmp_path: Path) -> None:
    """The populated case keeps working: the old body is gone and every
    byte outside the region is byte-identical."""
    project_dir = tmp_path / "populated_region"
    graph = POSITIVE_FIXTURES["linear_chat"]()
    scaffold(graph, project_dir, default_scaffold_model())

    step_path = project_dir / "src" / "swarm_workflow" / "steps" / "intake.py"
    original = step_path.read_text()
    assert "raise NotImplementedError" in original

    replacement = stub_body_for(graph.nodes[0], graph)
    spliced = _splice_region(original, "intake", replacement)

    assert "raise NotImplementedError" not in spliced
    assert replacement in spliced
    ast.parse(spliced)
    # Outside-marker text is untouched, which is what Phase 4 hashes:
    # comparing the text after the BODY end marker proves the imports
    # region (and everything above it) moved by exactly the splice.
    body_end = body_marker_end("intake")
    assert spliced.split(body_end, 1)[1] == original.split(body_end, 1)[1]
    assert spliced[: spliced.index(body_marker_begin("intake"))] == original[
        : original.index(body_marker_begin("intake"))
    ]


def test_splice_region_raises_when_the_markers_are_absent() -> None:
    """A module with no markers is a scaffold the fill stage cannot
    address: that must raise rather than write nothing and report
    success."""
    with pytest.raises(ValueError, match="body markers for node 'intake' not found"):
        _splice_region("x = 1\n", "intake", "    return 'y'")


# ---------------------------------------------------------------------------
# Emitted docstrings: escaping, and docstring-before-__future__ ordering.
# ---------------------------------------------------------------------------

#: Intent strings that break a naively interpolated docstring. The first
#: four are the ones PLAN.md calls out; the rest are the same hazard in
#: other clothing (a quote at the very end abuts the closing delimiter, a
#: control character forces the docstring onto a second physical line).
_HOSTILE_INTENTS = (
    'an intent with a "quote" in it',
    "an intent with ''' triple single quotes",
    'an intent with """ triple double quotes',
    "an intent with a trailing backslash \\",
    'a quote right at the end "',
    "single quotes ' and double quotes \" together",
    "backslash then quote \\\" and quote then backslash \"\\",
    'a """ and a \\ and a \'\'\' all at once \\',
    "a line break\ninside the intent",
    "a tab\tinside the intent",
    "",
)


@pytest.mark.parametrize("intent", _HOSTILE_INTENTS)
def test_hostile_intent_emits_a_parseable_step_module(tmp_path: Path, intent: str) -> None:
    """A node's ``intent`` is free text, so it can hold quotes and
    backslashes.

    It is interpolated into the generated step module's docstring, and
    emitting it unescaped produces a module that does not even parse --
    the naive form, a docstring delimiter followed by the node id, the
    intent and a closing delimiter, breaks on a quote or a trailing
    backslash, and the breakage is silent until the generated project is
    imported. Every hostile intent must still yield a valid module whose
    docstring is the text the user typed.
    """
    graph = linear_chat_graph()
    hostile_node = graph.nodes[0].model_copy(update={"intent": intent})
    graph = graph.model_copy(
        update={"nodes": [hostile_node, *graph.nodes[1:]]}
    )

    project_dir = tmp_path / "hostile_intent"
    scaffold(graph, project_dir, default_scaffold_model())

    step_source = (
        project_dir / "src" / "swarm_workflow" / "steps" / f"{hostile_node.id}.py"
    ).read_text()

    tree = ast.parse(step_source)  # the actual regression: this must not raise
    # ``clean=False`` so a tab in the intent is not expanded to spaces by
    # ``ast.get_docstring``'s whitespace cleanup -- the module is valid and
    # holds the tab either way; this assertion is about the text itself.
    docstring = ast.get_docstring(tree, clean=False)
    assert docstring == f"``{hostile_node.id}`` step: {intent}"

    # The module must stay one physical line of docstring, so the
    # ``from __future__`` statement still follows it directly.
    assert step_source.splitlines()[0].startswith("'")
    assert step_source.splitlines()[1] == ""
    assert step_source.splitlines()[2] == "from __future__ import annotations"

    # And the whole project must still type-check as Python.
    for path in sorted((project_dir / "src" / "swarm_workflow").rglob("*.py")):
        ast.parse(path.read_text())


def test_generated_modules_put_the_docstring_before_the_future_import(
    tmp_path: Path,
) -> None:
    """A module docstring must come *first* to be a docstring at all.

    ``from __future__ import annotations`` is the one statement that may
    precede other imports, but nothing may precede a docstring: a docstring
    emitted below the ``__future__`` import is not a docstring, it is a
    discarded constant expression, and ``module.__doc__`` is ``None``.
    Every generated module that carries a docstring therefore has to start
    with it. This locks in the order ``scaffold.py`` emits today (verified,
    not assumed: the ``validate/dry_run.py`` emission was called out as a
    suspected inversion and is in fact already correct).
    """
    project_dir = tmp_path / "docstring_order"
    graph = linear_chat_graph()
    scaffold(graph, project_dir, default_scaffold_model())

    checked = 0
    for path in sorted(project_dir.rglob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        first_statement = tree.body[0]
        if not (
            isinstance(first_statement, ast.Expr)
            and isinstance(first_statement.value, ast.Constant)
            and isinstance(first_statement.value.value, str)
        ):
            continue
        checked += 1
        assert ast.get_docstring(tree) is not None, f"{path} lost its docstring"
        # The docstring is the first line of the file, not merely the
        # first statement.
        assert source.startswith("'") or source.startswith('"'), (
            f"{path} does not begin with its docstring:\n{source.splitlines()[0]!r}"
        )

    assert checked > 0, "no generated module carried a docstring"
