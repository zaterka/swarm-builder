"""Tests for the database starter catalog, the shared emission module, and the
mock implementations the templates render.

The mock-behaviour half of this file runs against the **rendered** repository
modules: ``database_files_for`` is called, its output is written into a
temporary project tree, and the modules are imported from those files on disk.
The template is therefore the thing under test -- a template that rendered
something subtly different from what these tests exercise could not pass.

Nothing here needs a network, a driver, a server or a model: the whole mock path
is standard library. Live adapters are never imported (their drivers are opt-in
extras), and the only claims made about them are structural: that the driver
import sits inside the live branch, so importing the generated package stays
keyless, and that a missing DSN names itself.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from fixtures.graphs import (
    database_agent_tool_graph,
    linear_chat_graph,
    nosql_query_graph,
    sql_lookup_graph,
    two_sql_nodes_different_seed_graph,
    two_sql_nodes_same_seed_graph,
    vector_search_graph,
)
from swarm_builder.compile.database import (
    _ENV_EXAMPLE_VALUES,
    DATABASE_KINDS,
    database_env_lines,
    database_extra_lines,
    database_files_for,
    database_readme_section,
    used_db_kinds,
)
from swarm_builder.models import NosqlSpec, SqlSpec, SwarmGraph, VectorSpec
from swarm_builder.templates.database import (
    DATABASE_CATALOG,
    DATABASE_DIR,
    KIND_ORDER,
    SHARED_TEMPLATE_FILES,
    get_database_entry,
    load_starter_io,
    load_starter_spec,
    read_template,
)
from swarm_builder.templates.registry import TEMPLATE_CATALOG

#: The package name a rendered tree is written under. Deliberately *not*
#: ``swarm_workflow``: these tests must not be able to import a generated
#: project that happens to be importable in the test environment.
RENDER_PACKAGE = "swarm_workflow_test_render"

#: What a rendered module may import at module level: the standard library (and
#: its own package). Anything else would break the keyless gate, whose second
#: step is ``import <package>.graph`` with nothing installed.
_STDLIB = set(sys.stdlib_module_names)


# ---------------------------------------------------------------------------
# Rendering helper: a real tree on disk, imported from its files.
# ---------------------------------------------------------------------------


class RenderedProject:
    """A rendered repository layer on disk, imported by file.

    The package and its ``repositories`` subpackage are registered with an
    explicit ``submodule_search_locations``, so their own submodules resolve from
    the rendered directory while the rendered modules keep the absolute
    ``from <package>.repositories...`` imports a generated project uses.
    """

    def __init__(self, root: Path, package: str = RENDER_PACKAGE) -> None:
        self.root = root
        self.package = package

    def bootstrap(self) -> None:
        """Import the rendered package and its ``repositories`` package."""
        self._load(self.package, self.root / self.package / "__init__.py", is_package=True)
        self._load(
            f"{self.package}.repositories",
            self.root / self.package / "repositories" / "__init__.py",
            is_package=True,
        )

    def module(self, name: str) -> Any:
        """Import ``<package>.repositories.<name>`` from its rendered file."""
        dotted = f"{self.package}.repositories.{name}"
        if dotted in sys.modules:
            return sys.modules[dotted]
        return self._load(
            dotted, self._path(name), is_package=False
        )

    def repositories(self) -> Any:
        """The rendered factory module (``repositories/__init__.py``)."""
        return sys.modules[f"{self.package}.repositories"]

    def source(self, name: str) -> str:
        """The rendered module's text, for the structural assertions."""
        return self._path(name).read_text()

    def seed_path(self, node_id: str, suffix: str) -> Path:
        """One node's rendered seed file."""
        return self.root / self.package / "repositories" / "seed" / f"{node_id}.{suffix}"

    def _path(self, name: str) -> Path:
        return self.root / self.package / "repositories" / f"{name}.py"

    @staticmethod
    def _load(dotted: str, path: Path, is_package: bool) -> Any:
        if is_package:
            spec = importlib.util.spec_from_file_location(
                dotted, path, submodule_search_locations=[str(path.parent)]
            )
        else:
            spec = importlib.util.spec_from_file_location(dotted, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[dotted] = module
        spec.loader.exec_module(module)
        return module


def _unload() -> None:
    """Drop every module of every rendered package from ``sys.modules``.

    Without this, two tests rendering the same package name would share the
    first test's modules -- and its mock instances, and its ``SWARM_DB_MODE``
    reading.
    """
    rendered = [
        module
        for module in list(sys.modules)
        if module == RENDER_PACKAGE or module.startswith(RENDER_PACKAGE)
    ]
    for name in rendered:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def _clean_rendered_modules() -> Any:
    """Unload the rendered packages around every test in this module."""
    _unload()
    yield
    _unload()


def _render(root: Path, graph: SwarmGraph, package: str = RENDER_PACKAGE) -> RenderedProject:
    """Write one graph's rendered repository files under ``root`` and import them."""
    (root / package).mkdir(parents=True, exist_ok=True)
    (root / package / "__init__.py").write_text('"""Rendered package."""\n')
    for relative_path, content in database_files_for(graph, package).items():
        destination = root / package / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
    project = RenderedProject(root, package)
    project.bootstrap()
    return project


@pytest.fixture
def sql_project(tmp_path: Path) -> RenderedProject:
    """The SQL fixture's rendered repository layer.

    Each kind's fixture renders under its *own* package name: three projects
    bootstrapped under one name in a single test would overwrite each other's
    modules in ``sys.modules``.
    """
    return _render(tmp_path / "sql_render", sql_lookup_graph(), f"{RENDER_PACKAGE}_sql")


@pytest.fixture
def nosql_project(tmp_path: Path) -> RenderedProject:
    """The NoSQL fixture's rendered repository layer."""
    return _render(tmp_path / "nosql_render", nosql_query_graph(), f"{RENDER_PACKAGE}_nosql")


@pytest.fixture
def vector_project(tmp_path: Path) -> RenderedProject:
    """The vector fixture's rendered repository layer."""
    return _render(tmp_path / "vector_render", vector_search_graph(), f"{RENDER_PACKAGE}_vector")


def _sql_repository(project: RenderedProject, node_id: str = "orders_db") -> Any:
    """The repository the rendered factory returns for one node's seed."""
    return project.repositories().get_sql_repository(project.seed_path(node_id, "sql"))


def _document_repository(project: RenderedProject, node_id: str = "tickets") -> Any:
    """The document repository the rendered factory returns for one node's seed."""
    return project.repositories().get_document_repository(project.seed_path(node_id, "json"))


def _vector_repository(project: RenderedProject, node_id: str = "product_docs") -> Any:
    """The vector repository the rendered factory returns for one node's seed."""
    return project.repositories().get_vector_repository(project.seed_path(node_id, "json"))


# ---------------------------------------------------------------------------
# The starter catalog
# ---------------------------------------------------------------------------


class TestStarterCatalog:
    def test_catalog_has_exactly_the_three_database_kinds(self) -> None:
        assert set(DATABASE_CATALOG) == {"sql", "nosql", "vector"}
        assert set(KIND_ORDER) == set(DATABASE_CATALOG)
        assert DATABASE_KINDS == frozenset(KIND_ORDER)

    def test_every_entry_exposes_its_whole_contract(self) -> None:
        for kind, entry in DATABASE_CATALOG.items():
            assert entry.kind == kind
            assert entry.label
            assert entry.description
            assert entry.package_module
            assert entry.live_extra_name.startswith("live-")
            assert entry.live_extra_deps
            assert entry.env_vars
            assert entry.manifest

    def test_each_starter_spec_validates_against_its_own_model(self) -> None:
        models = {"sql": SqlSpec, "nosql": NosqlSpec, "vector": VectorSpec}
        for kind, entry in DATABASE_CATALOG.items():
            assert isinstance(entry.starter_spec, models[kind])
            # And the *file* validates too, so a hand-edited starter cannot
            # diverge from what the catalog serves.
            raw = json.loads((DATABASE_DIR / kind / "starter.json").read_text())
            assert isinstance(models[kind].model_validate(raw["spec"]), models[kind])

    def test_the_loaders_reread_the_file_and_agree_with_the_catalog(self) -> None:
        for kind in KIND_ORDER:
            assert load_starter_spec(kind) == DATABASE_CATALOG[kind].starter_spec
            assert load_starter_io(kind) == DATABASE_CATALOG[kind].starter_io

    def test_starter_io_is_the_mandatory_pair_for_every_kind(self) -> None:
        for kind, entry in DATABASE_CATALOG.items():
            assert entry.starter_io.input_type == "str", kind
            assert entry.starter_io.output_type == "list[json]", kind

    def test_every_starter_seed_is_non_empty_and_parses_for_its_kind(self) -> None:
        """The plan's ``db_seed_invalid`` probe, in the starter's own terms.

        Phase 1 owns that code and does not exist yet, so the probe is spelled
        out here exactly as the plan describes it: a SQL seed must run through a
        real in-memory ``sqlite3`` (stdlib only, no I/O), and a document/vector
        seed must be a non-empty JSON array of objects.
        """
        sql_spec = get_database_entry("sql").starter_spec
        assert isinstance(sql_spec, SqlSpec)
        assert sql_spec.seed_sql.strip()
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(sql_spec.seed_sql)
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        finally:
            connection.close()
        assert [row[0] for row in tables] == ["customers", "orders"]

        nosql_spec = get_database_entry("nosql").starter_spec
        assert isinstance(nosql_spec, NosqlSpec)
        assert len(nosql_spec.seed) == 3
        assert all(isinstance(document, dict) for document in nosql_spec.seed)

        vector_spec = get_database_entry("vector").starter_spec
        assert isinstance(vector_spec, VectorSpec)
        assert len(vector_spec.seed) == 4
        assert all(document.id and document.text for document in vector_spec.seed)

    def test_sql_starter_is_the_documented_customers_orders_example(self) -> None:
        spec = get_database_entry("sql").starter_spec
        assert isinstance(spec, SqlSpec)
        assert spec.query == (
            "SELECT o.id, o.total FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE c.name = :input ORDER BY o.id"
        )
        assert spec.write is False
        assert spec.note
        # Three customers and three orders, as the catalog documents.
        assert len(re.findall(r"^    \(\d+, '", spec.seed_sql, flags=re.MULTILINE)) == 3
        assert len(re.findall(r"^    \(\d+, \d+, ", spec.seed_sql, flags=re.MULTILINE)) == 3

    def test_nosql_starter_is_the_documented_tickets_example(self) -> None:
        spec = get_database_entry("nosql").starter_spec
        assert isinstance(spec, NosqlSpec)
        assert spec.collection == "tickets"
        assert spec.operation == "find"
        assert spec.filter == {"status": "$input"}
        assert spec.limit == 20

    def test_vector_starter_is_the_documented_product_docs_example(self) -> None:
        spec = get_database_entry("vector").starter_spec
        assert isinstance(spec, VectorSpec)
        assert spec.collection == "product_docs"
        assert spec.top_k == 4
        assert spec.min_score == 0.0
        assert [document.id for document in spec.seed] == ["doc-1", "doc-2", "doc-3", "doc-4"]

    def test_the_vector_entry_lists_its_optional_api_key_and_no_other_kind_does(
        self,
    ) -> None:
        """A hosted Qdrant needs a key on top of its URL; the other two do not.

        Postgres and MongoDB take their credentials inside the DSN, so neither
        grows a second variable -- and the key sits with the DSN rather than
        after the collection, because which collection is read is a different
        fact from how the connection is authenticated.
        """
        assert get_database_entry("vector").env_vars == (
            "SWARM_VECTOR_DSN",
            "SWARM_VECTOR_API_KEY",
            "SWARM_VECTOR_COLLECTION",
        )
        for kind in ("sql", "nosql"):
            assert "SWARM_VECTOR_API_KEY" not in get_database_entry(kind).env_vars

    def test_the_collection_variables_stay_optional_live_overrides(self) -> None:
        """The plan's recorded reversal: the collection is not banned, not required.

        Every emitted step and tool passes the node's declared collection, so
        these remain reachable only from hand-written code -- but they must still
        be offered, which is what keeps them in the catalog.
        """
        assert "SWARM_NOSQL_COLLECTION" in get_database_entry("nosql").env_vars
        assert "SWARM_VECTOR_COLLECTION" in get_database_entry("vector").env_vars

    def test_every_manifest_file_exists_on_disk(self) -> None:
        for entry in DATABASE_CATALOG.values():
            for relative in entry.manifest:
                assert (DATABASE_DIR / entry.kind / relative).is_file(), relative
        for relative in SHARED_TEMPLATE_FILES:
            assert (DATABASE_DIR / relative).is_file(), relative

    def test_read_template_raises_for_a_path_that_is_not_a_template(self) -> None:
        with pytest.raises(FileNotFoundError, match="no database template"):
            read_template("sql/does-not-exist.py.tmpl")

    def test_get_database_entry_raises_on_an_unknown_kind(self) -> None:
        with pytest.raises(ValueError, match="unknown database kind 'graphql'"):
            get_database_entry("graphql")
        with pytest.raises(ValueError, match="expected one of"):
            load_starter_spec("graphql")

    def test_the_agent_template_catalog_still_has_exactly_three_entries(self) -> None:
        """The database catalog must not have widened ``TemplateId``.

        A database node has a *kind*, not a template; the agent catalog's size is
        an existing contract, and keeping this catalog separate is what leaves it
        exactly three.
        """
        assert set(TEMPLATE_CATALOG) == {"chat", "orchestrator", "websearch"}

    def test_env_examples_cover_every_catalog_variable(self) -> None:
        catalog_variables = {
            variable for entry in DATABASE_CATALOG.values() for variable in entry.env_vars
        }
        assert set(_ENV_EXAMPLE_VALUES) == catalog_variables


# ---------------------------------------------------------------------------
# The shared emission module (compile/database.py)
# ---------------------------------------------------------------------------


class TestEmissionGates:
    def test_a_document_without_a_database_node_emits_nothing(self) -> None:
        graph = linear_chat_graph()
        assert used_db_kinds(graph) == frozenset()
        assert database_files_for(graph, "swarm_workflow") == {}
        assert database_extra_lines(graph) == ()
        assert database_env_lines(graph) == ()
        assert database_readme_section(graph) == ""

    def test_used_db_kinds_reports_only_the_kinds_the_graph_uses(self) -> None:
        assert used_db_kinds(sql_lookup_graph()) == frozenset({"sql"})
        assert used_db_kinds(nosql_query_graph()) == frozenset({"nosql"})
        assert used_db_kinds(database_agent_tool_graph()) == frozenset({"vector"})
        assert used_db_kinds(two_sql_nodes_same_seed_graph()) == frozenset({"sql"})

    def test_files_are_gated_on_the_kinds_the_graph_uses(self) -> None:
        assert set(database_files_for(sql_lookup_graph(), "swarm_workflow")) == {
            "repositories/__init__.py",
            "repositories/portshape.py",
            "repositories/sql.py",
            "repositories/seed/orders_db.sql",
        }
        assert set(database_files_for(vector_search_graph(), "swarm_workflow")) == {
            "repositories/__init__.py",
            "repositories/portshape.py",
            "repositories/embedding.py",
            "repositories/vector.py",
            "repositories/seed/product_docs.json",
        }
        assert set(database_files_for(nosql_query_graph(), "swarm_workflow")) == {
            "repositories/__init__.py",
            "repositories/portshape.py",
            "repositories/nosql.py",
            "repositories/seed/tickets.json",
        }

    def test_seed_files_are_rendered_per_node_from_its_own_spec(self) -> None:
        same_seed = database_files_for(two_sql_nodes_same_seed_graph(), "swarm_workflow")
        assert same_seed["repositories/seed/orders_db.sql"] == same_seed[
            "repositories/seed/orders_db_again.sql"
        ]
        different = database_files_for(two_sql_nodes_different_seed_graph(), "swarm_workflow")
        assert different["repositories/seed/orders_db.sql"] != different[
            "repositories/seed/orders_db_again.sql"
        ]

        spec = sql_lookup_graph().nodes[0].sql
        assert isinstance(spec, SqlSpec)
        assert same_seed["repositories/seed/orders_db.sql"] == spec.seed_sql

        documents = json.loads(
            database_files_for(vector_search_graph(), "swarm_workflow")[
                "repositories/seed/product_docs.json"
            ]
        )
        assert [document["id"] for document in documents] == [
            "doc-1",
            "doc-2",
            "doc-3",
            "doc-4",
        ]

    def test_a_node_with_no_spec_contributes_no_seed_rather_than_a_default(self) -> None:
        """A hand-written ``kind="sql"`` node with no spec gets no invented seed.

        Phase 1 reports ``db_empty_operation`` for it before any emitter runs; a
        fabricated seed here would be exactly the hidden default the design
        forbids.
        """
        graph = sql_lookup_graph()
        nodes = [node.model_copy(update={"sql": None}) for node in graph.nodes]
        graph = graph.model_copy(update={"nodes": nodes})
        files = database_files_for(graph, "swarm_workflow")
        assert "repositories/seed/orders_db.sql" not in files
        assert "repositories/sql.py" in files

    def test_the_factory_only_carries_the_getters_of_used_kinds(self) -> None:
        factory = database_files_for(sql_lookup_graph(), "swarm_workflow")[
            "repositories/__init__.py"
        ]
        assert "def get_sql_repository(" in factory
        assert "get_document_repository" not in factory
        assert "get_vector_repository" not in factory
        assert "repositories.nosql" not in factory
        assert "embedding" not in factory

    def test_extras_name_one_extra_per_used_kind(self) -> None:
        assert database_extra_lines(sql_lookup_graph()) == (
            'live-sql = ["psycopg[binary]>=3.2"]',
        )
        assert database_extra_lines(nosql_query_graph()) == ('live-nosql = ["pymongo>=4.9"]',)
        assert database_extra_lines(vector_search_graph()) == (
            'live-vector = ["qdrant-client>=1.12"]',
        )

    def test_env_lines_carry_the_mode_and_only_the_used_engines_variables(self) -> None:
        lines = database_env_lines(sql_lookup_graph())
        assert "SWARM_DB_MODE=mock" in lines
        assert "SWARM_SQL_DSN=postgresql://postgres:postgres@localhost:5432/swarm_workflow" in lines
        assert not any("VECTOR" in line or "NOSQL" in line for line in lines)

        vector_lines = database_env_lines(vector_search_graph())
        assert "SWARM_VECTOR_DSN=http://localhost:6333" in vector_lines
        # The key is optional, but it is still one of the engine's variables, so
        # the example file names it -- empty, which the adapter reads as "no key"
        # rather than as a credential.
        assert "SWARM_VECTOR_API_KEY=" in vector_lines
        assert "SWARM_VECTOR_COLLECTION=swarm_documents" in vector_lines
        assert not any("SQL_DSN" in line for line in vector_lines)

    def test_readme_section_names_the_files_variables_and_commands(self) -> None:
        section = database_readme_section(database_agent_tool_graph())
        assert section.startswith("## Database nodes")
        assert "uv sync --extra live-vector" in section
        assert "SWARM_DB_MODE" in section
        assert "SWARM_VECTOR_DSN" in section
        assert "SWARM_VECTOR_API_KEY" in section
        assert "SWARM_VECTOR_COLLECTION" in section
        assert "repositories/vector.py" in section
        assert "seed/product_docs.json" in section
        # Only the used kind's extra and variables -- never another engine's,
        # which would suggest a driver this graph cannot use.
        assert "live-sql" not in section
        assert "SWARM_SQL_DSN" not in section

    def test_readme_names_the_classes_the_templates_actually_define(self) -> None:
        files = database_files_for(database_agent_tool_graph(), "swarm_workflow")
        section = database_readme_section(database_agent_tool_graph())
        assert "class InMemoryVectorRepository:" in files["repositories/vector.py"]
        assert "class QdrantRepository:" in files["repositories/vector.py"]
        assert "InMemoryVectorRepository" in section
        assert "QdrantRepository" in section

        sql_files = database_files_for(sql_lookup_graph(), "swarm_workflow")
        sql_section = database_readme_section(sql_lookup_graph())
        assert "class SqliteRepository:" in sql_files["repositories/sql.py"]
        assert "class PostgresRepository:" in sql_files["repositories/sql.py"]
        assert "SqliteRepository" in sql_section
        assert "PostgresRepository" in sql_section

    def test_rendering_for_two_packages_produces_two_independent_renders(self) -> None:
        graph = sql_lookup_graph()
        first = database_files_for(graph, "swarm_workflow")
        second = database_files_for(graph, "swarm_workflow_lg")
        assert set(first) == set(second)
        assert "from swarm_workflow.repositories" in first["repositories/__init__.py"]
        assert "from swarm_workflow_lg.repositories" in second["repositories/__init__.py"]
        # No leakage either way. ('swarm_workflow_lg' contains 'swarm_workflow', so
        # the longer name is removed before the check.)
        assert "swarm_workflow." not in second["repositories/__init__.py"].replace(
            "swarm_workflow_lg.", ""
        )

    def test_renderings_are_deterministic_across_calls(self) -> None:
        graph = two_sql_nodes_same_seed_graph()
        assert list(database_files_for(graph, "swarm_workflow")) == list(
            database_files_for(graph, "swarm_workflow")
        )
        assert database_files_for(graph, "swarm_workflow") == database_files_for(
            graph, "swarm_workflow"
        )


# ---------------------------------------------------------------------------
# The rendered modules: shape and import hygiene
# ---------------------------------------------------------------------------


def _every_rendered_file() -> dict[str, str]:
    """Every file a graph using all three kinds renders."""
    graph = sql_lookup_graph()
    nodes = [
        *graph.nodes,
        *nosql_query_graph().nodes,
        *vector_search_graph().nodes,
    ]
    merged = graph.model_copy(
        update={"id": "all-kinds", "nodes": nodes, "exit_node_id": graph.exit_node_id}
    )
    return database_files_for(merged, RENDER_PACKAGE)


class TestRenderedModules:
    @pytest.mark.parametrize("package", ["swarm_workflow", "swarm_workflow_lg"])
    def test_every_rendered_python_file_parses(self, package: str) -> None:
        """Parsed with the interpreter floor the generated project declares.

        ``requires-python = ">=3.11"`` (``GENERATED_PROJECT_REQUIRES_PYTHON``), so
        a template using newer syntax would produce a project that cannot be
        installed where it says it can.
        """
        files = dict(_every_rendered_file())
        files.update(database_files_for(sql_lookup_graph(), package))
        for path, content in files.items():
            if path.endswith(".py"):
                ast.parse(content, feature_version=(3, 11))

    def test_no_driver_is_imported_at_module_level(self) -> None:
        """The keyless gate's second step is ``import <package>.graph``.

        A module-level driver import would make that import fail on every machine
        where the optional extra is not installed -- which is every machine the
        gate runs on.
        """
        for path, content in _every_rendered_file().items():
            if not path.endswith(".py"):
                continue
            for node in ast.parse(content).body:
                roots: set[str] = set()
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    roots = {node.module.split(".")[0]}
                assert roots <= (_STDLIB | {RENDER_PACKAGE}), (
                    f"{path} imports {sorted(roots)} at module level"
                )

    def test_the_driver_imports_are_lazy_seams_inside_the_live_adapters(self) -> None:
        files = _every_rendered_file()
        assert "import psycopg" in files["repositories/sql.py"]
        assert "from pymongo" in files["repositories/nosql.py"]
        assert "from qdrant_client" in files["repositories/vector.py"]
        for path in (
            "repositories/sql.py",
            "repositories/nosql.py",
            "repositories/vector.py",
        ):
            tree = ast.parse(files[path])
            module_level = {
                alias.name.split(".")[0]
                for node in tree.body
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            assert not module_level & {"psycopg", "pymongo", "qdrant_client"}, path

    def test_the_driver_import_sits_inside_the_adapter_not_at_module_level(self) -> None:
        """The lazy import is a function-body import, and it says why.

        The plan calls this the one documented degradation seam: the driver is an
        optional extra, so it must be imported where it is used -- inside the
        constructor -- and never where the package is imported.
        """
        expected = {
            "repositories/sql.py": "import psycopg",
            "repositories/nosql.py": "from pymongo",
            "repositories/vector.py": "from qdrant_client",
        }
        for path, statement in expected.items():
            content = _every_rendered_file()[path]
            lines = content.splitlines()
            tree = ast.parse(content)
            driver_nodes = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                and statement in lines[node.lineno - 1]
            ]
            assert driver_nodes, f"{path} never mentions {statement}"
            for node in driver_nodes:
                line = lines[node.lineno - 1]
                assert line != line.lstrip(), f"{path}: {statement} is not inside a function"
                assert node not in tree.body, f"{path}: {statement} is at module level"
            assert "keyless" in content, path

    def test_no_rendered_module_bakes_in_a_seed_path(self) -> None:
        """Seed paths are runtime arguments, never substitutions.

        A baked path would tie the mock's data to the compile -- which seed file
        existed then, in which directory -- instead of to the node's declared
        spec. The emitted step passes its own path into the factory precisely so
        that no rendered module has to hold one.
        """
        for path, content in _every_rendered_file().items():
            if path.endswith(".py"):
                assert "seed/" not in content, path
                assert "SEED_PATH" not in content, path

    @pytest.mark.parametrize(
        "path",
        [
            "repositories/__init__.py",
            "repositories/portshape.py",
            "repositories/sql.py",
            "repositories/nosql.py",
            "repositories/vector.py",
            "repositories/embedding.py",
        ],
    )
    def test_each_generated_module_has_a_docstring_then_the_future_import(self, path: str) -> None:
        content = _every_rendered_file()[path]
        assert content.startswith('"""'), path
        tree = ast.parse(content)
        assert isinstance(tree.body[1], ast.ImportFrom)
        assert tree.body[1].module == "__future__"

    def test_the_rendered_layer_imports_and_serves_mock_mode_with_nothing_installed(
        self, tmp_path: Path
    ) -> None:
        _unload()
        project = _render(tmp_path / "keyless", database_agent_tool_graph())
        assert project.repositories().db_mode() == "mock"
        installed_drivers = [
            name
            for name in sys.modules
            if name.split(".")[0] in {"psycopg", "pymongo", "qdrant_client"}
        ]
        assert installed_drivers == []


# ---------------------------------------------------------------------------
# SQL mock behaviour
# ---------------------------------------------------------------------------


class TestSqlMock:
    def test_the_seed_is_applied_and_columns_come_back_in_query_order(
        self, sql_project: RenderedProject
    ) -> None:
        repository = _sql_repository(sql_project)
        spec = get_database_entry("sql").starter_spec
        assert isinstance(spec, SqlSpec)

        assert repository.query(spec.query, {"input": "Acme Corp"}) == [
            {"id": 1, "total": 120.5},
            {"id": 2, "total": 80.0},
        ]

        # Column names follow the *query*, not the table definition.
        swapped = repository.query("SELECT o.total, o.id FROM orders o ORDER BY o.id", {})
        assert list(swapped[0]) == ["total", "id"]
        aliased = repository.query("SELECT id AS order_id FROM orders ORDER BY id", {})
        assert list(aliased[0]) == ["order_id"]

    def test_params_may_be_omitted_entirely(self, sql_project: RenderedProject) -> None:
        repository = _sql_repository(sql_project)
        assert repository.query("SELECT count(*) AS rows_present FROM customers") == [
            {"rows_present": 3}
        ]

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "select 1",
            "  \n SELECT id FROM orders  ",
            "WITH latest AS (SELECT max(id) AS id FROM orders) SELECT id FROM latest",
            "WITH RECURSIVE counter(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM counter"
            " WHERE n < 3) SELECT n FROM counter",
            "-- pick one\nSELECT 1",
            "/* a block comment */ SELECT 1",
            "SELECT ';' AS semicolon_is_not_a_separator",
            "SELECT id FROM orders;",  # one statement, trailing semicolon
        ],
    )
    def test_assert_read_only_accepts_reads(self, sql_project: RenderedProject, sql: str) -> None:
        sql_project.module("sql").assert_read_only(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "",
            "   \n  ",
            "INSERT INTO customers (id, name, city) VALUES (9, 'x', 'y')",
            "UPDATE customers SET name = 'x'",
            "DELETE FROM customers",
            "DROP TABLE customers",
            "CREATE TABLE t (a)",
            "ALTER TABLE customers ADD COLUMN note TEXT",
            "-- a comment in front of a write\nDROP TABLE customers",
            "/* hidden */ DELETE FROM customers",
            "SELECT 1; SELECT 2",
            "SELECT id FROM orders; DROP TABLE orders",
            "PRAGMA query_only = OFF",
        ],
    )
    def test_assert_read_only_rejects_everything_else(
        self, sql_project: RenderedProject, sql: str
    ) -> None:
        with pytest.raises(ValueError, match="read query|query is empty"):
            sql_project.module("sql").assert_read_only(sql)

    def test_the_authorizer_rejects_a_with_insert_the_lexical_check_accepts(
        self, sql_project: RenderedProject
    ) -> None:
        """The plan's central claim, exercised end to end.

        ``WITH x AS (SELECT 1) INSERT INTO ... RETURNING *`` is one statement
        whose head is ``WITH``, so the lexical check accepts it; the SQLite
        authorizer is what refuses it. Without this test the second guard could
        be deleted and every other test here would still pass.
        """
        sql_module = sql_project.module("sql")
        repository = _sql_repository(sql_project)
        hostile = (
            "WITH x AS (SELECT 1 AS one) "
            "INSERT INTO customers (id, name, city) SELECT 99, 'x', 'y' FROM x "
            "RETURNING *"
        )
        sql_module.assert_read_only(hostile)  # the lexical check passes it

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            repository.query(hostile, {})

        # Nothing was written, and both layers were lifted again afterwards.
        assert repository.query("SELECT count(*) AS rows_present FROM customers") == [
            {"rows_present": 3}
        ]
        assert repository.query("SELECT 1 AS one") == [{"one": 1}]

    def test_query_refuses_a_write_and_execute_is_the_declared_path(
        self, sql_project: RenderedProject
    ) -> None:
        repository = _sql_repository(sql_project)
        insert = "INSERT INTO customers (id, name, city) VALUES (:id, :name, :city)"
        with pytest.raises(ValueError, match="must start with SELECT or WITH"):
            repository.query(insert, {"id": 42, "name": "Umbrella", "city": "Oslo"})

        assert repository.execute(insert, {"id": 42, "name": "Umbrella", "city": "Oslo"}) == 1
        assert repository.query("SELECT name FROM customers WHERE id = :id", {"id": 42}) == [
            {"name": "Umbrella"}
        ]

    def test_a_denied_statement_leaves_the_write_path_usable(
        self, sql_project: RenderedProject
    ) -> None:
        repository = _sql_repository(sql_project)
        with pytest.raises(sqlite3.DatabaseError):
            repository.query("WITH x AS (SELECT 1) DELETE FROM customers RETURNING *", {})
        # `query_only` and the authorizer must both have been lifted: a write path
        # still under either would fail here.
        assert repository.execute(
            "INSERT INTO customers (id, name, city) VALUES (:id, :name, :city)",
            {"id": 7, "name": "Initech Two", "city": "Austin"},
        ) == 1

    def test_a_malformed_query_propagates_its_sqlite_error(
        self, sql_project: RenderedProject
    ) -> None:
        with pytest.raises(sqlite3.OperationalError):
            _sql_repository(sql_project).query("SELECT id FROM no_such_table", {})


