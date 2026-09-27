"""The PydanticAI emitter's database-node surface (PLAN-DB-NODES.md §4.4).

Fast tests: each scaffolds a fixture (or one of its variations) and asserts on the
emitted text, or calls one renderer directly. They cover the emitter's own
decisions -- the deterministic step bodies, the seed files, the extras/env/README
gating, the repository tree's placement and the agent repository tools -- while
the end-to-end keyless gate over the same fixtures belongs to the slow suite
(``tests/test_db_codegen.py``), which Task G owns.

The mock *behaviour* those bodies call is tested against the rendered repository
module by ``tests/test_database_templates.py``; here the bodies are what is under
test, and the one place the two meet is
:func:`test_the_step_seed_path_names_a_file_the_scaffold_wrote`.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from fixtures.graphs import (
    database_agent_tool_graph,
    linear_chat_graph,
    nosql_query_graph,
    sql_lookup_graph,
    two_sql_nodes_different_seed_graph,
    vector_search_graph,
)
from swarm_builder.compile import (
    ResolvedModel,
    body_marker_begin,
    body_marker_end,
    default_scaffold_model,
)
from swarm_builder.compile.boundary import (
    BoundaryViolationError,
    capture_baseline,
    check_boundary,
)
from swarm_builder.compile.fake_fill import (
    FILLABLE_NODE_KINDS,
    apply_fake_fill,
    stub_body_for,
)
from swarm_builder.compile.scaffold import (
    PORT_TYPE_RUNTIME_ORIGIN,
    PORT_TYPE_SAMPLE_INPUT,
    _database_constant_names,
    _inject_repository_tools,
    _render_agent_factory,
    _render_step,
    scaffold,
)
from swarm_builder.models import (
    AgentSpec,
    NodeIo,
    Position,
    ProgrammaticSpec,
    SwarmGraph,
    SwarmNode,
)

#: kind -> graph fixture, and the extension its seed file carries.
DATABASE_FIXTURES: dict[str, tuple[object, str]] = {
    "sql": (sql_lookup_graph, "sql"),
    "nosql": (nosql_query_graph, "json"),
    "vector": (vector_search_graph, "json"),
}

#: The kind-specific constant a step's body reads, per kind.
KIND_CONSTANTS = {
    "sql": "SQL_QUERY",
    "nosql": "NOSQL_FILTER",
    "vector": "VECTOR_TOP_K",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scaffold(graph: SwarmGraph, tmp_path: Path, model: ResolvedModel | None = None) -> Path:
    """Scaffold ``graph`` into a fresh directory and return it."""
    project = tmp_path / "project"
    scaffold(graph, project, model or default_scaffold_model())
    return project


def _database_node(graph: SwarmGraph) -> SwarmNode:
    """The graph's single database node."""
    return next(node for node in graph.nodes if node.kind in DATABASE_FIXTURES)


def _step_source(project: Path, node_id: str) -> str:
    return (project / "src" / "swarm_workflow" / "steps" / f"{node_id}.py").read_text()


def _agent_source(project: Path, node_id: str) -> str:
    return (project / "src" / "swarm_workflow" / "agents" / f"{node_id}.py").read_text()


def _body_region(source: str, node_id: str) -> str:
    """The text strictly between a module's two body markers."""
    lines = source.splitlines()
    begin = next(
        index for index, line in enumerate(lines) if line.strip() == body_marker_begin(node_id)
    )
    end = next(
        index
        for index, line in enumerate(lines)
        if index > begin and line.strip() == body_marker_end(node_id)
    )
    return "\n".join(lines[begin + 1 : end])


def _route_with_env_lines() -> ResolvedModel:
    """A resolved route that names a variable, like every route the pipeline emits.

    The test-fixture model (``default_scaffold_model``) deliberately emits no
    ``.env.example`` lines, which is its own documented case below.
    """
    return ResolvedModel(
        helper_source=(
            'DEFAULT_MODEL: str = "test:model"\n'
            "\n"
            "\n"
            "def _resolve_default_model() -> str:\n"
            "    return DEFAULT_MODEL\n"
        ),
        default_factory_name="_resolve_default_model",
        env_lines=("SWARM_MODEL=test:model",),
        readme_model_note="Inherited default model: test:model.",
    )


def _with_database_node(
    graph: SwarmGraph, node_id: str, **updates: object
) -> SwarmGraph:
    """``graph`` with one database node's fields replaced."""
    nodes = [
        node.model_copy(update=updates) if node.id == node_id else node for node in graph.nodes
    ]
    return graph.model_copy(update={"nodes": nodes})


# ---------------------------------------------------------------------------
# steps/<id>.py: the emitted body and the constants outside it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(DATABASE_FIXTURES))
def test_database_step_constants_sit_outside_the_body_region(kind: str, tmp_path: Path) -> None:
    """§4.4: the operation and the seed path are emitted *outside* the body marker
    region, so the fill stage can never rewrite what a database node runs, and the
    body region inside it is non-empty (``check_boundary`` rejects an empty one
    even for a file nobody fills)."""
    make_graph, _suffix = DATABASE_FIXTURES[kind]
    graph = make_graph()  # type: ignore[operator]
    node = _database_node(graph)
    source = _step_source(_scaffold(graph, tmp_path), node.id)
    body = _body_region(source, node.id)
    assert body.strip(), "a database step's body region must not be empty"
    definitions = [
        line for line in source.splitlines() if line.startswith(("SEED_PATH =", "SEED_PATH ="))
    ]
    assert definitions, "the step must name its seed file as a module-level constant"
    for line in (f"{KIND_CONSTANTS[kind]} =",):
        assert line in source
        assert line not in body
    assert "SEED_PATH =" not in body
    assert not any(line.startswith("raise NotImplementedError") for line in body.splitlines())


