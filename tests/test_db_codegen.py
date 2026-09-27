"""Task G: the verification suite for the database-node feature (PLAN-DB-NODES.md §9).

Four claims live here because no other suite can own them, and each one is a claim about
what a *generated project* does -- not about what a renderer contains:

- **The live seam is a real switch, and it fails loudly.** One variable, one DSN and the
  engine's extra have to be all that stands between the unmodified document and a real
  database -- and when the driver is missing, the failure has to name it rather than serve
  example rows. The emitted ``validate/dry_run.py`` cannot evidence either half (it asserts
  mock mode and fails first with "expects the mock database (mock)"), so every live-mode
  case here runs the **emitted graph** with a live mode and a DSN and asserts on what the
  generated project itself does.
- **The keyless gate installs no live driver.** "The drivers are opt-in extras" is a claim
  about what a *plain* ``uv sync`` leaves in a project's environment, so it is measured
  there -- with a real ``uv sync``, and with the extras asserted to be declared, because
  otherwise "the import fails" would pass on a project that declares no extra at all.
- **``list[str]`` binding is reachable end to end.** §12b item 2 widened Phase 1's gate to
  match §4.3 after probing that the emitted path works. This file is the committed proof:
  review accepts the document, the emitted step calls the shared helper, the mock answers
  the expanded query, and a real ``uv sync`` plus the project's own dry run pass.
- **No database body is model-authored.** The regression is one edit away -- adding a
  database kind to ``FILLABLE_NODE_KINDS`` would hand a query, a seed or a repository file
  to a model -- so it is asserted against the constants *and* against the conversion-target
  filter they feed, not only against today's behaviour.

The fast tests run the emitted project on the **server's** interpreter with the project's
``src`` on ``PYTHONPATH``: the whole mock path is standard library, so a behaviour claim
does not have to buy an environment. The two claims that are about the environment itself
pay for it and are marked ``slow`` (registered in ``pyproject.toml``).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest

from fixtures.graphs import (
    database_agent_tool_graph,
    nosql_agent_tool_graph,
    nosql_query_graph,
    sql_lookup_graph,
    vector_search_graph,
)
from swarm_builder.compile import default_scaffold_model
from swarm_builder.compile.database import DATABASE_KINDS
from swarm_builder.compile.fake_fill import FILLABLE_NODE_KINDS
from swarm_builder.compile.graph_ir import analyze
from swarm_builder.compile.langgraph.convert import _convert_targets
from swarm_builder.compile.langgraph.scaffold import render_node_module
from swarm_builder.compile.review import review
from swarm_builder.compile.scaffold import scaffold
from swarm_builder.compile.validate import (
    CREDENTIAL_ENV_VARS_TO_STRIP,
    DATABASE_ENV_VARS_TO_STRIP,
)
from swarm_builder.models import (
    AgentSpec,
    NodeIo,
    Position,
    ProgrammaticSpec,
    SeqEdge,
    SqlSpec,
    SwarmGraph,
    SwarmNode,
)
from swarm_builder.templates.database import get_database_entry

REPO_ROOT = Path(__file__).resolve().parent.parent
UV_CACHE_DIR = REPO_ROOT / ".uv-cache"

UPDATED_AT = datetime(2024, 1, 1, tzinfo=UTC)

#: kind -> (an input that selects seeded rows, a value that appears *only* in that kind's
#: seeded rows). The marker is what tells a mock answer apart from an empty one -- and, in
#: the live-mode tests, what a silent fallback to the mock would have printed.
_MODE_PROBE: dict[str, tuple[str, str]] = {
    "sql": ("Acme Corp", "120.5"),
    "nosql": ("open", "Cannot sign in after the password reset"),
    "vector": ("aperture sensor", "Nimbus Router X1"),
}

#: kind -> the variable its live getter requires. One uniform scheme (``SWARM_<KIND>_DSN``),
#: restated here deliberately: these tests are about the *emitted project's* contract with
#: its user, and a variable the project reads has to be spelled out to be set.
_LIVE_DSN_VAR: dict[str, str] = {
    "sql": "SWARM_SQL_DSN",
    "nosql": "SWARM_NOSQL_DSN",
    "vector": "SWARM_VECTOR_DSN",
}

#: A guard so a test that claims to exercise every database kind cannot silently shrink to
#: the kinds a fixture happens to cover.
assert set(_MODE_PROBE) == {"sql", "nosql", "vector"}


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _keyless_env() -> dict[str, str]:
    """The environment every ``uv`` call here runs under: a writable cache, no credentials.

    The *database* variables are stripped for the same reason the credentials are:
    the emitted ``validate/dry_run.py`` asserts mock mode, so a developer who exported
    ``SWARM_DB_MODE=live`` -- the documented way to make the Run button read a real
    database -- must not be able to fail this suite's compile. Stripping
    ``validate``'s own tuples rather than a local list means a variable added there is
    stripped here automatically.
    """
    env = {**os.environ, "UV_CACHE_DIR": str(UV_CACHE_DIR)}
    for variable in (*CREDENTIAL_ENV_VARS_TO_STRIP, *DATABASE_ENV_VARS_TO_STRIP):
        env.pop(variable, None)
    return env


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one keyless ``uv`` command in ``cwd``."""
    return subprocess.run(
        cmd, cwd=cwd, env=_keyless_env(), capture_output=True, text=True, check=False
    )