class TestListParameterExpansion:
    def test_expansion_binds_one_placeholder_per_value(self, sql_project: RenderedProject) -> None:
        portshape = sql_project.module("portshape")
        repository = _sql_repository(sql_project)

        sql, params = portshape.expand_list_param(
            "SELECT name FROM customers WHERE name IN (:input) ORDER BY id",
            ["Globex", "Acme Corp"],
        )
        assert sql == "SELECT name FROM customers WHERE name IN (:input_0, :input_1) ORDER BY id"
        assert params == {"input_0": "Globex", "input_1": "Acme Corp"}
        assert repository.query(sql, params) == [{"name": "Acme Corp"}, {"name": "Globex"}]

    def test_a_single_value_still_expands(self, sql_project: RenderedProject) -> None:
        sql, params = sql_project.module("portshape").expand_list_param(
            "SELECT name FROM customers WHERE name = :input", ["Globex"]
        )
        assert sql == "SELECT name FROM customers WHERE name = :input_0"
        assert params == {"input_0": "Globex"}

    def test_an_empty_list_binds_one_null_placeholder(self, sql_project: RenderedProject) -> None:
        portshape = sql_project.module("portshape")
        sql, params = portshape.expand_list_param(
            "SELECT name FROM customers WHERE name IN (:input)", []
        )
        assert sql == "SELECT name FROM customers WHERE name IN (:input_0)"
        assert params == {"input_0": None}
        # An empty input reads as an empty result set, not as a syntax error.
        assert _sql_repository(sql_project).query(sql, params) == []

    def test_a_query_it_cannot_expand_is_a_named_error(self, sql_project: RenderedProject) -> None:
        portshape = sql_project.module("portshape")
        with pytest.raises(ValueError, match="needs an ':input' placeholder"):
            portshape.expand_list_param("SELECT name FROM customers", ["x"])
        with pytest.raises(ValueError, match="may appear only"):
            portshape.expand_list_param(
                "SELECT name FROM customers WHERE name = :input OR city = :input", ["x", "y"]
            )
        # `:input_0` is not `:input`.
        with pytest.raises(ValueError, match="needs an ':input' placeholder"):
            portshape.expand_list_param("SELECT name FROM customers WHERE name = :input_0", ["x"])


