"""Database-node emission: the one place repository files, seeds, extras, env
lines and README text are rendered.

**Why this module exists instead of each emitter doing its own rendering.** The
PydanticAI target (``compile/scaffold.py``) and the LangGraph target
(``compile/langgraph/scaffold.py``) both have to place the same repository layer,
gate it on the same "does this document use a database node at all" question and
apply the same substitutions. Two copies would drift, and the drift would be
invisible: each target would keep passing its own tests while the two exports
stop matching each other.

**The contract between this module and the two emitters** (see the plan's
interface section):

- The functions here return *content and relative paths only*. Neither emitter
  is imported from here, and this module never writes a file: each emitter places
  what it is handed under its own package directory.
- ``package`` is the generated package's import name -- ``"swarm_workflow"`` for
  the PydanticAI target, ``"swarm_workflow_lg"`` for the LangGraph one. The same
  templates render both; there is never a second copy of the adapter code.
- Template substitution is ``string.Template`` and the only variables are
  ``$package`` plus, in ``factory.py.tmpl``, ``$kind_imports`` and
  ``$kind_getters``. Seed paths are **runtime arguments** passed by the emitted
  step, never baked into a template -- a baked path would make the mock's data
  depend on the compile, not on the document.
- Everything is gated on :func:`used_db_kinds`. With no database node in the
  document, every function here returns empty output, so the emitted project is
  byte-identical to what it was before database nodes existed.

The rendered modules import nothing third-party at import time: the live drivers
are imported inside their own constructors, so ``import <package>.graph`` (the
keyless gate's second step) succeeds with nothing installed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from string import Template

from swarm_builder.models import NosqlSpec, SqlSpec, SwarmGraph, SwarmNode, VectorSpec
from swarm_builder.templates.database import (
    KIND_GETTERS_MARKER,
    KIND_IMPORTS_MARKER,
    KIND_ORDER,
    get_database_entry,
    read_template,
)

#: The node kinds that have a repository layer. Everything this module emits is
#: gated on the intersection of a document's kinds with this set.
DATABASE_KINDS: frozenset[str] = frozenset(KIND_ORDER)

#: kind -> the seed file's extension. SQL seeds are SQL text; the document and
#: vector mocks both load a JSON array of objects.
SEED_SUFFIX: dict[str, str] = {"sql": "sql", "nosql": "json", "vector": "json"}

#: kind -> the mock class the generated README names for it. Duplicated from the
#: templates *on purpose*, because the README has to be readable without opening
#: them; a test asserts each name is really defined in its rendered module, so
#: the two cannot drift apart silently.
_MOCK_CLASSES: dict[str, str] = {
    "sql": "SqliteRepository",
    "nosql": "InMemoryDocumentRepository",
    "vector": "InMemoryVectorRepository",
}

#: kind -> the live adapter class the README points the user at.
_LIVE_CLASSES: dict[str, str] = {
    "sql": "PostgresRepository",
    "nosql": "MongoRepository",
    "vector": "QdrantRepository",
}

#: The example value each engine's variables get in ``.env.example``. A test
#: asserts this map's keys are exactly the catalog's ``env_vars``, so adding a
#: variable to the catalog cannot leave the example file without a value.
_ENV_EXAMPLE_VALUES: dict[str, str] = {
    "SWARM_SQL_DSN": "postgresql://postgres:postgres@localhost:5432/swarm_workflow",
    "SWARM_NOSQL_DSN": "mongodb://localhost:27017/swarm_workflow",
    "SWARM_NOSQL_COLLECTION": "swarm_documents",
    "SWARM_VECTOR_DSN": "http://localhost:6333",
    # Empty on purpose: only a hosted (Qdrant Cloud) endpoint needs a key, and a
    # local Qdrant is the common development target. The rendered adapter's
    # ``api_key_from_env()`` reads an empty or blank value as "no key", so an
    # empty example value is the honest rendering -- a commented-out line would
    # be a second way to say the same thing.
    "SWARM_VECTOR_API_KEY": "",
    "SWARM_VECTOR_COLLECTION": "swarm_documents",
}


def used_db_kinds(graph: SwarmGraph) -> frozenset[str]:
    """The database kinds this document actually uses.

    Empty means "emit nothing new": no ``repositories/`` file, no seed file, no
    optional dependency, no environment line, no README section. That single gate
    is what keeps a document without database nodes emitting the same tree it did
    before database nodes existed.

    Args:
        graph: The document being emitted.

    Returns:
        A subset of :data:`DATABASE_KINDS`.
    """
    return frozenset(node.kind for node in graph.nodes) & DATABASE_KINDS


def database_files_for(graph: SwarmGraph, package: str) -> dict[str, str]:
    """Render every repository and seed file this document's database nodes need.

    Keys are paths **relative to the generated package directory**
    (``src/<package>/``), not to the project root: each emitter places them under
    its own package, and ``package`` is also the name the rendered modules import
    themselves by.

    Args:
        graph: The document being emitted.
        package: The generated package's import name.

    Returns:
        ``{relative path: rendered content}`` in a fixed order -- the factory and
        the port/row helpers, then each used kind's adapter (``embedding.py``
        ahead of ``vector.py``), then one seed file per database node. Empty when
        the document has no database node.
    """
    kinds = used_db_kinds(graph)
    if not kinds:
        return {}

    files: dict[str, str] = {
        "repositories/__init__.py": _render_factory(package, kinds),
        "repositories/portshape.py": _render("portshape.py.tmpl", package),
    }
    for kind in KIND_ORDER:
        if kind not in kinds:
            continue
        module = get_database_entry(kind).package_module
        if kind == "vector":
            # Before vector.py, which imports it: a reader walking the emitted
            # tree in order sees the embedder defined before it is used.
            files["repositories/embedding.py"] = _render("embedding.py.tmpl", package)
        files[f"repositories/{module}.py"] = _render(f"{kind}/repository.py.tmpl", package)
    files.update(_render_seed_files(graph))
    return files


def database_extra_lines(graph: SwarmGraph) -> tuple[str, ...]:
    """The ``[project.optional-dependencies]`` *body* for the used kinds.

    One line per used kind, e.g. ``live-sql = ["psycopg[binary]>=3.2"]``. The
    table header is the caller's: this returns the body only, and an empty tuple
    when the document has no database node, so a project without one never grows
    an empty table.

    Args:
        graph: The document being emitted.

    Returns:
        The lines to append inside ``[project.optional-dependencies]``.
    """
    kinds = used_db_kinds(graph)
    if not kinds:
        return ()
    lines: list[str] = []
    for kind in KIND_ORDER:
        if kind not in kinds:
            continue
        entry = get_database_entry(kind)
        requirements = ", ".join(f'"{dependency}"' for dependency in entry.live_extra_deps)
        lines.append(f"{entry.live_extra_name} = [{requirements}]")
    return tuple(lines)


def database_env_lines(graph: SwarmGraph) -> tuple[str, ...]:
    """The ``.env.example`` lines for the used kinds (empty tuple when none).

    ``SWARM_DB_MODE`` first, then only the *used* engines' variables -- never
    another engine's DSN, which would suggest a driver this graph's steps cannot
    reach. The caller appends these to the model lines; nothing here replaces
    them.

    Args:
        graph: The document being emitted.

    Returns:
        The lines to append to ``.env.example``.
    """
    kinds = used_db_kinds(graph)
    if not kinds:
        return ()
    lines = [
        "# Database nodes: leave SWARM_DB_MODE unset (or set it to mock) to run the",
        "# seeded in-memory mock. Set it to live and fill in the values below to use a",
        '# real database; see the "Database nodes" section of this README.',
        "SWARM_DB_MODE=mock",
    ]
    for kind in KIND_ORDER:
        if kind not in kinds:
            continue
        for variable in get_database_entry(kind).env_vars:
            lines.append(f"{variable}={_ENV_EXAMPLE_VALUES[variable]}")
    return tuple(lines)


def database_readme_section(graph: SwarmGraph) -> str:
    """The generated README's "Database nodes" section (``""`` when none).

    Names the three things a generated document section usually leaves out: the
    exact file to edit to change a live adapter, every environment variable the
    used engines read, and the exact ``uv sync --extra live-<kind>`` command(s)
    for the kinds this graph uses. Paths are written relative to the generated
    package (``repositories/sql.py``) because both targets share this text and
    they have different package names.

    Args:
        graph: The document being emitted.

    Returns:
        The section, or ``""`` when the document has no database node.
    """
    kinds = used_db_kinds(graph)
    if not kinds:
        return ""
    entries = [get_database_entry(kind) for kind in KIND_ORDER if kind in kinds]

    lines: list[str] = [
        "## Database nodes",
        "",
        "Every database node in this workflow runs against a seeded in-memory mock by",
        "default: no credentials, no server, no driver installed. `SWARM_DB_MODE`",
        "switches all of them at once.",
        "",
        "| `SWARM_DB_MODE` | Behaviour |",
        "| --- | --- |",
        "| unset, or `mock` | the seeded in-memory mock (what the validation gate runs) |",
        "| `live` | the live engines below, using the variables below |",
        "| anything else | an error naming the accepted values -- never a silent",
        "fallback to the mock |",
        "",
        "### Seeded mock data",
        "",
        "The mock is in-memory and ephemeral: it is rebuilt from each node's seed on",
        "every run, so a write in one step is visible to the steps after it and gone",
        "afterwards. Two nodes declaring the same seed share one mock; two nodes with",
        "different seeds never do.",
        "",
        "| Node | Kind | Seed file | Mock implementation |",
        "| --- | --- | --- | --- |",
    ]
    for node in graph.nodes:
        if node.kind not in DATABASE_KINDS:
            continue
        seed = f"repositories/seed/{node.id}.{SEED_SUFFIX[node.kind]}"
        module = get_database_entry(node.kind).package_module
        lines.append(
            f"| `{node.id}` | `{node.kind}` | `{seed}` | "
            f"`{_MOCK_CLASSES[node.kind]}` in `repositories/{module}.py` |"
        )
    lines.append("")

    # Only the used kinds get an extra line or an env variable: a `uv sync
    # --extra live-vector` on a graph with no vector node installs a driver
    # nothing can reach.
    lines += [
        "### Going live",
        "",
        "Install the driver for each engine this workflow uses:",
        "",
        "```bash",
    ]
    lines += [f"uv sync --extra {entry.live_extra_name}" for entry in entries]
    lines += [
        "```",
        "",
        "Then set `SWARM_DB_MODE=live` and the variables below, and change the live",
        "adapter where your connection needs something the defaults do not cover:",
        "",
        "| Kind | Environment variables | Live adapter |",
        "| --- | --- | --- |",
    ]
    for entry in entries:
        variables = ", ".join(f"`{variable}`" for variable in entry.env_vars)
        adapter = f"`{_LIVE_CLASSES[entry.kind]}` in `repositories/{entry.package_module}.py`"
        lines.append(f"| {entry.label} | {variables} | {adapter} |")
    lines.append("")
    lines += [
        "Notes:",
        "",
        "- A missing DSN variable raises an error naming it, and an unknown",
        "  `SWARM_DB_MODE` value raises too -- live mode never falls back to the mock.",
        "- A live vector search uses the same deterministic local embedder the mock",
        "  does. It is deterministic and free, but its recall is poor: pass your",
        "  provider's embedder to the live adapter for real similarity.",
        "- Agent tools can only run the read operation a node declares. There is no",
        "  free-form query tool, so a prompt cannot reach the database with a",
        "  statement of its own.",
        "- The seed files are generated from each node's declared seed; edit the node",
        "  (not the file) so a recompile does not discard the change.",
    ]
    return "\n".join(lines) + "\n"


def _render(relative_path: str, package: str) -> str:
    """Render one template with ``$package`` substituted.

    ``string.Template`` rather than ``str.format`` because these templates are
    generated *Python*: a stray ``{}`` (a dict literal, an f-string) must not be
    a placeholder, while ``$package`` cannot occur by accident. Nothing else is
    substituted -- in particular no seed path, which is a runtime argument.
    """
    return Template(read_template(relative_path)).substitute(package=package)


def _render_factory(package: str, kinds: frozenset[str]) -> str:
    """Render ``repositories/__init__.py`` from the shared factory template.

    Each used kind contributes its import block and its getter(s); an unused kind
    contributes nothing, which is what keeps a SQL-only graph from carrying an
    import of a module it never emits.
    """
    imports: list[str] = []
    getters: list[str] = []
    for kind in KIND_ORDER:
        if kind not in kinds:
            continue
        import_block, getter_block = _split_kind_factory(kind)
        imports.append(Template(import_block).substitute(package=package))
        getters.append(Template(getter_block).substitute(package=package))
    return Template(read_template("factory.py.tmpl")).substitute(
        package=package,
        kind_imports="\n".join(imports),
        kind_getters="\n\n".join(getters),
    )


def _split_kind_factory(kind: str) -> tuple[str, str]:
    """Split one kind's factory template into its imports and getters blocks.

    One file with two marker-delimited sections rather than two files: the
    getter references the class the import block brings in, so the halves are
    only meaningful together, and a second file per kind would be one more thing
    to keep in step.

    Raises:
        ValueError: If the template is missing a marker or has content before the
            first one -- a renderer's typo must fail loudly, not emit a factory
            with an undefined name in it.
    """
    text = read_template(f"{kind}/factory.py.tmpl")
    for marker in (KIND_IMPORTS_MARKER, KIND_GETTERS_MARKER):
        if marker not in text:
            raise ValueError(f"{kind}/factory.py.tmpl is missing its {marker!r} marker")
    preamble, remainder = text.split(KIND_IMPORTS_MARKER, 1)
    if preamble.strip():
        raise ValueError(
            f"{kind}/factory.py.tmpl has content before {KIND_IMPORTS_MARKER!r}"
        )
    import_block, getter_block = remainder.split(KIND_GETTERS_MARKER, 1)
    return import_block.strip(), getter_block.strip()


def database_seed_text(node: SwarmNode) -> str | None:
    """The exact text emitted as this database node's seed file, or ``None``.

    ``None`` means the node contributes no seed: it is not a database kind, or it
    carries no spec (Phase 1 reports ``db_empty_operation`` for the latter before
    any emitter runs, and inventing a seed would be the hidden default this design
    forbids).

    **One implementation, two consumers, deliberately.** The file written to
    ``repositories/seed/<node_id>.<suffix>`` and Phase 1's
    ``db_separate_mock_instances`` check must agree on this text *exactly*: the
    generated factory keys a mock instance by a digest of it, so a check that
    normalized the spec fields itself could warn about two nodes that in fact
    share one database -- or stay silent about two that do not. That is why
    ``review.py`` imports this function rather than reproducing it, and why the
    trailing-newline and JSON-dump choices below are documented rather than
    incidental.
    """
    if node.kind == "sql" and node.sql is not None:
        return _seed_sql_text(node.sql)
    if node.kind == "nosql" and node.nosql is not None:
        return _seed_json_text(node.nosql.seed)
    if node.kind == "vector" and node.vector is not None:
        return _seed_json_text(
            [document.model_dump(mode="json", by_alias=True) for document in node.vector.seed]
        )
    return None


def _render_seed_files(graph: SwarmGraph) -> dict[str, str]:
    """One seed file per database node, rendered from that node's own spec.

    Per *node*, not per kind: the factory keys a mock instance by the seed's
    content, so two nodes declaring the same seed share one database and two
    declaring different seeds never do.
    """
    files: dict[str, str] = {}
    for node in graph.nodes:
        text = database_seed_text(node)
        if text is None:
            continue
        files[f"repositories/seed/{node.id}.{SEED_SUFFIX[node.kind]}"] = text
    return files


def _seed_sql_text(spec: SqlSpec) -> str:
    """The node's seed SQL, ending in exactly one newline.

    Normalizing the trailing newline is not cosmetic: the mock instance is keyed
    by a digest of this text, so two nodes whose seeds differ only in trailing
    blank lines must still share one database rather than silently getting two.
    """
    return spec.seed_sql.rstrip("\n") + "\n"


def _seed_json_text(documents: Sequence[object]) -> str:
    """The seed documents as one JSON array of objects.

    The shape the document and vector mocks both load, and the shape Phase 1's
    seed check parses -- one shape, so a seed cannot be valid for the check and
    invalid for the mock.
    """
    return json.dumps(documents, indent=2, ensure_ascii=False) + "\n"


def database_tool_parameter(node: SwarmNode, spec: object) -> tuple[str, str]:
    """The repository tool's parameter name and the guidance the model needs.

    **Why the name is not ``query``.** A real-model run (claude-sonnet-5) showed
    the failure this prevents: with a parameter called ``query`` and a docstring
    saying "query the SQL node's declared read statement", the model passed *a SQL
    statement* -- ``{"query": "SELECT order_id, total FROM orders WHERE ..."}`` --
    which the tool then bound as the declared ``:input`` *parameter*, matched
    nothing, and returned ``[]``. The agent reported "no orders on file" while the
    database step one node earlier had returned two rows for that customer: a
    silent wrong answer, the worst failure mode this feature has. The parameter is
    therefore named for what it is on every path -- the value bound to the
    declaration -- and the rendered docstring spells out that the statement itself
    is fixed. Naming it ``query_text`` for a vector search is deliberate: there the
    text genuinely *is* the query.

    Args:
        node: The database node being attached.
        spec: Its spec, already resolved by the caller's resolved spec.

    Returns:
        ``(parameter name, guidance phrase)``.
    """
    port_type = node.io.input_type
    if isinstance(spec, VectorSpec):
        return "query_text", "the text to search for"
    if isinstance(spec, NosqlSpec):
        if port_type == "json":
            return "values", "a document merged over the declared filter's top level"
        if port_type == "list[str]":
            return "input_values", 'the list the declared filter\'s "$input" sentinel takes'
        return "input_value", 'the value the declared filter\'s "$input" sentinel takes'
    if port_type == "json":
        return "values", "a mapping of the declared statement's :name placeholders"
    if port_type == "list[str]":
        return (
            "input_values",
            "the list bound to the declared statement's single :input placeholder",
        )
    return "input_value", "the value bound to the declared statement's :input placeholder"


def database_tool_description(
    node: SwarmNode, spec: object, value_name: str, guidance: str
) -> str:
    """The repository tool's docstring: purpose, the fixed declaration, the parameter.

    The declared operation is quoted verbatim so the model can see what its
    argument is bound to -- that is the information whose absence produced the
    empty-result tool call described on :func:`database_tool_parameter`.
    """
    if isinstance(spec, VectorSpec):
        return (
            f"Search the '{spec.collection}' vector collection and return the closest "
            f"documents with their scores. Pass {value_name}: {guidance}."
        )
    if isinstance(spec, NosqlSpec):
        return (
            f"Read the '{spec.collection}' collection with the '{node.id}' node's fixed "
            f"filter {dict(spec.filter)!r} and return the matching documents. "
            f"Pass {value_name}: {guidance} -- a value, never a query; the filter itself "
            "is fixed and cannot be changed by you."
        )
    return (
        f"Run the '{node.id}' node's fixed read statement {spec.query!r} and return its rows. "
        f"Pass {value_name}: {guidance} -- a value, never a SQL statement; the statement "
        "itself is fixed and cannot be changed by you."
    )

__all__ = [
    "DATABASE_KINDS",
    "database_tool_description",
    "database_tool_parameter",
    "SEED_SUFFIX",
    "database_env_lines",
    "database_extra_lines",
    "database_files_for",
    "database_readme_section",
    "database_seed_text",
    "used_db_kinds",
]