def _run_in_project(
    project_dir: Path, script: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` against an emitted project, on the server's interpreter.

    ``PYTHONPATH`` points at the generated package and nothing else is inherited, so the
    only variables the probe sees are the ones a test passes -- which is what makes "live
    mode with a DSN and no driver" a hermetic statement. No ``uv sync``: the database mock
    is standard library and pydantic-graph is already installed for the server, so this
    stays a fast test.
    """
    return subprocess.run(
        [sys.executable, script],
        cwd=project_dir,
        env={
            "PYTHONPATH": str(project_dir / "src"),
            "PATH": "/usr/bin:/bin",
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _write_probe(tmp_path: Path, source: str) -> Path:
    """Write a probe script outside the generated project (it is not part of the export)."""
    probe = tmp_path / "probe.py"
    probe.write_text(source)
    return probe


#: Installs a stand-in driver, then runs the emitted graph in live mode. A real server is out
#: of scope for this suite (the plan refuses testcontainers), so this is the furthest a
#: hermetic test can go: the driver records what the *generated project* handed it.
_LIVE_ADAPTER_PROBE = '''
"""Install a stand-in psycopg, then run the emitted graph in live mode."""

import asyncio
import sys
import types

seen: list[str] = []


def connect(dsn: str) -> object:
    seen.append(dsn)
    raise RuntimeError(f"the stand-in driver was handed {dsn}")


psycopg = types.ModuleType("psycopg")
psycopg.connect = connect
sys.modules["psycopg"] = psycopg

from pydantic_ai.models.test import TestModel  # noqa: E402

from swarm_workflow.deps import Deps  # noqa: E402
from swarm_workflow.graph import graph  # noqa: E402
from swarm_workflow.state import State  # noqa: E402


async def main() -> None:
    try:
        await graph.run(inputs="Acme Corp", state=State(), deps=Deps(model=TestModel()))
    except RuntimeError as exc:
        print("LIVE PATH:", exc)


asyncio.run(main())
'''


#: Runs the emitted graph once and prints the mode the *generated* factory resolved, then
#: the rows the graph produced. Reading the mode from the project's own ``db_mode()`` is the
#: point: a test that reimplemented the lookup could disagree with the emitted seam.
_RUN_GRAPH_PROBE = '''
"""Run the emitted graph once, keylessly, and report its mode and its rows."""

import asyncio
import json
import os

from pydantic_ai.models.test import TestModel

from swarm_workflow.deps import Deps
from swarm_workflow.graph import graph
from swarm_workflow.repositories import db_mode
from swarm_workflow.state import State


async def main() -> None:
    print("MODE", db_mode())
    out = await graph.run(
        inputs=os.environ["SWARM_PROBE_INPUT"], state=State(), deps=Deps(model=TestModel())
    )
    print("ROWS", json.dumps(out, default=str))


asyncio.run(main())
'''


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------


def _single_database_node_graph(kind: str) -> SwarmGraph:
    """A review-clean document whose only node is one database node of ``kind``.

    One node that is both the entry and the exit, carrying its kind's starter spec and I/O
    verbatim: the emitted project is then complete exactly as ``scaffold()`` writes it (no
    programmatic step exists to stub), so the graph can be *run* with nothing filled in.
    That is what makes a mode probe a statement about the database seam alone rather than
    about a stub.
    """
    entry = get_database_entry(kind)
    node = SwarmNode(
        id=f"{kind}_db",
        kind=kind,  # type: ignore[arg-type]
        title=f"{kind} probe",
        intent=f"Read the seeded {kind} example for the probe input.",
        position=Position(x=0, y=0),
        io=entry.starter_io.model_copy(),
        **{kind: entry.starter_spec.model_copy()},
    )
    return SwarmGraph(
        id=f"{kind}-mode-probe",
        name=f"{kind} mode probe",
        entry_node_id=node.id,
        exit_node_id=node.id,
        state_fields=[],
        nodes=[node],
        edges=[],
        updated_at=UPDATED_AT,
    )


def _list_str_binding_graph() -> SwarmGraph:
    """One SQL node declaring ``list[str] -> list[json]``, exactly as §4.3 specifies it.

    The query has the single ``:input`` placeholder the rule allows, and it is an ``IN``
    list -- the shape the rule exists for, because neither SQLite nor psycopg can bind a
    list to one placeholder.
    """
    starter = get_database_entry("sql").starter_spec
    assert isinstance(starter, SqlSpec)
    node = SwarmNode(
        id="cities",
        kind="sql",
        title="Customers in cities",
        intent="Read the customers that live in any of the given cities.",
        position=Position(x=0, y=0),
        io=NodeIo(input_type="list[str]", output_type="list[json]"),
        sql=SqlSpec(
            query="SELECT id, name FROM customers WHERE city IN (:input) ORDER BY id",
            seed_sql=starter.seed_sql,
        ),
    )
    return SwarmGraph(
        id="list-str-binding",
        name="list[str] binding",
        entry_node_id=node.id,
        exit_node_id=node.id,
        state_fields=[],
        nodes=[node],
        edges=[],
        updated_at=UPDATED_AT,
    )


def _all_three_kinds_graph() -> SwarmGraph:
    """One document that uses every database kind, so one ``uv sync`` covers all three.

    The kinds are chained through programmatic bridges because a database node's ports are
    fixed at ``str -> list[json]``: two database nodes cannot be wired to each other
    directly, which is why every real document that reads two databases has something in
    between that turns rows back into the value the next step binds.
    """
    database_nodes = [
        _single_database_node_graph(kind).nodes[0] for kind in ("sql", "nosql", "vector")
    ]
    bridges = [
        SwarmNode(
            id=f"{kind}_to_text",
            kind="programmatic",
            title=f"Format {kind} rows",
            intent="Render the returned rows as one line.",
            position=Position(x=index + 0.5, y=0),
            io=NodeIo(input_type="list[json]", output_type="str"),
            programmatic=ProgrammaticSpec(needs=[], signature_hint="one line per row"),
        )
        for index, kind in enumerate(("sql", "nosql"))
    ]
    nodes = [
        database_nodes[0],
        bridges[0],
        database_nodes[1],
        bridges[1],
        database_nodes[2],
    ]
    edges = [
        SeqEdge(kind="seq", id=f"e{index}", source=source.id, target=target.id)
        for index, (source, target) in enumerate(pairwise(nodes), start=1)
    ]
    return SwarmGraph(
        id="all-three-kinds",
        name="Every database kind",
        entry_node_id=nodes[0].id,
        exit_node_id=nodes[-1].id,
        state_fields=[],
        nodes=nodes,
        edges=edges,
        updated_at=UPDATED_AT,
    )


def _driver_name(kind: str) -> str:
    """One kind's live driver, read off the catalog's extra requirement.

    Derived rather than restated so a renamed driver cannot leave this file asserting a
    name the generated project no longer mentions: ``live_extra_deps`` is the one place the
    driver's name is declared for the emitted project.
    """
    requirement = get_database_entry(kind).live_extra_deps[0]
    return re.split(r"[\[=><]", requirement, maxsplit=1)[0]


# ---------------------------------------------------------------------------
# The live seam: live mode fails loudly and never serves the mock (§6, §9.7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(_MODE_PROBE))
def test_live_mode_without_the_driver_names_the_driver_and_the_extra(
    kind: str, tmp_path: Path
) -> None:
    """A missing live driver must be a *named* failure, not a silent one.

    The user who flips ``SWARM_DB_MODE=live`` has edited no graph: the only thing between
    them and a working read is one package they have not installed. So the error has to say
    which driver and which command -- and it has to come from the generated project, which
    is why this runs the emitted graph rather than the rendered module.
    """
    project_dir = _scaffold(_single_database_node_graph(kind), tmp_path / kind)
    probe = _write_probe(tmp_path, _RUN_GRAPH_PROBE)

    result = _run_in_project(
        project_dir,
        str(probe),
        {
            "SWARM_PROBE_INPUT": _MODE_PROBE[kind][0],
            "SWARM_DB_MODE": "live",
            _LIVE_DSN_VAR[kind]: "postgresql://example/swarm",
        },
    )

    assert result.returncode != 0, result.stdout
    assert _driver_name(kind) in result.stderr
    assert f"uv sync --extra {get_database_entry(kind).live_extra_name}" in result.stderr


@pytest.mark.parametrize("kind", sorted(_MODE_PROBE))
def test_live_mode_without_the_driver_never_serves_the_mock_rows(kind: str, tmp_path: Path) -> None:
    """The inverse of the message test, and the reason it matters.

    A fallback to the mock is indistinguishable from success: the graph would exit 0 and
    print example rows while the user believes they are reading their own database. The
    marker below is a value that exists *only* in the seed, so finding it here would mean
    the project served example data in live mode.
    """
    project_dir = _scaffold(_single_database_node_graph(kind), tmp_path / kind)
    probe = _write_probe(tmp_path, _RUN_GRAPH_PROBE)

    result = _run_in_project(
        project_dir,
        str(probe),
        {
            "SWARM_PROBE_INPUT": _MODE_PROBE[kind][0],
            "SWARM_DB_MODE": "live",
            _LIVE_DSN_VAR[kind]: "postgresql://example/swarm",
        },
    )

    _input, seeded_marker = _MODE_PROBE[kind]
    assert result.returncode != 0
    assert seeded_marker not in result.stdout
    assert "ROWS" not in result.stdout


@pytest.mark.parametrize("kind", sorted(_MODE_PROBE))
def test_an_unset_mode_runs_the_seeded_mock_through_the_emitted_graph(
    kind: str, tmp_path: Path
) -> None:
    """With no ``SWARM_DB_MODE`` at all the project reports mock *and* answers with rows.

    Both halves are needed: "the mode is mock" is a string from the emitted factory, while
    the rows are what prove the mock actually read this node's seed file. A project whose
    factory said "mock" and then returned nothing would satisfy the first alone.
    """
    probe_input, seeded_marker = _MODE_PROBE[kind]
    project_dir = _scaffold(_single_database_node_graph(kind), tmp_path / kind)
    probe = _write_probe(tmp_path, _RUN_GRAPH_PROBE)

    result = _run_in_project(project_dir, str(probe), {"SWARM_PROBE_INPUT": probe_input})

    assert result.returncode == 0, result.stderr
    assert "MODE mock" in result.stdout
    assert seeded_marker in result.stdout


@pytest.mark.parametrize("kind", sorted(_MODE_PROBE))
def test_live_mode_without_a_dsn_names_the_variable_it_needs(kind: str, tmp_path: Path) -> None:
    """The DSN cannot be guessed, so live mode without one has to say which is missing.

    A defaulted connection string would point somewhere plausible and wrong, and the user
    would read whatever database that is. The failure therefore has to name the variable --
    and, like the missing-driver case, it is asserted against the emitted graph because the
    emitted dry run asserts mock mode and never reaches this path.
    """
    project_dir = _scaffold(_single_database_node_graph(kind), tmp_path / kind)
    probe = _write_probe(tmp_path, _RUN_GRAPH_PROBE)

    result = _run_in_project(
        project_dir,
        str(probe),
        {"SWARM_PROBE_INPUT": _MODE_PROBE[kind][0], "SWARM_DB_MODE": "live"},
    )

    assert result.returncode != 0
    assert _LIVE_DSN_VAR[kind] in result.stderr
    # ...and the failure is not a fallback: the seed's own rows never appear.
    assert _MODE_PROBE[kind][1] not in result.stdout


def test_an_unrecognized_mode_value_names_the_accepted_set(tmp_path: Path) -> None:
    """``SWARM_DB_MODE=life`` must stop, not quietly run the mock.

    Every other unknown value is a user's typo, and the mock is exactly the failure mode
    that is hardest to notice: the canvas would show example rows while the user believes
    they are reading their own database. The accepted set is therefore part of the message.
    """
    project_dir = _scaffold(_single_database_node_graph("sql"), tmp_path / "typo")
    probe = _write_probe(tmp_path, _RUN_GRAPH_PROBE)

    result = _run_in_project(
        project_dir,
        str(probe),
        {"SWARM_PROBE_INPUT": _MODE_PROBE["sql"][0], "SWARM_DB_MODE": "life"},
    )

    assert result.returncode != 0
    assert "'life' is not a supported mode" in result.stderr
    assert "'mock' or 'live'" in result.stderr
    assert _MODE_PROBE["sql"][1] not in result.stdout


def test_the_keyless_gate_strips_every_variable_the_emitted_project_reads() -> None:
    """The compile gate's own strip list has to cover the project's live variables.

    §12b item 1: the emitted dry run asserts mock mode, so a developer who exported
    ``SWARM_DB_MODE=live`` for the Run button would otherwise be unable to recompile. That
    fix is only durable if the strip list keeps up with the generated project's variables,
    so the set is derived from the catalog here rather than trusted.
    """
    project_reads = {
        "SWARM_DB_MODE",
        *(
            variable
            for kind in DATABASE_KINDS
            for variable in get_database_entry(kind).env_vars
        ),
    }

    # The three engines' DSNs, so this cannot pass by the catalog turning up empty.
    assert {"SWARM_SQL_DSN", "SWARM_NOSQL_DSN", "SWARM_VECTOR_DSN"} <= project_reads
    assert project_reads <= set(DATABASE_ENV_VARS_TO_STRIP)


def test_going_live_needs_the_driver_and_the_dsn_and_no_graph_edit(tmp_path: Path) -> None:
    """The "one variable, one DSN, one extra" claim, with the driver present and no server.

    §9 criterion 7 asks whether flipping the mode makes *the same graph* use a real database
    with no edit. A real server is deliberately out of scope for this suite (§10 refuses
    testcontainers), so what a hermetic test can show is the whole path up to the driver: the
    unmodified document, the unmodified step, and the DSN from the environment arriving at
    the adapter that the extra installs. The mock is never constructed on the way -- which is
    the property that makes live mode live, and the one a fallback would break.
    """
    project_dir = _scaffold(_single_database_node_graph("sql"), tmp_path / "live_adapter")
    probe = _write_probe(tmp_path, _LIVE_ADAPTER_PROBE)

    result = _run_in_project(
        project_dir,
        str(probe),
        {"SWARM_DB_MODE": "live", "SWARM_SQL_DSN": "postgresql://example/measured"},
    )

    assert (
        "LIVE PATH: the stand-in driver was handed postgresql://example/measured"
        in result.stdout
    )


# ---------------------------------------------------------------------------
# No model writes a database body (§1.3, §4.5, §9 regression)
# ---------------------------------------------------------------------------


def test_no_database_kind_is_a_model_fillable_body() -> None:
    """A database node's body is emitted deterministically; nothing may ever fill it.

    ``FILLABLE_NODE_KINDS`` is what the compile agent's tools range over. Adding a database
    kind to it would let a model author a query, a seed or a repository file -- the one
    boundary this feature draws -- and the change would be a single word in a frozenset, so
    it is pinned here next to the kinds it must never contain.
    """
    assert DATABASE_KINDS == {"sql", "nosql", "vector"}, "the kinds under test are not all here"
    assert DATABASE_KINDS & FILLABLE_NODE_KINDS == frozenset()


def test_no_database_node_is_a_langgraph_conversion_target() -> None:
    """The LangGraph half of the boundary, asserted where the filter is used.

    ``_convert_targets`` decides which steps the conversion agent is asked to rewrite. A
    database node in that list would be a free invitation to re-author the operation the
    document declares, so each kind is checked through the real filter on a real document
    -- the constants alone would not catch a filter that stopped reading them.
    """
    for make_graph, node_id in (
        (sql_lookup_graph, "orders_db"),
        (nosql_query_graph, "tickets"),
        (vector_search_graph, "product_docs"),
        (database_agent_tool_graph, "product_docs"),
    ):
        graph = make_graph()
        database_node = next(node for node in graph.nodes if node.id == node_id)
        assert database_node.kind not in FILLABLE_NODE_KINDS
        assert node_id not in {node.id for node in _convert_targets(graph)}


# ---------------------------------------------------------------------------
# `list[str]` binding, the reachability the adjudication restored (§4.3, §12b.2)
# ---------------------------------------------------------------------------


def test_review_accepts_a_list_str_database_input() -> None:
    """Phase 1's gate and §4.3's binding rule have to agree, or the rule is dead code.

    §12b item 2: the emitters implemented §4.3 while review enforced the narrower §4.5, so
    ``expand_list_param`` and its emitter branches were unreachable. This is the committed
    assertion that the widened gate admits the document -- one that also has to be clean of
    the *other* database codes, since a rejection there would be an equally silent block.
    """
    result = review(_list_str_binding_graph())

    codes = {finding.code for finding in result.errors}
    assert result.ok, result.errors
    assert not codes & {
        "db_input_type_unsupported",
        "db_op_io_mismatch",
        "db_placeholder_mismatch",
    }


def test_a_list_str_database_step_expands_the_placeholder_with_the_shared_helper(
    tmp_path: Path,
) -> None:
    """The document's single ``:input`` has to reach the repository as one placeholder per
    value, rewritten by the module both targets share.

    A test that asserted only "no error" would pass on a step that bound the list to one
    placeholder -- which no driver accepts -- so the emitted *source* is what is pinned.
    """
    project_dir = _scaffold(_list_str_binding_graph(), tmp_path / "list_str")
    source = (project_dir / "src" / "swarm_workflow" / "steps" / "cities.py").read_text()

    assert "repo.query(*expand_list_param(SQL_QUERY, ctx.inputs))" in source
    assert (
        "from swarm_workflow.repositories.portshape import as_port, expand_list_param" in source
    )


def test_a_list_str_database_mock_returns_a_row_per_matching_city(tmp_path: Path) -> None:
    """The expanded query really runs: two cities in, the two seeded customers out.

    This is the measurement the adjudication rests on, moved from a scratch probe into the
    suite. The seed has one customer per city, so a list bound as a *single* value would
    match nothing (`[]`) and an unexpanded list would be a driver error -- the row count is
    what distinguishes a working expansion from either.
    """
    project_dir = _scaffold(_list_str_binding_graph(), tmp_path / "list_str_rows")
    probe = _write_probe(
        tmp_path,
        '''
"""Read the seeded customers for two cities through the emitted step."""

import asyncio
import json

from pydantic_ai.models.test import TestModel

from swarm_workflow.deps import Deps
from swarm_workflow.graph import graph
from swarm_workflow.state import State


async def main() -> None:
    out = await graph.run(
        inputs=["Berlin", "Lisbon"], state=State(), deps=Deps(model=TestModel())
    )
    print("ROWS", json.dumps(out))


asyncio.run(main())
''',
    )

    result = _run_in_project(project_dir, str(probe), {})

    assert result.returncode == 0, result.stderr
    assert 'ROWS [{"id": 1, "name": "Acme Corp"}, {"id": 2, "name": "Globex"}]' in result.stdout


@pytest.mark.slow
def test_a_list_str_database_project_passes_the_full_keyless_gate(tmp_path: Path) -> None:
    """The same document through the gate every other positive fixture runs.

    §12b item 2 decided the question by measurement -- "the generated project's own
    ``validate/dry_run.py`` passes" -- and a source assertion cannot evidence that. A real
    ``uv sync`` and the emitted dry run are what make the ``list[str]`` rule supported
    rather than merely accepted.
    """
    graph = _list_str_binding_graph()
    project_dir = tmp_path / "list_str_gate"
    scaffold(graph, project_dir, default_scaffold_model())

    sync_result = _run(["uv", "sync"], cwd=project_dir)
    assert sync_result.returncode == 0, sync_result.stderr

    import_result = _run(
        ["uv", "run", "python", "-c", "import swarm_workflow.graph"], cwd=project_dir
    )
    assert import_result.returncode == 0, import_result.stderr

    dry_run = _run(["uv", "run", "python", "validate/dry_run.py"], cwd=project_dir)
    assert dry_run.returncode == 0, f"{dry_run.stdout}\n{dry_run.stderr}"
    assert "ALL CHECKS PASSED" in dry_run.stdout


# ---------------------------------------------------------------------------
# The keyless gate installs no live driver (§9 slow, §10's re-measurement)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_plain_uv_sync_installs_no_live_driver(tmp_path: Path) -> None:
    """The extras are declared and *not* installed, which is the whole opt-in claim.

    §10 records that the shipped shape is three separate extras while the planning probe
    measured one combined extra, so this re-measures with the shipped names: after the same
    plain ``uv sync`` the keyless gate runs, all three drivers are absent from the project's
    own environment. The extras are asserted to be present in ``pyproject.toml`` first --
    otherwise "the import fails" would also pass on a project that declares no extra.
    """
    project_dir = _scaffold(_all_three_kinds_graph(), tmp_path / "all_kinds")
    pyproject = (project_dir / "pyproject.toml").read_text()
    for kind in sorted(DATABASE_KINDS):
        assert get_database_entry(kind).live_extra_name in pyproject

    sync_result = _run(["uv", "sync"], cwd=project_dir)
    assert sync_result.returncode == 0, sync_result.stderr

    for module in ("psycopg", "pymongo", "qdrant_client"):
        result = _run(["uv", "run", "python", "-c", f"import {module}"], cwd=project_dir)
        assert result.returncode != 0, f"a plain uv sync installed {module}"
        assert f"No module named '{module}'" in result.stderr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scaffold(graph: SwarmGraph, project_dir: Path) -> Path:
    """Scaffold ``graph`` into ``project_dir`` and assert it was review-clean first.

    A fixture that Phase 1 would reject would make every assertion below a statement about
    a project no user could produce, so the gate is asserted here rather than assumed.
    """
    review_result = review(graph)
    assert review_result.ok, f"the probe document is not compilable: {review_result.errors}"
    scaffold(graph, project_dir, default_scaffold_model())
    return project_dir


# ---------------------------------------------------------------------------
# Cross-target parity of the model-facing tool text
# ---------------------------------------------------------------------------


def _pydantic_agent_source(graph: SwarmGraph, agent_node: SwarmNode) -> str:
    """The PydanticAI target's rendered ``agents/<id>.py`` for ``agent_node``."""
    from swarm_builder.compile.scaffold import _render_agent_factory

    return _render_agent_factory(graph, agent_node)


def _agent_tool_graph(kind: str) -> SwarmGraph:
    """A graph of ``kind`` whose agent node attaches that database node as a tool."""
    if kind == "nosql":
        return nosql_agent_tool_graph()
    if kind == "vector":
        return database_agent_tool_graph()
    # The SQL fixtures have no agent-tool twin; attach one the same way the others do.
    base = sql_lookup_graph()
    database_node = next(node for node in base.nodes if node.kind == "sql")
    agent = SwarmNode(
        id="reader",
        kind="agent",
        title="Reader",
        intent="Read what the orders lookup returned.",
        position=database_node.position,
        template="chat",
        io=NodeIo(input_type="list[json]", output_type="str"),
        agent=AgentSpec(instructions="Read it.", tools=[f"sql:{database_node.id}"]),
    )
    return base.model_copy(update={"nodes": [*base.nodes, agent]})


#: kind -> the parameter name that kind's tool must use. Never `query`: a real-model
#: run passed a *SQL statement* to a parameter of that name and the tool bound it as
#: the declared `:input` value, matching nothing and returning `[]` -- a silent wrong
#: answer. `query_text` is the vector kind's deliberate exception: there the argument
#: genuinely is the text to search for, and no statement exists to confuse it with.
_TOOL_PARAMETER: dict[str, str] = {
    "sql": "input_value",
    "nosql": "input_value",
    "vector": "query_text",
}

#: kind -> text that can only come from quoting that kind's declared operation in the
#: docstring: the SQL statement, the filter with its sentinel, the collection name.
_DECLARATION_MARKER: dict[str, str] = {
    "sql": "SELECT",
    "nosql": "$input",
    "vector": "product_docs",
}


@pytest.mark.parametrize("kind", sorted(_TOOL_PARAMETER))
def test_an_agent_tool_is_named_for_its_value_in_whichever_target_renders_it(
    kind: str,
) -> None:
    """Both targets must describe a database tool the same way.

    The two targets once had their own copies of the sentinel *binding* and of the
    seed-suffix table, and they disagreed -- one silently read nothing. The tool's
    parameter name and docstring are the text a model acts on, so this asserts them
    on both targets and all three kinds rather than on whichever one a fixture
    happened to cover: the emission is shared (`database_tool_parameter`), and this
    is what keeps it shared.
    """
    graph = _agent_tool_graph(kind)
    parameter = _TOOL_PARAMETER[kind]
    database_node = next(node for node in graph.nodes if node.kind == kind)
    agent_node = next(node for node in graph.nodes if node.kind == "agent")

    pydantic_source = _pydantic_agent_source(graph, agent_node)
    langgraph_source = render_node_module(graph, analyze(graph), agent_node)

    for label, source in (("pydantic", pydantic_source), ("langgraph", langgraph_source)):
        assert f"({parameter}:" in source, (label, kind)
        assert "(query:" not in source, (label, kind)
        if kind != "vector":
            # A SQL/NoSQL tool must say outright that its argument is a value and the
            # declaration is fixed. A vector tool needs no such clause: its argument
            # genuinely is free search text, and naming it `query_text` says so.
            assert "never a" in source, (label, kind)
        assert database_node.id in source, (label, kind)
    # The declared operation is quoted, so the model can see what its argument binds
    # to -- the piece of context whose absence caused the wrong answer above.
    assert _DECLARATION_MARKER[kind] in pydantic_source, kind


# ---------------------------------------------------------------------------
# The write path (the plan's second promise, previously unexercised end to end)
# ---------------------------------------------------------------------------


def _write_graph() -> SwarmGraph:
    """One SQL node that inserts a row, taking the name to insert from its input.

    Deliberately a *single* node: a database node's output is always `list[json]`
    and `list[json]` is never a valid database *input*, so two database nodes cannot
    be wired directly -- `port_type_mismatch`. Chaining would need a step between
    them to reshape the rows, which is the documented (and tested) consequence of the
    mandatory output type rather than a gap.
    """
    seed_sql = get_database_entry("sql").starter_spec.seed_sql
    return SwarmGraph(
        id="write-graph",
        name="Write a row",
        entry_node_id="add_customer",
        exit_node_id="add_customer",
        state_fields=[],
        nodes=[
            SwarmNode(
                id="add_customer",
                kind="sql",
                title="Add a customer",
                intent="Insert one customer.",
                position=Position(x=0, y=0),
                io=NodeIo(input_type="str", output_type="list[json]"),
                sql=SqlSpec(
                    query="INSERT INTO customers (id, name, city) VALUES (99, :input, 'Oslo')",
                    seed_sql=seed_sql,
                    write=True,
                ),
            )
        ],
        edges=[],
        updated_at=UPDATED_AT,
    )


def test_a_write_declared_step_reaches_the_mock_and_shares_one_instance(
    tmp_path: Path,
) -> None:
    """The write path, end to end and keylessly.

    `sql.write: true` emits `repo.execute(...)` and returns `[{"rows_affected": N}]`.
    Source assertions cannot prove the statement reaches the mock, so this runs the
    emitted project on the server's own interpreter (no `uv sync`, no database) and
    then re-opens the repository in the same process: the inserted row is there,
    which also pins the property the instance cache exists for -- one process, one
    instance per seed, so a write is visible to whatever fetches the instance next.
    """
    graph = _write_graph()
    result = review(graph)
    assert result.ok, [finding.code for finding in result.errors]

    project = tmp_path / "write_graph"
    scaffold(graph, project, default_scaffold_model())

    probe = tmp_path / "probe_write.py"
    probe.write_text(
        "import asyncio, json\n"
        "from pydantic_ai.models.test import TestModel\n"
        "from swarm_workflow.deps import Deps\n"
        "from swarm_workflow.graph import graph\n"
        "from swarm_workflow.repositories import get_sql_repository\n"
        "from swarm_workflow.state import State\n"
        "from swarm_workflow.steps.add_customer import SEED_PATH\n"
        "out = asyncio.run(graph.run(inputs='Umbrella', state=State(),\n"
        "                            deps=Deps(model=TestModel())))\n"
        "print(json.dumps(out))\n"
        "rows = get_sql_repository(SEED_PATH).query(\n"
        "    'SELECT name FROM customers WHERE id = 99')\n"
        "print(json.dumps(rows))\n"
    )
    completed = subprocess.run(
        [sys.executable, str(probe)],
        cwd=project,
        env={"PYTHONPATH": str(project / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    written, read_back = completed.stdout.strip().splitlines()
    assert written == '[{"rows_affected": 1}]'
    assert read_back == '[{"name": "Umbrella"}]'


def test_a_write_declared_step_is_never_offered_as_an_agent_tool() -> None:
    """A write node is refused as a tool, so a model can never reach the write path.

    `db_write_as_tool` is the gate; this asserts the *emitter* also keeps the write
    unreachable, by checking that no `execute` call is emitted into any agent module
    for a graph whose database node declares a write.
    """
    base = sql_lookup_graph()
    database_node = next(node for node in base.nodes if node.kind == "sql")
    assert database_node.sql is not None
    writer = database_node.model_copy(
        update={"sql": database_node.sql.model_copy(update={"write": True})}
    )
    agent = SwarmNode(
        id="reader",
        kind="agent",
        title="Reader",
        intent="Read it.",
        position=database_node.position,
        template="chat",
        io=NodeIo(input_type="str", output_type="list[json]"),
        agent=AgentSpec(
            instructions="Read it.", tools=[f"sql:{database_node.id}"]
        ),
    )
    graph = base.model_copy(
        update={
            "nodes": [
                writer if node.id == database_node.id else node
                for node in [*base.nodes, agent]
            ]
        }
    )

    # Phase 1 refuses the wiring outright, which is the real guarantee.
    assert "db_write_as_tool" in {finding.code for finding in review(graph).errors}