class TestToPsycopg:
    """``to_psycopg`` against a fixed corpus, including its documented limits."""

    @pytest.mark.parametrize(
        ("sqlite_sql", "expected"),
        [
            ("SELECT 1", "SELECT 1"),
            (
                "SELECT id FROM orders WHERE customer_id = :id",
                "SELECT id FROM orders WHERE customer_id = %(id)s",
            ),
            (
                "SELECT id FROM customers WHERE name = :name AND city = :city",
                "SELECT id FROM customers WHERE name = %(name)s AND city = %(city)s",
            ),
            # A literal percent sign MUST be doubled, or psycopg rejects the query.
            (
                "SELECT id FROM customers WHERE name LIKE '%acme%'",
                "SELECT id FROM customers WHERE name LIKE '%%acme%%'",
            ),
            (
                "SELECT id FROM orders WHERE note = '50%' AND id = :id",
                "SELECT id FROM orders WHERE note = '50%%' AND id = %(id)s",
            ),
            # The placeholder the pass inserts is never doubled by the same pass.
            (
                "SELECT id FROM customers WHERE name LIKE :pattern AND city LIKE '%x%'",
                "SELECT id FROM customers WHERE name LIKE %(pattern)s AND city LIKE '%%x%%'",
            ),
            # A `::type` cast is left alone: the second colon is not a placeholder.
            ("SELECT 'x'::text AS value", "SELECT 'x'::text AS value"),
            (
                "SELECT id FROM orders WHERE total::numeric > :floor",
                "SELECT id FROM orders WHERE total::numeric > %(floor)s",
            ),
            (
                "SELECT id FROM orders WHERE total > :floor::numeric",
                "SELECT id FROM orders WHERE total > %(floor)s::numeric",
            ),
            # Documented limitation: a `:name` inside a string literal is
            # rewritten like any other placeholder. psycopg parses the statement
            # itself and leaves the literal text alone, so the right parameters
            # are still bound -- only the literal's spelling changes.
            (
                "SELECT ':not_a_placeholder' AS literal, :real AS value",
                "SELECT '%(not_a_placeholder)s' AS literal, %(real)s AS value",
            ),
            # An already-doubled percent sign is doubled again: this function is
            # for `:name` queries, and the module points at psycopg's own `sql`
            # composition for anything it would have to guess at.
            ("SELECT '100%%' AS pct", "SELECT '100%%%%' AS pct"),
        ],
    )
    def test_rewrites_named_placeholders_and_doubles_percent_signs(
        self, sql_project: RenderedProject, sqlite_sql: str, expected: str
    ) -> None:
        assert sql_project.module("portshape").to_psycopg(sqlite_sql) == expected

    def test_expansion_then_rewrite_is_the_documented_order(
        self, sql_project: RenderedProject
    ) -> None:
        portshape = sql_project.module("portshape")
        expanded, params = portshape.expand_list_param(
            "SELECT name FROM customers WHERE name IN (:input) AND city LIKE '%x%'",
            ["a", "b"],
        )
        assert portshape.to_psycopg(expanded) == (
            "SELECT name FROM customers WHERE name IN (%(input_0)s, %(input_1)s) "
            "AND city LIKE '%%x%%'"
        )
        assert params == {"input_0": "a", "input_1": "b"}