@pytest.mark.parametrize("kind", sorted(DATABASE_FIXTURES))
def test_a_database_step_body_calls_its_own_declared_operation(kind: str, tmp_path: Path) -> None:
    """One call, the node's own operation, then the declared port coercion."""
    make_graph, _suffix = DATABASE_FIXTURES[kind]
    graph = make_graph()  # type: ignore[operator]
    node = _database_node(graph)
    body = _body_region(_step_source(_scaffold(graph, tmp_path), node.id), node.id)
    source = _step_source(_scaffold(graph, tmp_path), node.id)
    assert "result = as_port(rows, \"list[json]\")" in body
    assert body.rstrip().endswith("return result")
    if kind == "sql":
        assert "repo = get_sql_repository(SEED_PATH)" in body
        assert 'repo.query(SQL_QUERY, {"input": ctx.inputs})' in body
        assert "GROUP BY" not in body  # nothing is invented around the declared query
    elif kind == "nosql":
        assert "repo = get_document_repository(SEED_PATH, collection=NOSQL_COLLECTION)" in body
        # The sentinel binding is delegated to the shared repository layer rather
        # than inlined here: the langgraph emitter calls the same function, and an
        # inlined, top-level-only substitution silently matched nothing in an `$in`
        # shape while the other target returned the rows.
        assert "filters = bind_input_filter(NOSQL_FILTER, ctx.inputs)" in body
        assert "bind_input_filter" in source.split("async def")[0]
        assert "rows = repo.find(filters, limit=NOSQL_LIMIT)" in body
    else:
        assert "repo = get_vector_repository(SEED_PATH, collection=VECTOR_COLLECTION)" in body
        assert (
            "rows = repo.search(ctx.inputs, top_k=VECTOR_TOP_K, min_score=VECTOR_MIN_SCORE)"
            in body
        )


def test_the_declared_query_is_copied_verbatim_into_the_step(tmp_path: Path) -> None:
    """The emitted statement *is* the document's: no rewrite, no model in the loop."""
    graph = sql_lookup_graph()
    node = _database_node(graph)
    assert node.sql is not None
    source = _step_source(_scaffold(graph, tmp_path), node.id)
    assert re.search(r"SQL_QUERY = .*WHERE c\.name = :input ORDER BY o\.id", source)
    assert node.sql.query in ast.literal_eval(
        re.search(r"SQL_QUERY = (.+)", source).group(1)  # type: ignore[union-attr]
    )


def test_the_nodes_note_is_emitted_as_a_comment_above_the_constants(tmp_path: Path) -> None:
    """The Inspector's help text is visible in the file a reader edits."""
    graph = sql_lookup_graph()
    node = _database_node(graph)
    assert node.sql is not None and node.sql.note
    source = _step_source(_scaffold(graph, tmp_path), node.id)
    assert f"# {node.sql.note}" in source
    assert source.index(f"# {node.sql.note}") < source.index(f"async def {node.id}(")


def test_a_multiline_note_cannot_end_the_comment(tmp_path: Path) -> None:
    """A note is free text: every line of it is prefixed, or the rest would be code."""
    graph = sql_lookup_graph()
    node = _database_node(graph)
    assert node.sql is not None
    spec = node.sql.model_copy(update={"note": "first line\nsecond import os\nthird"})
    graph = _with_database_node(graph, node.id, sql=spec)
    source = _step_source(_scaffold(graph, tmp_path), node.id)
    assert "# first line\n# second import os\n# third\n" in source


def test_a_declared_write_field_receives_the_coerced_rows(tmp_path: Path) -> None:
    """A database node is a step like any other: a declared ``writes`` is assigned,
    so a downstream ``reads`` never observes an unset state field."""
    graph = _with_database_node(sql_lookup_graph(), "orders_db", writes=["orders"])
    body = _body_region(_step_source(_scaffold(graph, tmp_path), "orders_db"), "orders_db")
    assert body.count('result = as_port(rows, "list[json]")') == 1
    assert "ctx.state.orders = result" in body
    assert body.index("ctx.state.orders = result") < body.index("return result")


@pytest.mark.parametrize("kind", sorted(DATABASE_FIXTURES))
def test_every_emitted_python_file_parses(kind: str, tmp_path: Path) -> None:
    """The rendered tree (repository layer included) is valid Python -- the golden
    render the scaffolder itself captures only proves the graph module *imports*."""
    make_graph, _suffix = DATABASE_FIXTURES[kind]
    project = _scaffold(make_graph(), tmp_path)  # type: ignore[operator]
    for path in sorted(project.rglob("*.py")):
        ast.parse(path.read_text(), filename=str(path))


