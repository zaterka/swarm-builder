"""Deterministic stub-fill helper for the Group-2 codegen test suite.

Group 2 has no model at all. Every ``agent``-kind node's step body and
factory are already fully rendered by ``scaffold.py`` (see
GROUP2_PLAN.md decision B6.5), but ``programmatic``-kind step bodies
default to ``raise NotImplementedError(...)`` -- the only genuine
placeholder Group 2 emits. This module supplies literal, type-correct
replacement bodies for every programmatic node across the six positive
fixtures, and a marker-aware splicing step to apply them to a scaffolded
project before running its validation gate. This is *not* part of
``scaffold.py`` -- it is test-only machinery standing in for what the
fill agent would do.

The splice itself is delegated to
``swarm_builder.compile.fake_fill._splice_region`` rather than
reimplemented here: that helper splices by line arithmetic, which is what
lets it fill a zero-content region (``scaffold.py`` emits every
``imports`` region with its two markers on consecutive lines). A
``DOTALL`` regex of the form ``begin\\n.*?\\nend`` cannot match such a
region, because it requires at least one line between the markers.

The helper is private to its module and reached deliberately: it is the
single already-correct implementation of the marker-region contract, and
importing it keeps this fixture from becoming a second, subtly divergent
copy of it.
"""

from __future__ import annotations

from pathlib import Path

from fixtures.graphs import POSITIVE_FIXTURES
from swarm_builder.compile.fake_fill import _splice_region, apply_fake_fill

#: node_id -> literal replacement body (module-indented at 4 spaces),
#: keyed per fixture name (matching tests/fixtures/graphs.py's
#: POSITIVE_FIXTURES keys) so two fixtures can reuse a node id with
#: different bodies without colliding.
STUB_BODIES: dict[str, dict[str, str]] = {
    "linear_chat": {
        "intake": (
            '    topic = ctx.inputs.strip()\n'
            '    ctx.state.topic = topic\n'
            '    return topic'
        ),
        "summarize": '    return f"SUMMARY: {ctx.inputs}"',
    },
    "websearch": {
        "format_result": '    return f"RESULT: {ctx.inputs}"',
    },
    "orchestrator": {},  # no programmatic nodes -- both nodes are agents
    "fanout_join": {
        "split": "    return ctx.inputs",
        "left": '    return f"L:{ctx.inputs}"',
        "right": '    return f"R:{ctx.inputs}"',
    },
    "decision_branching": {
        "classify": (
            '    bucket = "big" if len(ctx.inputs) > 3 else "small"\n'
            "    ctx.state.length_bucket = bucket\n"
            "    return bucket"
        ),
        "big": '    return f"BIG:{ctx.inputs}"',
        "small": '    return f"small:{ctx.inputs}"',
    },
    "mixed_programmatic": {
        "fetch": '    return f"DATA:{ctx.inputs}"',
    },
}


def apply_stub_fill(project_dir: Path, fixture_name: str) -> None:
    """Fill every stubbed node's body region in a scaffolded project.

    A fixture with a hand-written entry in :data:`STUB_BODIES` uses it, so
    the expressive bodies those fixtures assert on (``BIG:``/``DATA:``
    prefixes, the branch-matching classifier) stay exactly as written.
    Any other fixture falls back to the production stub filler, which
    derives a type-correct body from each node's declared ports and so
    works for a graph shape no table entry anticipated.

    Args:
        project_dir: Root of the project ``scaffold.py`` just emitted.
        fixture_name: Key into :data:`STUB_BODIES`.

    Raises:
        ValueError: If a stubbed node's step module or body markers are
            missing -- a scaffold the fill stage cannot address.
    """
    bodies = STUB_BODIES.get(fixture_name)
    if bodies is None:
        apply_fake_fill(project_dir, POSITIVE_FIXTURES[fixture_name]())
        return
    for node_id, replacement in bodies.items():
        step_path = project_dir / "src" / "swarm_workflow" / "steps" / f"{node_id}.py"
        step_path.write_text(_splice_region(step_path.read_text(), node_id, replacement))


__all__ = ["STUB_BODIES", "apply_stub_fill"]