# ---------------------------------------------------------------------------
# The document (NoSQL) mock
# ---------------------------------------------------------------------------


class TestDocumentMock:
    def test_the_seed_documents_load_and_find_filters_them(
        self, nosql_project: RenderedProject
    ) -> None:
        repository = _document_repository(nosql_project)
        assert [document["_id"] for document in repository.find({})] == ["t-1", "t-2", "t-3"]
        assert [document["_id"] for document in repository.find({"status": "open"})] == [
            "t-1",
            "t-2",
        ]
        assert repository.count() == 3
        assert repository.count({"status": "open"}) == 2
        assert repository.find_one({"status": "closed"})["_id"] == "t-3"
        assert repository.find_one({"status": "nope"}) is None

    @pytest.mark.parametrize(
        ("document_filter", "expected_ids"),
        [
            ({"status": {"$eq": "open"}}, ["t-1", "t-2"]),
            ({"status": {"$ne": "open"}}, ["t-3"]),
            ({"priority": {"$gt": 2}}, ["t-3"]),
            ({"priority": {"$gte": 2}}, ["t-1", "t-3"]),
            ({"priority": {"$lt": 2}}, ["t-2"]),
            ({"priority": {"$lte": 2}}, ["t-1", "t-2"]),
            ({"status": {"$in": ["open", "closed"]}}, ["t-1", "t-2", "t-3"]),
            ({"status": {"$in": []}}, []),
            ({"subject": {"$regex": "^Invoice"}}, ["t-2"]),
            ({"tags": {"$in": ["billing"]}}, ["t-2"]),
            ({"$and": [{"status": "open"}, {"priority": {"$gte": 2}}]}, ["t-1"]),
            ({"$or": [{"status": "closed"}, {"priority": 1}]}, ["t-2", "t-3"]),
            (
                {"$and": [{"status": "open"}, {"$or": [{"priority": 1}, {"priority": 2}]}]},
                ["t-1", "t-2"],
            ),
            ({"_id": {"$exists": True}, "status": "open"}, ["t-1", "t-2"]),
            ({"owner": {"$exists": False}}, ["t-1", "t-2", "t-3"]),
            # A missing field equals nothing, and `$ne` therefore matches it.
            ({"owner": {"$ne": "ops"}}, ["t-1", "t-2", "t-3"]),
            # An incomparable pair matches nothing rather than raising.
            ({"priority": {"$gt": "not a number"}}, []),
            # A sub-document with no operators is an exact-equality comparison.
            ({"tags": ["billing"]}, ["t-2"]),
        ],
    )
    def test_every_supported_operator(
        self,
        nosql_project: RenderedProject,
        document_filter: dict[str, Any],
        expected_ids: list[str],
    ) -> None:
        repository = _document_repository(nosql_project)
        assert [document["_id"] for document in repository.find(document_filter)] == expected_ids

    def test_equality_against_an_array_matches_a_contained_value(
        self, nosql_project: RenderedProject
    ) -> None:
        """Mongo semantics: ``{"tags": "auth"}`` matches a document *containing* it.

        Without this the mock would return nothing for a filter the live database
        answers, which is the divergence the whole two-implementation design is
        meant to avoid.
        """
        repository = _document_repository(nosql_project)
        assert [document["_id"] for document in repository.find({"tags": "auth"})] == ["t-1"]

    def test_dotted_paths_reach_into_nested_documents(
        self, nosql_project: RenderedProject
    ) -> None:
        repository = _document_repository(nosql_project)
        nested = {"_id": "t-9", "status": "open", "owner": {"team": "ops", "lead": None}}
        assert repository.insert_one(nested) == "t-9"
        assert [document["_id"] for document in repository.find({"owner.team": "ops"})] == ["t-9"]
        assert repository.count({"owner.team": "ops"}) == 1
        assert repository.find_one({"owner.team": "support"}) is None
        # A stored None is present, so `$exists` and `$eq: None` must not be
        # confused with a missing field.
        assert [d["_id"] for d in repository.find({"owner.lead": {"$exists": True}})] == ["t-9"]
        assert [d["_id"] for d in repository.find({"owner.lead": {"$eq": None}})] == ["t-9"]

    @pytest.mark.parametrize(
        "document_filter",
        [
            {"status": {"$near": "x"}},
            {"priority": {"$size": 2}},
            {"$nor": [{"status": "open"}]},
            {"$where": "this.priority > 1"},
            {"priority": {"$not": {"$gt": 1}}},
            {"$and": [{"tags": {"$elemMatch": {"$eq": "auth"}}}]},
        ],
    )
    def test_an_unsupported_operator_raises_naming_it(
        self, nosql_project: RenderedProject, document_filter: dict[str, Any]
    ) -> None:
        repository = _document_repository(nosql_project)
        with pytest.raises(NotImplementedError, match=r"\$[a-z]+"):
            repository.find(document_filter)
        with pytest.raises(NotImplementedError):
            repository.count(document_filter)

    def test_a_logical_operator_needs_a_list_of_filters(
        self, nosql_project: RenderedProject
    ) -> None:
        repository = _document_repository(nosql_project)
        with pytest.raises(TypeError, match=r"\$and needs a list of filters"):
            repository.find({"$and": {"status": "open"}})

    def test_limit_bounds_the_result_and_zero_means_no_limit(
        self, nosql_project: RenderedProject
    ) -> None:
        repository = _document_repository(nosql_project)
        assert len(repository.find({"status": "open"}, limit=1)) == 1
        assert len(repository.find({"status": "open"}, limit=0)) == 2
        assert len(repository.find({"status": "open"}, limit=-1)) == 2

    def test_insert_one_is_ephemeral_and_visible_to_later_reads(
        self, nosql_project: RenderedProject, tmp_path: Path
    ) -> None:
        repository = _document_repository(nosql_project)
        assert repository.insert_one({"status": "open", "subject": "New"}) == "doc-3"
        assert repository.count({"status": "open"}) == 3
        # A second instance over the same seed is a *new* in-memory collection:
        # persistence is what live mode is for, and the README says so.
        fresh = nosql_project.module("nosql").InMemoryDocumentRepository(
            nosql_project.seed_path("tickets", "json")
        )
        assert fresh.count({"status": "open"}) == 2

    def test_a_seed_that_is_not_an_array_of_objects_is_a_named_error(
        self, nosql_project: RenderedProject, tmp_path: Path
    ) -> None:
        broken = tmp_path / "broken.json"
        broken.write_text(json.dumps({"status": "open"}))
        with pytest.raises(ValueError, match="must hold a JSON array of documents"):
            nosql_project.module("nosql").InMemoryDocumentRepository(broken)