@pytest.mark.parametrize("kind", sorted(DATABASE_FIXTURES))
def test_scaffolding_a_database_fixture_twice_is_byte_identical(
    kind: str, tmp_path: Path
) -> None:
    """Determinism: the same document scaffolds to the same bytes, repository layer
    and seed files included (the no-database case is covered in test_emit_graph.py)."""
    make_graph, _suffix = DATABASE_FIXTURES[kind]
    first = _scaffold(make_graph(), tmp_path / "a")  # type: ignore[operator]
    second = _scaffold(make_graph(), tmp_path / "b")  # type: ignore[operator]
    for path in sorted(p for p in first.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        relative = path.relative_to(first)
        assert path.read_text() == (second / relative).read_text(), str(relative)


# ---------------------------------------------------------------------------
# SQL binding rules (§4.3), for the input types a clean document can declare
# ---------------------------------------------------------------------------


def test_a_str_input_binds_the_single_input_placeholder(tmp_path: Path) -> None:
    project = _scaffold(sql_lookup_graph(), tmp_path)
    body = _body_region(_step_source(project, "orders_db"), "orders_db")
    assert 'repo.query(SQL_QUERY, {"input": ctx.inputs})' in body


def test_a_json_input_binds_its_own_keys_and_input_only_when_declared(tmp_path: Path) -> None:
    """§4.3: a ``json`` input's keys bind directly, and ``:input`` binds the whole
    dict too -- but only when the query declares that placeholder, because binding a
    parameter a query does not have is a driver error at run time."""
    graph = sql_lookup_graph()
    node = _database_node(graph)
    assert node.sql is not None
    json_io = NodeIo(input_type="json", output_type="list[json]")

    declares_input = node.sql.model_copy(update={"query": "SELECT * FROM orders WHERE id = :input"})
    aliases = node.sql.model_copy(
        update={"query": "SELECT * FROM orders WHERE id = :order_id AND total > :floor"}
    )
    with_input = _with_database_node(
        graph, node.id, io=json_io, sql=declares_input
    )
    with_aliases = _with_database_node(graph, node.id, io=json_io, sql=aliases)

    body_with_input = _body_region(
        _step_source(_scaffold(with_input, tmp_path / "a"), node.id), node.id
    )
    body_with_aliases = _body_region(
        _step_source(_scaffold(with_aliases, tmp_path / "b"), node.id), node.id
    )
    assert 'repo.query(SQL_QUERY, {**ctx.inputs, "input": ctx.inputs})' in body_with_input
    assert "repo.query(SQL_QUERY, dict(ctx.inputs))" in body_with_aliases


def test_a_list_str_input_expands_the_placeholder(tmp_path: Path) -> None:
    """§4.3: neither SQLite nor the live driver can bind a list to one placeholder,
    so the query is rewritten alongside its parameters by the shared helper."""
    graph = sql_lookup_graph()
    node = _database_node(graph)
    assert node.sql is not None
    spec = node.sql.model_copy(update={"query": "SELECT * FROM orders WHERE id IN (:input)"})
    graph = _with_database_node(
        graph, node.id, io=NodeIo(input_type="list[str]", output_type="list[json]"), sql=spec
    )
    source = _step_source(_scaffold(graph, tmp_path), node.id)
    assert "repo.query(*expand_list_param(SQL_QUERY, ctx.inputs))" in _body_region(source, node.id)
    assert (
        "from swarm_workflow.repositories.portshape import as_port, expand_list_param" in source
    )


def test_a_write_node_calls_execute_and_is_never_an_agent_tool(tmp_path: Path) -> None:
    """A declared write is a declaration, not a hint: it selects ``execute()`` in the
    step, and the tool renderer never reaches it (see the read-only tool tests)."""
    graph = sql_lookup_graph()
    node = _database_node(graph)
    assert node.sql is not None
    graph = _with_database_node(
        graph, node.id, sql=node.sql.model_copy(update={"write": True})
    )
    body = _body_region(_step_source(_scaffold(graph, tmp_path), node.id), node.id)
    assert "repo.execute(SQL_QUERY, {\"input\": ctx.inputs})" in body
    assert 'rows = [{"rows_affected": repo.execute(' in body
    assert "repo.query(" not in body


@pytest.mark.parametrize(
    "operation,expected",
    [
        ("find", "rows = repo.find(filters, limit=NOSQL_LIMIT)"),
        ("find_one", "rows = [document] if document is not None else []"),
        ("count", 'rows = [{"count": repo.count(filters)}]'),
        ("insert_one", 'rows = [{"id": repo.insert_one(document)}]'),
    ],
)
def test_each_nosql_operation_has_a_deterministic_body(
    operation: str, expected: str, tmp_path: Path
) -> None:
    """Every declared operation maps to one emitted call, and every one of them
    still produces the ``list[json]`` rows its declared output type promises."""
    graph = nosql_query_graph()
    node = _database_node(graph)
    assert node.nosql is not None
    spec = node.nosql.model_copy(update={"operation": operation})
    graph = _with_database_node(graph, node.id, nosql=spec)
    source = _step_source(_scaffold(graph, tmp_path), node.id)
    body = _body_region(source, node.id)
    assert expected in body
    assert body.rstrip().endswith("return result")
    if operation == "insert_one":
        # An insert binds no filter, so no filter constant is emitted for it.
        assert "NOSQL_FILTER =" not in source


# ---------------------------------------------------------------------------
# Seed files and the repository tree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(DATABASE_FIXTURES))
def test_the_step_seed_path_names_a_file_the_scaffold_wrote(kind: str, tmp_path: Path) -> None:
    """The step's ``SEED_PATH`` and the renderer's seed file must be the same file.

    This is the one thing the emitter duplicates from ``compile/database.py`` (the
    kind -> extension map), and this assertion is what keeps the two from drifting:
    a seed the mock never loads would only show up at run time.
    """
    make_graph, suffix = DATABASE_FIXTURES[kind]
    graph = make_graph()  # type: ignore[operator]
    node = _database_node(graph)
    project = _scaffold(graph, tmp_path)
    source = _step_source(project, node.id)
    assert f'"seed" / "{node.id}.{suffix}"' in source
    seed = project / "src" / "swarm_workflow" / "repositories" / "seed" / f"{node.id}.{suffix}"
    assert seed.is_file()


def test_seed_files_are_rendered_from_each_nodes_own_spec(tmp_path: Path) -> None:
    """Per *node*, not per kind: two SQL nodes with different seeds get two files, and
    a recompile rewrites both (so a stale seed cannot survive)."""
    project = _scaffold(two_sql_nodes_different_seed_graph(), tmp_path)
    seed_dir = project / "src" / "swarm_workflow" / "repositories" / "seed"
    seeds = sorted(path.name for path in seed_dir.iterdir())
    assert seeds == ["orders_db.sql", "orders_db_again.sql"]
    first = (seed_dir / "orders_db.sql").read_text()
    second = (seed_dir / "orders_db_again.sql").read_text()
    assert first != second, "each node's declared seed must reach its own seed file"
    assert "Umbrella" in second and "Umbrella" not in first


def test_seed_contents_match_the_declared_seed(tmp_path: Path) -> None:
    graph = nosql_query_graph()
    node = _database_node(graph)
    assert node.nosql is not None
    project = _scaffold(graph, tmp_path)
    seed_path = project / "src" / "swarm_workflow" / "repositories" / "seed" / f"{node.id}.json"
    seed = json.loads(seed_path.read_text())
    assert seed == node.nosql.seed


def test_the_repository_tree_is_emitted_only_for_the_used_kinds(tmp_path: Path) -> None:
    project = _scaffold(vector_search_graph(), tmp_path)
    repositories = project / "src" / "swarm_workflow" / "repositories"
    assert sorted(path.name for path in repositories.iterdir() if path.is_file()) == [
        "__init__.py",
        "embedding.py",
        "portshape.py",
        "vector.py",
    ]


def test_a_graph_without_a_database_node_emits_no_repository_tree(tmp_path: Path) -> None:
    """§4.4's gate: no ``repositories/`` file, no seed, no extra, no env line -- the
    whole-tree byte-identity of a no-database document depends on this."""
    project = _scaffold(linear_chat_graph(), tmp_path)
    assert not (project / "src" / "swarm_workflow" / "repositories").exists()
    assert "optional-dependencies" not in (project / "pyproject.toml").read_text()
    assert "Database nodes" not in (project / "README.md").read_text()
    assert "SWARM_DB_MODE" not in (project / ".env.example").read_text()
    assert "check_db_mode" not in (project / "validate" / "dry_run.py").read_text()


# ---------------------------------------------------------------------------
# pyproject extras, .env.example, README
# ---------------------------------------------------------------------------


def test_pyproject_extras_cover_exactly_the_used_kinds(tmp_path: Path) -> None:
    text = (_scaffold(sql_lookup_graph(), tmp_path) / "pyproject.toml").read_text()
    head, _, tail = text.partition("\n[project.optional-dependencies]\n")
    assert tail.strip() == 'live-sql = ["psycopg[binary]>=3.2"]'
    # Appended after the existing sections, never interleaved with them.
    assert head.endswith('[tool.hatch.build.targets.wheel]\npackages = ["src/swarm_workflow"]\n')
    assert "live-nosql" not in text and "live-vector" not in text


def test_pyproject_extras_are_absent_when_no_kind_is_used(tmp_path: Path) -> None:
    text = (_scaffold(linear_chat_graph(), tmp_path) / "pyproject.toml").read_text()
    assert "[project.optional-dependencies]" not in text


def test_pyproject_project_section_is_unchanged_by_a_database_node(tmp_path: Path) -> None:
    """A database node adds a table; it does not touch the ``[project]`` section."""
    graph = sql_lookup_graph()
    with_db = (_scaffold(graph, tmp_path / "a") / "pyproject.toml").read_text()
    # The same document's name, one node kind apart: only the extras table may differ.
    no_db = linear_chat_graph().model_copy(update={"name": graph.name})
    without_db = (_scaffold(no_db, tmp_path / "b") / "pyproject.toml").read_text()
    assert with_db.partition("\n[project.optional-dependencies]\n")[0] == without_db


def test_env_example_appends_the_database_lines_after_the_route_lines(tmp_path: Path) -> None:
    project = _scaffold(sql_lookup_graph(), tmp_path, _route_with_env_lines())
    text = (project / ".env.example").read_text()
    assert text.startswith("SWARM_MODEL=test:model\n")
    assert "SWARM_DB_MODE=mock" in text
    assert "SWARM_SQL_DSN=" in text
    # Only the engines this graph uses: no driver it cannot reach is suggested.
    assert "SWARM_VECTOR_DSN" not in text and "SWARM_NOSQL_DSN" not in text
    assert text.index("SWARM_MODEL") < text.index("SWARM_DB_MODE")


def test_env_example_names_the_database_variables_even_without_a_model_route(
    tmp_path: Path,
) -> None:
    """Dry run mode has no route, and it is a first-class documented workflow.

    An earlier revision suppressed the whole file when the route contributed no
    lines, which meant a database graph compiled in dry run mode shipped an empty
    ``.env.example`` -- the one file a reader looks at to go live. The database
    block is therefore per *feature*, not per route: it is emitted whenever the
    graph has a database node. A graph with neither a route nor a database node
    still emits the empty file it always did (``test_a_graph_without_a_database_node...``
    above pins that), which is what keeps the no-database byte-identity.
    """
    project = _scaffold(sql_lookup_graph(), tmp_path)
    text = (project / ".env.example").read_text()
    assert text.startswith("# Database nodes:")
    assert "SWARM_DB_MODE=mock" in text
    assert "SWARM_SQL_DSN=" in text
    # The route's variables are absent because there is no route -- not because
    # the database block replaced them.
    assert "SWARM_MODEL" not in text
    assert "## Database nodes" in (project / "README.md").read_text()


def test_env_example_carries_only_the_used_engines_keys(tmp_path: Path) -> None:
    project = _scaffold(vector_search_graph(), tmp_path, _route_with_env_lines())
    text = (project / ".env.example").read_text()
    assert "SWARM_VECTOR_DSN=" in text
    assert "SWARM_SQL_DSN" not in text and "SWARM_NOSQL_DSN" not in text


def test_readme_names_the_file_the_variables_and_the_exact_extra_command(tmp_path: Path) -> None:
    """Acceptance criterion 8: the exact file to edit, every env var, and the exact
    ``uv sync --extra live-<kind>`` command(s) for the kinds this graph uses."""
    text = (_scaffold(sql_lookup_graph(), tmp_path) / "README.md").read_text()
    assert "## Database nodes" in text
    assert "uv sync --extra live-sql" in text
    assert "SWARM_SQL_DSN" in text
    assert "repositories/sql.py" in text  # the file to edit
    assert "repositories/seed/orders_db.sql" in text
    assert "live-vector" not in text


def test_readme_has_no_database_section_without_a_database_node(tmp_path: Path) -> None:
    text = (_scaffold(linear_chat_graph(), tmp_path) / "README.md").read_text()
    assert "Database nodes" not in text
    assert text.endswith("(the dry run injects `TestModel()`).\n")


# ---------------------------------------------------------------------------
# validate/dry_run.py
# ---------------------------------------------------------------------------


def test_dry_run_asserts_the_mock_mode_only_for_a_database_graph(tmp_path: Path) -> None:
    with_db_project = _scaffold(sql_lookup_graph(), tmp_path / "a")
    without_db_project = _scaffold(linear_chat_graph(), tmp_path / "b")
    with_db = (with_db_project / "validate" / "dry_run.py").read_text()
    without_db = (without_db_project / "validate" / "dry_run.py").read_text()
    assert "def check_db_mode() -> None:" in with_db
    assert "    check_db_mode()" in with_db
    assert "from swarm_workflow.repositories import MOCK, db_mode" in with_db
    assert "check_db_mode" not in without_db
    assert "MOCK" not in without_db


# ---------------------------------------------------------------------------
# Port-type tables (§5 items 2-3)
# ---------------------------------------------------------------------------


def test_list_json_has_a_runtime_origin_and_a_sample_input() -> None:
    """``isinstance(x, list[json])`` raises ``TypeError``, so the dry run needs the
    origin type; and the sample it feeds an entry node declaring that port must
    itself be a list of row objects."""
    assert PORT_TYPE_RUNTIME_ORIGIN["list[json]"] == "list"
    assert PORT_TYPE_SAMPLE_INPUT["list[json]"] == '[{"key": "value"}]'
    assert ast.literal_eval(PORT_TYPE_SAMPLE_INPUT["list[json]"]) == [{"key": "value"}]


# ---------------------------------------------------------------------------
# Agent -> repository tools (§4.4)
# ---------------------------------------------------------------------------


def test_no_database_step_body_is_ever_model_filled(tmp_path: Path) -> None:
    """The "no model call" regression: database nodes are not fillable, and a fake
    fill leaves their bodies exactly as the emitter wrote them."""
    assert FILLABLE_NODE_KINDS == {"programmatic"}
    graph = sql_lookup_graph()
    project = _scaffold(graph, tmp_path)
    before = _step_source(project, "orders_db")
    filled = apply_fake_fill(project, graph)
    assert filled == ("format_orders",)
    assert _step_source(project, "orders_db") == before


def test_a_tool_only_reads_even_when_the_node_declares_a_write() -> None:
    """No path exists from a model's tool argument to ``execute()``/``insert_one()``:
    a write-declared node's tool runs the read operation instead. Phase 1 rejects such
    a node as a tool in the first place (``db_write_as_tool``); the emitter's read
    fallback is what keeps the module it writes valid regardless."""
    sql_graph = sql_lookup_graph()
    sql_node = _database_node(sql_graph)
    assert sql_node.sql is not None
    sql_graph = _with_database_node(
        sql_graph, sql_node.id, sql=sql_node.sql.model_copy(update={"write": True})
    )
    sql_source = _render_agent_factory(*_with_tool_agent(sql_graph, ["sql:orders_db"], sql_node))
    ast.parse(sql_source)
    assert "repo.query(ORDERS_DB_QUERY" in sql_source
    assert "execute" not in sql_source

    nosql_graph = nosql_query_graph()
    nosql_node = _database_node(nosql_graph)
    assert nosql_node.nosql is not None
    nosql_graph = _with_database_node(
        nosql_graph,
        nosql_node.id,
        nosql=nosql_node.nosql.model_copy(update={"operation": "insert_one"}),
    )
    nosql_source = _render_agent_factory(
        *_with_tool_agent(nosql_graph, ["nosql:tickets"], nosql_node)
    )
    ast.parse(nosql_source)
    assert "repo.find(filters, limit=TICKETS_LIMIT)" in nosql_source
    assert "insert_one" not in nosql_source


def _with_tool_agent(
    graph: SwarmGraph,
    tool_entries: list[str],
    anchor: SwarmNode,
    template: str = "chat",
) -> tuple[SwarmGraph, SwarmNode]:
    """``graph`` plus an agent node whose tools are exactly ``tool_entries``."""
    agent = SwarmNode(
        id="reader",
        kind="agent",
        title="Reader",
        intent="Read what the database node returns.",
        position=anchor.position,
        template=template,
        io=NodeIo(input_type="str", output_type="list[json]"),
        agent=AgentSpec(instructions="Read it.", tools=list(tool_entries)),
    )
    extended = graph.model_copy(update={"nodes": [*graph.nodes, agent]})
    return extended, next(node for node in extended.nodes if node.kind == "agent")


def _agent_with_tools(graph: SwarmGraph, tools: list[str], template: str = "chat") -> SwarmGraph:
    """``graph`` with its agent node's ``tools`` (and template) replaced."""
    return graph.model_copy(
        update={
            "nodes": [
                node.model_copy(
                    update={
                        "template": template,
                        "agent": node.agent.model_copy(update={"tools": tools}),
                    }
                )
                if node.kind == "agent" and node.agent is not None
                else node
                for node in graph.nodes
            ]
        }
    )


def test_the_repository_tool_signature_is_the_nodes_own_declared_io() -> None:
    """The tool takes the database node's declared input type and returns its
    declared output type -- never a hardcoded ``(query: str) -> str``.

    The *parameter name* is part of the contract too, and not only cosmetics: a
    parameter called ``query`` with a docstring about "the declared read query" made a
    real model pass a SQL statement, which the tool bound as the declared ``:input``
    parameter and matched nothing -- a silent empty result on the canvas.
    """
    graph = _agent_with_tools(database_agent_tool_graph(), ["vector:product_docs"])
    node = next(node for node in graph.nodes if node.kind == "agent")
    assert (
        "async def product_docs_search(query_text: str) -> list[dict[str, Any]]:"
        in _render_agent_factory(graph, node)
    )

    json_io = NodeIo(input_type="json", output_type="list[json]")
    sql_graph = _with_database_node(sql_lookup_graph(), "orders_db", io=json_io)
    sql_node = _database_node(sql_graph)
    sql_graph, sql_agent = _with_tool_agent(sql_graph, ["sql:orders_db"], sql_node)
    source = _render_agent_factory(sql_graph, sql_agent)
    assert "async def orders_db_query(values: dict[str, Any]) -> list[dict[str, Any]]:" in source
    # The starter's query declares `:input`, so the whole dict binds under it as
    # well as its own keys doing so (the binding rule for a `json` input).
    assert 'repo.query(ORDERS_DB_QUERY, {**values, "input": values})' in source


@pytest.mark.parametrize("template", ["chat", "websearch", "orchestrator"])
def test_every_agent_template_carries_a_repository_tool(template: str) -> None:
    """§4.4: the tool is available to **every** template, not only ``orchestrator``
    (where the delegate tools happen to live). ``chat``/``websearch`` build their
    agent in one ``return Agent(...)``, so the tool is spliced into that body."""
    graph = _agent_with_tools(
        database_agent_tool_graph(), ["vector:product_docs"], template=template
    )
    node = next(node for node in graph.nodes if node.kind == "agent")
    source = _render_agent_factory(graph, node)
    ast.parse(source)
    assert source.count("def build_agent(") == 1
    assert "@agent.tool_plain" in source
    assert "async def product_docs_search(query_text: str) -> list[dict[str, Any]]:" in source
    assert "from swarm_workflow.repositories import get_vector_repository" in source
    assert "PRODUCT_DOCS_SEED_PATH" in source
    assert "return agent" in source


def test_two_database_tools_in_one_agent_do_not_collide() -> None:
    """One agent may attach several database nodes; each tool's constants are
    prefixed by its own node id, so neither shadows the other."""
    graph = sql_lookup_graph()
    vector_node = _database_node(vector_search_graph())
    graph = graph.model_copy(update={"nodes": [*graph.nodes, vector_node]})
    graph, node = _with_tool_agent(
        graph, ["sql:orders_db", "vector:product_docs"], _database_node(graph)
    )
    source = _render_agent_factory(graph, node)
    ast.parse(source)
    assert "async def orders_db_query(" in source
    assert "async def product_docs_search(" in source
    assert "ORDERS_DB_SEED_PATH = " in source
    assert "PRODUCT_DOCS_SEED_PATH = " in source
    assert "from swarm_workflow.repositories import (" not in source
    assert "get_sql_repository" in source and "get_vector_repository" in source


def test_an_unknown_or_mismatched_namespaced_entry_is_skipped() -> None:
    """Phase 1 reports ``db_tool_unknown_node``; Phase 2 must not turn a validation
    finding into a crash -- and a bare, non-namespaced name is ignored exactly as
    before database nodes existed."""
    for tools in (["sql:missing_node"], ["nosql:product_docs"], ["web_search"]):
        graph = _agent_with_tools(database_agent_tool_graph(), tools)
        node = next(node for node in graph.nodes if node.kind == "agent")
        source = _render_agent_factory(graph, node)
        ast.parse(source)
        assert "@agent.tool_plain" not in source, tools
        assert "repositories" not in source, tools


# ---------------------------------------------------------------------------
# Phase 4: the boundary check over the new files
# ---------------------------------------------------------------------------


def test_the_boundary_check_passes_for_a_database_project(tmp_path: Path) -> None:
    """The emitted marker regions stay well-formed with repository tools inside the
    agent module's imports region, and a database step's body region is non-empty --
    ``check_boundary`` is what would otherwise fail the compile in Phase 4."""
    project = _scaffold(database_agent_tool_graph(), tmp_path)
    baseline = capture_baseline(project)
    assert check_boundary(project, baseline).ok
    assert "src/swarm_workflow/steps/product_docs.py" in baseline.permitted_regions
    assert "src/swarm_workflow/agents/summarize_docs.py" in baseline.permitted_regions


def test_repository_files_are_not_boundary_permitted_paths(tmp_path: Path) -> None:
    """``repositories/`` is deliberately *not* in ``DEFAULT_PERMITTED_DIRS``: the fill
    agent has no tool that may edit the repository layer, and a hand-edit is reported
    like any other changed forbidden file."""
    project = _scaffold(sql_lookup_graph(), tmp_path)
    baseline = capture_baseline(project)
    repository = project / "src" / "swarm_workflow" / "repositories" / "sql.py"
    assert "src/swarm_workflow/repositories/sql.py" in baseline.forbidden_hashes
    repository.write_text("# hand-edited\n")
    with pytest.raises(BoundaryViolationError) as caught:
        check_boundary(project, baseline)
    assert [violation.code for violation in caught.value.result.violations] == [
        "forbidden_file_changed"
    ]
    assert caught.value.result.violations[0].path == "src/swarm_workflow/repositories/sql.py"


# ---------------------------------------------------------------------------
# The tool-splicing assertion itself, and the module shapes it produces
# ---------------------------------------------------------------------------


def test_tool_splicing_refuses_a_body_that_is_not_a_single_return() -> None:
    """``chat``/``websearch`` build their agent in one ``return Agent(...)``. A
    template whose body changes shape must fail the compile loudly rather than emit
    a tool nobody registered."""
    module = (
        '"""x"""\n\n\n'
        "def build_agent(model):\n"
        f"    {body_marker_begin('n')}\n"
        "    agent = Agent(model)\n"
        f"    {body_marker_end('n')}\n"
    )
    with pytest.raises(ValueError, match="does not build its agent with a single"):
        _inject_repository_tools(module, "n", "    @agent.tool_plain\n")


def test_tool_splicing_rewrites_the_inline_return_into_a_binding() -> None:
    module = (
        '"""x"""\n\n\n'
        "def build_agent(model):\n"
        f"    {body_marker_begin('n')}\n"
        "    return Agent(\n"
        "        model,\n"
        "    )\n"
        f"    {body_marker_end('n')}\n"
    )
    rewritten = _inject_repository_tools(
        module, "n", "    @agent.tool_plain\n    def t():\n        pass"
    )
    assert "    agent = Agent(\n" in rewritten
    assert "    @agent.tool_plain" in rewritten
    assert rewritten.rstrip().endswith("    return agent\n    # --- swarm:end n ---")
    ast.parse(rewritten)


# ---------------------------------------------------------------------------
# Renderers used directly: the node-prefixed constant names of an agent module
# ---------------------------------------------------------------------------


def test_tool_constants_are_prefixed_by_node_id_not_by_kind() -> None:
    node = _database_node(vector_search_graph())
    step_names = _database_constant_names(node, in_agent_module=False)
    tool_names = _database_constant_names(node, in_agent_module=True)
    assert step_names["seed_path"] == "SEED_PATH"
    assert step_names["top_k"] == "VECTOR_TOP_K"
    assert tool_names["seed_path"] == "PRODUCT_DOCS_SEED_PATH"
    assert tool_names["top_k"] == "PRODUCT_DOCS_TOP_K"


def test_a_non_namespaced_tool_leaves_the_agent_module_untouched() -> None:
    """``agent.tools`` keeps its existing members (``web_search``) and its existing
    role as prompt metadata: only the namespaced form is acted on, so an agent whose
    tools are all catalog names renders exactly the module it rendered before."""
    graph = database_agent_tool_graph()
    node = next(node for node in graph.nodes if node.kind == "agent")
    untouched = _render_agent_factory(graph, node)
    other = _agent_with_tools(graph, [])
    rendered = _render_agent_factory(other, next(n for n in other.nodes if n.id == node.id))
    assert "tool_plain" not in rendered
    assert "repositories" not in rendered
    assert rendered != untouched
    assert rendered.replace(rendered[rendered.index('"""', 3) :], "") == untouched.replace(
        untouched[untouched.index('"""', 3) :], ""
    )


# ---------------------------------------------------------------------------
# The stub-fill expression for the new port type (§5 item 5)
# ---------------------------------------------------------------------------


def test_a_programmatic_list_json_stub_returns_a_list_of_rows(tmp_path: Path) -> None:
    """The stub fill's expression must be valid for ``list[dict[str, Any]]`` and still
    thread the input through, so a fake-fill compile of a graph with a ``list[json]``
    programmatic step passes the keyless gate."""
    node = SwarmNode(
        id="rows_out",
        kind="programmatic",
        title="Rows out",
        intent="Reshape the rows for the next step.",
        position=Position(x=0, y=0),
        io=NodeIo(input_type="list[json]", output_type="list[json]"),
        programmatic=ProgrammaticSpec(needs=[]),
    )
    assert stub_body_for(node).strip() == 'return [{"input": str(ctx.inputs)}]'
    graph = linear_chat_graph().model_copy(
        update={
            "id": "g",
            "name": "Rows",
            "entry_node_id": "rows_out",
            "exit_node_id": "rows_out",
            "nodes": [node],
            "edges": [],
        }
    )
    source = _render_step(graph, node)
    assert "-> list[dict[str, Any]]:" in source
    assert "from typing import Any" in source
    ast.parse(source)


def test_a_sql_repository_tool_never_asks_for_a_query() -> None:
    """Pin the model-facing contract a real run got wrong.

    Verified against `anthropic:claude-sonnet-5`: with a parameter called ``query``
    and a docstring about "the declared read statement", the model passed *a SQL
    statement* as the argument. The tool bound that string to the declared
    ``:input`` parameter, matched nothing, and returned ``[]`` -- so the agent
    answered "no orders on file" while the database step one node earlier had
    returned two rows for that customer. The guard held (no model-authored SQL ever
    executed), but the documented use failed *silently*, which is the worst failure
    mode this feature has.

    The three things that prevent it are therefore pinned together: a parameter
    named for what it holds, the declared statement quoted in the docstring so the
    model can see what its argument binds to, and an explicit instruction that the
    statement is fixed.
    """
    graph = sql_lookup_graph()
    sql_node = next(node for node in graph.nodes if node.kind == "sql")
    agent = next(node for node in graph.nodes if node.kind == "programmatic")
    agent = agent.model_copy(
        update={"agent": AgentSpec(instructions="look it up", tools=[f"sql:{sql_node.id}"])}
    )

    source = _render_agent_factory(graph, agent)

    assert "async def orders_db_query(input_value: str) -> list[dict[str, Any]]:" in source
    assert "input_value(query: str)" not in source
    # The declaration is quoted, so the model can see the placeholder it fills.
    assert ":input" in source
    assert "never a SQL statement" in source


def test_a_nosql_agent_tool_binds_its_filter_through_the_shared_helper() -> None:
    """A read-only NoSQL tool must import the binding helper it calls.

    Found by inspection while fixing the cross-target sentinel bug: the tool path
    emitted `filters = bind_input_filter(...)` while its import line still listed
    only `as_port`, so a NoSQL node attached as an agent tool would have raised
    `NameError` the moment the model called it -- and no fixture covered a NoSQL
    agent tool, so nothing would have caught it. The import is now derived from the
    same predicate as the call site.
    """
    graph = nosql_query_graph()
    node = _database_node(graph)
    graph, agent = _with_tool_agent(graph, ["nosql:tickets"], node)

    source = _render_agent_factory(graph, agent)

    assert "filters = bind_input_filter(TICKETS_FILTER, input_value)" in source
    import_line = next(
        line for line in source.splitlines() if "repositories.portshape import" in line
    )
    assert "bind_input_filter" in import_line
    assert "as_port" in import_line


def test_a_nosql_write_step_does_not_import_the_binding_helper(tmp_path: Path) -> None:
    """``insert_one`` takes its document from the input and binds no filter.

    The import is therefore conditional, so the generated module carries no unused
    import -- which would also be a misleading signal to a reader about what the
    operation does.
    """
    node = _database_node(nosql_query_graph())
    assert node.nosql is not None
    graph = _with_database_node(
        nosql_query_graph(),
        "tickets",
        nosql=node.nosql.model_copy(update={"operation": "insert_one"}),
    )
    source = _step_source(_scaffold(graph, tmp_path), "tickets")

    assert "bind_input_filter" not in source
    assert 'repo.insert_one(document)' in source