# ---------------------------------------------------------------------------
# The vector mock
# ---------------------------------------------------------------------------


class TestHashEmbedder:
    def test_the_vector_is_fixed_width_and_l2_normalized(
        self, vector_project: RenderedProject
    ) -> None:
        embedding = vector_project.module("embedding")
        vector = embedding.HashEmbedder().embed("Reset the router")
        assert len(vector) == embedding.DIMENSIONS == 256
        assert sum(component * component for component in vector) == pytest.approx(1.0)

    def test_it_is_a_pure_function_of_the_text(self, vector_project: RenderedProject) -> None:
        embedder = vector_project.module("embedding").HashEmbedder()
        assert embedder.embed("Reset the router") == embedder.embed("Reset the router")
        # Normalization is only lower-casing plus whitespace collapsing, so two
        # spellings of the same text embed identically.
        assert embedder.embed("Reset  the\nrouter") == embedder.embed("reset the router")

    def test_text_with_no_characters_still_embeds(self, vector_project: RenderedProject) -> None:
        embedder = vector_project.module("embedding").HashEmbedder()
        assert embedder.embed("   ") == [0.0] * 256

    def test_it_is_deterministic_across_processes(self, vector_project: RenderedProject) -> None:
        """Two interpreters with different hash seeds must agree.

        This is the property that matters and the one a naive implementation
        loses: ``hash()`` is salted per process, so an embedder built on it would
        give two runs of one graph different orderings.
        """
        script = (
            "import json, sys\n"
            f"sys.path.insert(0, {str(vector_project.root)!r})\n"
            f"from {vector_project.package}.repositories.embedding import HashEmbedder\n"
            "print(json.dumps(HashEmbedder().embed('Reset the router')))\n"
        )
        outputs = []
        for hash_seed in ("1", "4242"):
            completed = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, "PYTHONHASHSEED": hash_seed},
            )
            outputs.append(json.loads(completed.stdout))
        assert outputs[0] == outputs[1]
        assert len(outputs[0]) == 256

    def test_a_non_positive_dimension_is_rejected(self, vector_project: RenderedProject) -> None:
        embedder_class = vector_project.module("embedding").HashEmbedder
        with pytest.raises(ValueError, match="dimensions must be positive"):
            embedder_class(dimensions=0)
        with pytest.raises(ValueError, match="ngram_size must be positive"):
            embedder_class(ngram_size=0)


class TestVectorMock:
    def test_search_ranks_the_seed_documents_by_similarity(
        self, vector_project: RenderedProject
    ) -> None:
        hits = _vector_repository(vector_project).search("how do I reset the router", top_k=4)
        assert hits[0]["id"] == "doc-2"
        scores = [hit["score"] for hit in hits]
        assert scores == sorted(scores, reverse=True)
        assert set(hits[0]) == {"id", "text", "metadata", "score"}
        assert hits[0]["text"].startswith("To reset the Nimbus Router X1")
        assert hits[0]["metadata"]["product"] == "Nimbus Router X1"
        assert all(-1.0 <= score <= 1.0 for score in scores)

    def test_ties_break_on_the_document_id(
        self, vector_project: RenderedProject, tmp_path: Path
    ) -> None:
        """Equal scores must come out in a stable, declared order.

        Insertion order would make the result depend on something the document
        does not declare, and two runs of one graph could disagree.
        """
        seed = tmp_path / "tie.json"
        seed.write_text(
            json.dumps(
                [
                    {"id": "b-doc", "text": "identical text", "metadata": {}},
                    {"id": "a-doc", "text": "identical text", "metadata": {}},
                ]
            )
        )
        repository = vector_project.module("vector").InMemoryVectorRepository(seed)
        hits = repository.search("identical text", top_k=4)
        assert [hit["id"] for hit in hits] == ["a-doc", "b-doc"]
        assert hits[0]["score"] == hits[1]["score"]

    def test_top_k_and_min_score_bound_the_result(self, vector_project: RenderedProject) -> None:
        repository = _vector_repository(vector_project)
        # `min_score=-1.0` admits cosine similarity's whole range, so the counts
        # below are about top_k alone.
        assert len(repository.search("router", top_k=2, min_score=-1.0)) == 2
        # `top_k <= 0` means no limit, mirroring the document repository's limit.
        assert len(repository.search("router", top_k=0, min_score=-1.0)) == 4
        assert repository.search("router", top_k=4, min_score=0.99) == []

    def test_upsert_replaces_by_id_and_returns_the_count(
        self, vector_project: RenderedProject
    ) -> None:
        repository = _vector_repository(vector_project)
        assert repository.upsert([{"id": "doc-1", "text": "an entirely different sentence"}]) == 1
        hits = repository.search("an entirely different sentence", top_k=4, min_score=-1.0)
        assert len(hits) == 4  # replaced, not appended
        assert hits[0]["id"] == "doc-1"
        with pytest.raises(ValueError, match="needs a non-empty 'id' string"):
            repository.upsert([{"text": "no id"}])
        with pytest.raises(ValueError, match="needs a 'text' string"):
            repository.upsert([{"id": "doc-5"}])
        with pytest.raises(ValueError, match="non-object 'metadata'"):
            repository.upsert([{"id": "doc-5", "text": "x", "metadata": "nope"}])


# ---------------------------------------------------------------------------
# The live vector credential (the rendered adapter, against a fake driver)
# ---------------------------------------------------------------------------


class _RecordingQdrantClient:
    """A recording stand-in for ``qdrant_client.QdrantClient``.

    qdrant-client is an opt-in extra that is never installed in this suite -- and
    this suite never opens a socket -- so the live vector constructor is
    otherwise unreachable. What has to be proved is the arguments the rendered
    adapter hands the driver, which is the one thing the template controls, so
    the fake records them and answers just enough to construct.
    """

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        _RecordingQdrantClient.last_kwargs = kwargs

    def collection_exists(self, collection: str) -> bool:
        """Every collection exists: the adapter's own check is not under test."""
        return True

    def search(self, **kwargs: Any) -> list[Any]:
        """No hits, so a constructed adapter can still be exercised."""
        return []


@pytest.fixture
def fake_qdrant_driver(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingQdrantClient]:
    """Install fake ``qdrant_client`` modules for one test, and take them away.

    Injected into ``sys.modules`` rather than installed, because qdrant-client
    must stay absent from every other test in this module: the keyless-gate test
    asserts that importing the rendered layer pulls no driver in.
    """
    client_module = ModuleType("qdrant_client")
    client_module.QdrantClient = _RecordingQdrantClient
    http_module = ModuleType("qdrant_client.http")
    http_module.models = ModuleType("qdrant_client.http.models")
    monkeypatch.setitem(sys.modules, "qdrant_client", client_module)
    monkeypatch.setitem(sys.modules, "qdrant_client.http", http_module)
    return _RecordingQdrantClient


class TestVectorLiveCredentials:
    """``SWARM_VECTOR_API_KEY``: what a hosted endpoint needs, and nothing more.

    The generated project promises that going live costs one mode variable, that
    engine's DSN and that engine's extra. A Qdrant Cloud endpoint also needs a
    key, so the adapter has to read one -- without turning it into a second
    *required* variable, because a local Qdrant answers unauthenticated.
    """

    def test_the_api_key_reaches_the_client_when_it_is_set(
        self,
        vector_project: RenderedProject,
        fake_qdrant_driver: type[_RecordingQdrantClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SWARM_VECTOR_API_KEY", "  a-cloud-api-key  ")
        repository = vector_project.module("vector").QdrantRepository(
            "https://example.cloud.qdrant.io", "product_docs"
        )
        assert fake_qdrant_driver.last_kwargs == {
            "url": "https://example.cloud.qdrant.io",
            "api_key": "a-cloud-api-key",
        }
        assert repository.search("router") == []

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_an_unset_empty_or_blank_key_stays_valid(
        self,
        vector_project: RenderedProject,
        fake_qdrant_driver: type[_RecordingQdrantClient],
        monkeypatch: pytest.MonkeyPatch,
        value: str | None,
    ) -> None:
        """Unset, empty and blank all mean "no key".

        ``api_key=None`` is the client's own default, so the local case -- the
        common development case, and what the generated ``.env.example`` ships --
        constructs exactly as it did before the key existed.
        """
        if value is None:
            monkeypatch.delenv("SWARM_VECTOR_API_KEY", raising=False)
        else:
            monkeypatch.setenv("SWARM_VECTOR_API_KEY", value)
        vector_module = vector_project.module("vector")
        vector_module.QdrantRepository("http://localhost:6333", "product_docs")
        assert fake_qdrant_driver.last_kwargs == {
            "url": "http://localhost:6333",
            "api_key": None,
        }
        assert vector_module.api_key_from_env() is None

    def test_the_rendered_adapter_owns_the_credential_and_reads_it_as_optional(
        self, vector_project: RenderedProject
    ) -> None:
        """The template is what runs, so the structural claims are made on it.

        The key is read with ``.get`` -- a key is optional, and only the DSN is
        required to go live -- and it is the *adapter* that reads it, so no
        secret is threaded through a node's declared fields.
        """
        source = vector_project.source("vector")
        assert '_API_KEY_ENV_VAR = "SWARM_VECTOR_API_KEY"' in source
        assert "api_key=api_key_from_env()" in source
        assert 'os.environ["SWARM_VECTOR_API_KEY"]' not in source

        # And the module docstring still names both the variable and the extra,
        # which is all a reader of the generated project gets.
        docstring = ast.get_docstring(ast.parse(source)) or ""
        assert "SWARM_VECTOR_API_KEY" in docstring
        assert "SWARM_VECTOR_DSN" in docstring
        assert "uv sync --extra live-vector" in docstring


# ---------------------------------------------------------------------------
# as_port and the factory
# ---------------------------------------------------------------------------


class TestAsPort:
    def test_str_coerces_anything_into_text(self, sql_project: RenderedProject) -> None:
        as_port = sql_project.module("portshape").as_port
        assert as_port("already text", "str") == "already text"
        assert as_port({"a": 1}, "str") == '{"a": 1}'
        assert as_port([1, 2], "str") == "[1, 2]"
        assert as_port(None, "str") == "null"
        assert as_port(42, "str") == "42"

    def test_json_takes_a_mapping_or_a_json_object_string(
        self, sql_project: RenderedProject
    ) -> None:
        as_port = sql_project.module("portshape").as_port
        assert as_port({"a": 1}, "json") == {"a": 1}
        assert as_port('{"a": 1}', "json") == {"a": 1}
        with pytest.raises(TypeError, match="needs a JSON object"):
            as_port("[1, 2]", "json")
        with pytest.raises(TypeError, match="needs a mapping"):
            as_port([1, 2], "json")
        with pytest.raises(TypeError, match="could not parse the value as JSON"):
            as_port("not json at all", "json")

    def test_list_str_never_iterates_a_bare_string(self, sql_project: RenderedProject) -> None:
        as_port = sql_project.module("portshape").as_port
        assert as_port(["a", 2], "list[str]") == ["a", "2"]
        # The whole point: "abc" must not become ["a", "b", "c"].
        assert as_port("abc", "list[str]") == ["abc"]
        assert as_port(None, "list[str]") == []
        assert as_port(7, "list[str]") == ["7"]

    def test_list_json_returns_rows_and_makes_them_serializable(
        self, sql_project: RenderedProject
    ) -> None:
        as_port = sql_project.module("portshape").as_port
        assert as_port([{"a": 1}], "list[json]") == [{"a": 1}]
        assert as_port({"a": 1}, "list[json]") == [{"a": 1}]
        assert as_port(None, "list[json]") == []
        assert as_port('[{"a": 1}]', "list[json]") == [{"a": 1}]
        # A blobby leaf is decoded rather than left to break the edge's json.dumps.
        assert as_port([{"blob": b"bytes"}], "list[json]") == [{"blob": "bytes"}]
        # Keys are strings, so a row is a JSON object either way.
        assert as_port([{1: "one"}], "list[json]") == [{"1": "one"}]
        with pytest.raises(TypeError, match="must be a mapping"):
            as_port([42], "list[json]")
        # A string row must be the JSON object text of one; malformed text is
        # reported as the same TypeError, not as a bare JSONDecodeError.
        with pytest.raises(TypeError, match="could not parse the value as JSON"):
            as_port(["not a row"], "list[json]")
        with pytest.raises(TypeError, match="must be a mapping"):
            as_port('[[{"not": "a row"}]]', "list[json]")
        with pytest.raises(TypeError, match="needs a JSON array"):
            as_port('{"a": 1}', "list[json]")

    def test_an_unknown_port_type_names_the_known_ones(self, sql_project: RenderedProject) -> None:
        with pytest.raises(ValueError, match="unknown port type") as failure:
            sql_project.module("portshape").as_port("x", "json[]")
        assert "'json[]'" in str(failure.value)
        assert "list[json]" in str(failure.value)


class TestFactorySwitch:
    def test_db_mode_defaults_to_mock_and_never_falls_back(
        self, sql_project: RenderedProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_mode = sql_project.repositories().db_mode
        monkeypatch.delenv("SWARM_DB_MODE", raising=False)
        assert db_mode() == "mock"
        for value in ("", "   ", "mock", "MOCK", " Mock "):
            monkeypatch.setenv("SWARM_DB_MODE", value)
            assert db_mode() == "mock"
        for value in ("live", "LIVE", " Live "):
            monkeypatch.setenv("SWARM_DB_MODE", value)
            assert db_mode() == "live"

        monkeypatch.setenv("SWARM_DB_MODE", "life")
        with pytest.raises(ValueError, match="'life' is not a supported mode"):
            db_mode()
        with pytest.raises(ValueError, match="use 'mock' or 'live'"):
            db_mode()

    def test_live_mode_without_a_dsn_names_the_missing_variable(
        self,
        sql_project: RenderedProject,
        nosql_project: RenderedProject,
        vector_project: RenderedProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SWARM_DB_MODE", "live")
        for variable in ("SWARM_SQL_DSN", "SWARM_NOSQL_DSN", "SWARM_VECTOR_DSN"):
            monkeypatch.delenv(variable, raising=False)

        with pytest.raises(KeyError, match="SWARM_SQL_DSN"):
            _sql_repository(sql_project)
        with pytest.raises(KeyError, match="SWARM_NOSQL_DSN"):
            _document_repository(nosql_project)

        # The vector getter resolves its URL first, then the collection, so the
        # fallback for an omitted collection names its own variable.
        monkeypatch.setenv("SWARM_VECTOR_DSN", "http://localhost:6333")
        monkeypatch.delenv("SWARM_VECTOR_COLLECTION", raising=False)
        with pytest.raises(KeyError, match="SWARM_VECTOR_COLLECTION"):
            _vector_repository(vector_project)

    def test_live_vector_requires_the_dsn_and_never_the_api_key(
        self,
        vector_project: RenderedProject,
        fake_qdrant_driver: type[_RecordingQdrantClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Going live is the mode, the DSN and the extra -- not a credential.

        With a hosted endpoint the DSN alone would connect to nothing, so the key
        must reach the client; but a local Qdrant answers unauthenticated, so the
        key must never become a *required* variable beside the DSN. Here the
        factory resolves live mode with the key unset and hands the adapter
        ``None``, which is the client's own default.
        """
        monkeypatch.setenv("SWARM_DB_MODE", "live")
        monkeypatch.setenv("SWARM_VECTOR_DSN", "http://localhost:6333")
        monkeypatch.setenv("SWARM_VECTOR_COLLECTION", "product_docs")
        monkeypatch.delenv("SWARM_VECTOR_API_KEY", raising=False)

        _vector_repository(vector_project)
        assert fake_qdrant_driver.last_kwargs == {
            "url": "http://localhost:6333",
            "api_key": None,
        }

    def test_two_nodes_over_the_same_seed_share_one_instance(
        self, tmp_path: Path
    ) -> None:
        project = _render(tmp_path / "same_seed", two_sql_nodes_same_seed_graph())
        first = project.repositories().get_sql_repository(project.seed_path("orders_db", "sql"))
        second = project.repositories().get_sql_repository(
            project.seed_path("orders_db_again", "sql")
        )
        assert first is second

        # And the sharing is observable: a write through one node's handle is a
        # read through the other's.
        first.execute(
            "INSERT INTO customers (id, name, city) VALUES (:id, :name, :city)",
            {"id": 11, "name": "Shared", "city": "Lisbon"},
        )
        assert second.query("SELECT name FROM customers WHERE id = :id", {"id": 11}) == [
            {"name": "Shared"}
        ]

    def test_two_nodes_over_different_seeds_get_independent_instances(
        self, tmp_path: Path
    ) -> None:
        """Neither node's declared seed may be ignored.

        A process-wide "first constructor wins" cache would hand both nodes the
        first seed's data, which is a silently wrong answer rather than an error.
        """
        project = _render(
            tmp_path / "different_seed", two_sql_nodes_different_seed_graph()
        )
        first = project.repositories().get_sql_repository(project.seed_path("orders_db", "sql"))
        second = project.repositories().get_sql_repository(
            project.seed_path("orders_db_again", "sql")
        )
        assert first is not second
        assert first.query("SELECT count(*) AS n FROM customers") == [{"n": 3}]
        assert second.query("SELECT count(*) AS n FROM customers") == [{"n": 4}]

    def test_the_mock_ignores_the_collection_because_a_mock_instance_is_one(
        self, vector_project: RenderedProject, nosql_project: RenderedProject
    ) -> None:
        """Documented behaviour, pinned so it cannot change silently.

        In mock mode a repository instance *is* its single collection, keyed by
        the seed, so the collection a step passes cannot change which mock comes
        back. Live mode honours it, because there the wrong collection is a wrong
        answer rather than the same data under another name.
        """
        vector_seed = vector_project.seed_path("product_docs", "json")
        assert vector_project.repositories().get_vector_repository(
            vector_seed, "product_docs"
        ) is vector_project.repositories().get_vector_repository(vector_seed, "something_else")

        document_seed = nosql_project.seed_path("tickets", "json")
        assert nosql_project.repositories().get_document_repository(
            document_seed, "tickets"
        ) is nosql_project.repositories().get_document_repository(document_seed, "elsewhere")

    def test_seed_key_is_a_digest_of_the_content_not_the_path(
        self, sql_project: RenderedProject, tmp_path: Path
    ) -> None:
        seed_key = sql_project.repositories().seed_key
        first = tmp_path / "one.sql"
        second = tmp_path / "two.sql"
        first.write_text("SELECT 1;\n")
        second.write_text("SELECT 1;\n")
        third = tmp_path / "three.sql"
        third.write_text("SELECT 2;\n")

        assert seed_key(first) == seed_key(second)
        assert seed_key(first) != seed_key(third)
        assert re.fullmatch(r"[0-9a-f]{32}", seed_key(first))

    def test_the_factory_reexports_the_port_helpers(self, sql_project: RenderedProject) -> None:
        """A step body may import them from either place.

        Both spellings are exercised, because both are emitted in generated
        projects and a missing re-export would be a NameError at import time.
        """
        repositories = sql_project.repositories()
        assert repositories.as_port([{"a": 1}], "list[json]") == [{"a": 1}]
        assert repositories.to_psycopg("SELECT :a") == "SELECT %(a)s"
        assert repositories.expand_list_param("SELECT :input", ["x"]) == (
            "SELECT :input_0",
            {"input_0": "x"},
        )


# ---------------------------------------------------------------------------
# All three kinds in one generated project
# ---------------------------------------------------------------------------


class TestAllThreeKindsTogether:
    def test_one_factory_serves_every_used_kind(self, tmp_path: Path) -> None:
        """The assembled factory defines one getter per used kind.

        This is the case a per-kind unit test cannot reach: three kinds' import
        blocks and getters land in one module, so a name collision, a shadowed
        helper or a duplicated import only shows up here.
        """
        graph = sql_lookup_graph()
        merged = graph.model_copy(
            update={
                "id": "all-three-kinds",
                "nodes": [
                    *graph.nodes,
                    *nosql_query_graph().nodes,
                    *vector_search_graph().nodes,
                ],
            }
        )
        project = _render(tmp_path / "all_kinds", merged)
        repositories = project.repositories()

        assert repositories.get_sql_repository(project.seed_path("orders_db", "sql")).query(
            "SELECT count(*) AS n FROM orders"
        ) == [{"n": 3}]
        assert (
            repositories.get_document_repository(
                project.seed_path("tickets", "json"), "tickets"
            ).count()
            == 3
        )
        hits = repositories.get_vector_repository(
            project.seed_path("product_docs", "json"), "product_docs"
        ).search("how do I reset the router")
        assert hits[0]["id"] == "doc-2"


# ---------------------------------------------------------------------------
# The "$input" sentinel binding (one rule, shared by both compile targets)
# ---------------------------------------------------------------------------


class TestFilterBinding:
    """``bind_input_filter``, which both emitters call on a NoSQL node's filter.

    This is the rule the two targets used to implement separately, and the reason
    it lives in the generated repository layer: an inlined, top-level-only
    substitution matched nothing in an ``$in`` value position, so the *same*
    document read its rows in one export and silently returned ``[]`` in the other.
    """

    def test_a_top_level_sentinel_takes_the_input(self, nosql_project: RenderedProject) -> None:
        bind = nosql_project.module("portshape").bind_input_filter

        assert bind({"status": "$input"}, "open") == {"status": "open"}

    def test_a_nested_sentinel_takes_the_input(self, nosql_project: RenderedProject) -> None:
        """The shape an ``$in`` needs, and the one that silently matched nothing."""
        bind = nosql_project.module("portshape").bind_input_filter

        assert bind({"status": {"$in": "$input"}}, ["open", "closed"]) == {
            "status": {"$in": ["open", "closed"]}
        }

    def test_a_sentinel_inside_a_list_is_reached(self, nosql_project: RenderedProject) -> None:
        bind = nosql_project.module("portshape").bind_input_filter

        assert bind({"$or": [{"status": "$input"}, {"priority": 1}]}, "open") == {
            "$or": [{"status": "open"}, {"priority": 1}]
        }

    def test_a_filter_without_a_sentinel_is_unchanged(self, nosql_project: RenderedProject) -> None:
        bind = nosql_project.module("portshape").bind_input_filter
        declared = {"status": "open", "priority": {"$gte": 2}}

        assert bind(declared, "ignored") == declared

    def test_a_sentinel_substring_is_not_a_sentinel(self, nosql_project: RenderedProject) -> None:
        """Only the exact string is the sentinel -- ``"$inputs"`` is a literal."""
        bind = nosql_project.module("portshape").bind_input_filter

        assert bind({"status": "$inputs"}, "open") == {"status": "$inputs"}

    def test_the_declared_sentinel_constant_is_the_documented_spelling(
        self, nosql_project: RenderedProject
    ) -> None:
        """The template escapes a literal ``$`` as ``$$``; this pins the rendering."""
        assert nosql_project.module("portshape").INPUT_SENTINEL == "$input"
