"""Deterministic emission of the whole generated-project tree (the
compile pipeline's Phase 2).

:func:`scaffold` writes every file a generated PydanticAI project needs
and returns a :class:`ScaffoldResult` recording what was written and the
``graph.render()`` text captured at scaffold time. Golden diagrams are
generated, never hand-authored: the scaffolder runs the project it just
wrote and stores the output, so the diagram cannot drift from the code.

The model side of the tree is spliced from pre-rendered source fragments
(:class:`~swarm_builder.compile.ResolvedModel`) rather than assembled from
provider knowledge: this module never branches on which provider or
protocol a route uses.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from string import Template

from swarm_builder.compile import (
    ResolvedModel,
    body_marker_begin,
    body_marker_end,
    imports_marker_begin,
    imports_marker_end,
)
from swarm_builder.compile.database import (
    DATABASE_KINDS,
    SEED_SUFFIX,
    database_env_lines,
    database_extra_lines,
    database_files_for,
    database_readme_section,
    database_tool_description,
    database_tool_parameter,
    used_db_kinds,
)
from swarm_builder.compile.emit_graph import STEP_KINDS, emit_graph
from swarm_builder.compile.graph_ir import GraphStructure, analyze
from swarm_builder.models import (
    PORT_TYPE_ANNOTATIONS,
    PORT_TYPE_IMPORTS,
    NosqlSpec,
    SqlSpec,
    SwarmGraph,
    SwarmNode,
    VectorSpec,
)
from swarm_builder.store.projects import project_dir_name
from swarm_builder.templates.registry import get_template, infer_template

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

#: The two library packages every generated project depends on, pinned to
#: the exact version this server was verified against. They are emitted as
#: exact pins rather than ranges because the generated project's dry run
#: asserts behaviour (native-tool rejection, reducer signatures) that was
#: probed against this specific wheel.
PINNED_PYDANTIC_AI_VERSION = "2.43.0"
PINNED_PYDANTIC_GRAPH_VERSION = "2.43.0"

#: The interpreter floor the generated project declares. Matches this
#: repository's own floor, since the emitted code uses ``X | None``
#: annotation syntax and ``match``-era stdlib features.
GENERATED_PROJECT_REQUIRES_PYTHON = ">=3.11"

#: PortType -> the *origin* runtime type for isinstance() checks in
#: dry_run.py (``isinstance(x, list[str])`` raises ``TypeError`` on a
#: parameterized generic, so the emitted assertion must use the origin).
PORT_TYPE_RUNTIME_ORIGIN: dict[str, str] = {
    "str": "str",
    "list[str]": "list",
    "json": "dict",
    "list[json]": "list",
}

#: PortType -> literal Python source for a representative graph.run(inputs=...)
#: sample value.
PORT_TYPE_SAMPLE_INPUT: dict[str, str] = {
    "str": '"swarm builder"',
    "list[str]": '["a", "b"]',
    "json": '{"key": "value"}',
    # A list of row objects: `list[json]` is a database read's result set, so the
    # sample the dry run feeds a graph declaring that entry port has to *be* one
    # -- a dict or a bare string would fail the port's own coercion.
    "list[json]": '[{"key": "value"}]',
}

#: ReducerId -> initial_factory default, restated here per JoinSpec's
#: docstring (list_append/list_extend -> list, dict_update -> dict, sum -> int).
_INITIAL_FACTORY_BY_REDUCER = {
    "list_append": "list",
    "list_extend": "list",
    "dict_update": "dict",
    "sum": "int",
}


@dataclass(frozen=True)
class ScaffoldResult:
    """What one :func:`scaffold` call produced.

    ``written_paths`` lists every file emitted, in emission order, so a
    caller can report or hash them without re-walking the directory, and
    ``golden_render`` is the diagram captured from the project at scaffold
    time -- Phase 5 later asserts the project still renders identically.
    """

    project_dir: Path
    written_paths: tuple[Path, ...]
    golden_render: str


def scaffold(graph: SwarmGraph, project_dir: Path, resolved_model: ResolvedModel) -> ScaffoldResult:
    """Write the whole generated-project tree for one graph document.

    Emission is fully deterministic: every byte comes from this module's
    templates plus the document, so the same graph always scaffolds to the
    same project. Each step module is emitted with its marker regions
    (empty for the body, which the fill stage then writes) and the
    ``graph.render()`` golden is captured from the freshly scaffolded
    project rather than hand-authored.

    Args:
        graph: The canvas document to emit a project for.
        project_dir: Directory to write into; created if absent.
        resolved_model: Pre-rendered model-resolution source fragments
            (see :class:`ResolvedModel`). This function never branches on
            provider or protocol.

    Returns:
        The written paths and the captured golden render.

    Raises:
        subprocess.SubprocessError: If capturing the golden render fails,
            which means the tree just written does not import.
    """
    structure = analyze(graph)
    project_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def write(rel_path: str, content: str) -> None:
        """Write one file, creating parents, and record it as emitted."""
        path = project_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written.append(path)

    write("pyproject.toml", _render_pyproject(graph, resolved_model))
    write(".python-version", _read_python_version_pin())
    write("README.md", _render_readme(graph, resolved_model))
    write(".env.example", _render_env_example(graph, resolved_model))

    write("src/swarm_workflow/__init__.py", _render_package_init())
    write("src/swarm_workflow/steps/__init__.py", _render_package_init())
    write("src/swarm_workflow/agents/__init__.py", _render_package_init())
    write("src/swarm_workflow/state.py", _render_state(graph))
    write("src/swarm_workflow/deps.py", _render_deps(resolved_model))
    write("src/swarm_workflow/graph.py", emit_graph(graph))

    # The repository layer -- the factory, the per-kind adapters, the row/port
    # helpers and one seed file per database node. Gated on the document
    # actually using a database kind, so a graph without one emits exactly the
    # tree it emitted before database nodes existed (a fixed no-database
    # document is byte-identical to the pre-change output). The renderer owns
    # the whole gate; `used_db_kinds` is consulted here too so the gate is
    # visible at the call site rather than only inside a callee.
    if used_db_kinds(graph):
        for relative_path, content in database_files_for(graph, "swarm_workflow").items():
            write(f"src/swarm_workflow/{relative_path}", content)

    for node in graph.nodes:
        is_step_node = node.kind in STEP_KINDS and node.id not in structure.delegate_only_node_ids
        if is_step_node:
            write(f"src/swarm_workflow/steps/{node.id}.py", _render_step(graph, node))
        if node.kind == "agent":
            write(f"src/swarm_workflow/agents/{node.id}.py", _render_agent_factory(graph, node))

    write("validate/dry_run.py", _render_dry_run(graph, structure))
    write("run/stream_run.py", _render_stream_run(graph, structure))

    golden_render = _capture_golden_render(project_dir)
    write("validate/golden_render.txt", golden_render)

    return ScaffoldResult(
        project_dir=project_dir, written_paths=tuple(written), golden_render=golden_render
    )


# ---------------------------------------------------------------------------
# pyproject.toml / .python-version / README / .env.example
# ---------------------------------------------------------------------------


def _read_python_version_pin() -> str:
    """Read this repository's ``.python-version`` for the generated project.

    The emitted project must pin the same interpreter this server runs on,
    so the golden render captured here and the generated project's own
    validation gate agree.
    """
    return (REPO_ROOT / ".python-version").read_text()


def _template_deps_for_graph(graph: SwarmGraph) -> list[str]:
    """Collect the package deps every ``agent`` node's template declares.

    A node's template is either set explicitly or inferred from its
    intent, mirroring ``_render_agent_factory``.
    """
    deps: set[str] = set()
    for node in graph.nodes:
        if node.kind != "agent" or node.agent is None:
            continue
        template_id = node.template or infer_template(node.intent).suggestion
        deps.update(get_template(template_id).deps)
    return sorted(deps)


def _programmatic_needs_for_graph(graph: SwarmGraph) -> list[str]:
    """Collect the extra requirements every ``programmatic`` node declares.

    ``ProgrammaticSpec.needs`` is user-supplied free text about what the
    step body will import, since the scaffolder cannot inspect a body that
    has not been written yet.
    """
    needs: set[str] = set()
    for node in graph.nodes:
        if node.kind == "programmatic" and node.programmatic is not None:
            needs.update(node.programmatic.needs)
    return sorted(needs)


def _render_pyproject(graph: SwarmGraph, resolved_model: ResolvedModel) -> str:
    """Render the generated project's ``pyproject.toml``.

    Dependencies are the pinned PydanticAI/Pydantic Graph pair, the
    ``pydantic-ai-slim[...]`` extras the inherited route needs, plus the
    deps of every template and the needs of every programmatic step.

    A graph with database nodes appends one ``[project.optional-dependencies]``
    entry per *used* engine after the existing sections. Appended, never
    interleaved: the ``[project]`` section's bytes are the same whether or not
    the graph has a database node, and the table itself is absent -- not empty --
    when no engine is used, so the live drivers stay opt-in extras a plain
    ``uv sync`` never installs.

    Args:
        graph: The document being emitted.
        resolved_model: Source of the bracketed extras (never a bare
            package name; see :class:`ResolvedModel`).

    Returns:
        The complete ``pyproject.toml`` text.
    """
    extras = sorted(resolved_model.pyproject_extras)
    bracket = f"[{','.join(extras)}]" if extras else ""
    dep_lines = [
        f'    "pydantic-ai-slim{bracket}=={PINNED_PYDANTIC_AI_VERSION}",',
        f'    "pydantic-graph=={PINNED_PYDANTIC_GRAPH_VERSION}",',
    ]
    for dep in _template_deps_for_graph(graph):
        dep_lines.append(f'    "{dep}",')
    for dep in _programmatic_needs_for_graph(graph):
        dep_lines.append(f'    "{dep}",')

    # TOML is a quoted-string format and the graph name is free text, so a
    # double quote in it would end the description string early.
    description = graph.name.replace('"', "'")
    # The project name carries the graph name (PEP 508 allows hyphens), while
    # the import package stays `swarm_workflow`. The explicit hatchling
    # packages mapping is what keeps `uv sync` working when the project name is
    # NOT the `swarm-workflow` that hatchling's heuristic would infer from
    # `src/swarm_workflow` -- without it, a slug name makes the build fail with
    # "Unable to determine which files to ship".
    project_name = project_dir_name(graph.name, graph.id)
    deps_block = "\n".join(dep_lines)
    text = (
        "[project]\n"
        f'name = "{project_name}"\n'
        'version = "0.1.0"\n'
        f'description = "{description}"\n'
        'readme = "README.md"\n'
        f'requires-python = "{GENERATED_PROJECT_REQUIRES_PYTHON}"\n'
        "dependencies = [\n"
        f"{deps_block}\n"
        "]\n"
        "\n"
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
        "\n"
        "[tool.hatch.build.targets.wheel]\n"
        'packages = ["src/swarm_workflow"]\n'
    )

    live_extras = database_extra_lines(graph)
    if live_extras:
        text += "\n[project.optional-dependencies]\n" + "\n".join(live_extras) + "\n"
    return text


def _render_readme(graph: SwarmGraph, resolved_model: ResolvedModel) -> str:
    """Render the generated project's ``README.md``.

    Names which files are generated and therefore must not be hand-edited,
    states the inherited model route, and gives the exact three commands
    that make up the keyless validation gate.

    A graph with database nodes appends the renderer's "Database nodes"
    section: the exact file to edit per engine, every environment variable the
    used engines read, and the exact ``uv sync --extra live-<kind>`` command
    for each of them. Nothing is appended for a graph without one, so a
    no-database project's README keeps its exact bytes.
    """
    model_note = resolved_model.readme_model_note or "No inherited model route was resolved."
    project_name = project_dir_name(graph.name, graph.id)
    text = (
        f"# {project_name} ({graph.name})\n"
        "\n"
        "Generated by Swarm Builder. Do not hand-edit `graph.py`, `state.py`,\n"
        "`deps.py`, or anything under `validate/` -- they are regenerated on\n"
        "every recompile.\n"
        "\n"
        f"{model_note}\n"
        "\n"
        "## Run the validation gate\n"
        "\n"
        "```bash\n"
        "export UV_CACHE_DIR=/path/to/writable/cache\n"
        "uv sync\n"
        'uv run python -c "import swarm_workflow.graph"\n'
        "uv run python validate/dry_run.py\n"
        "```\n"
        "\n"
        "All three must succeed with **no API key and no AWS credentials\n"
        "present** (the dry run injects `TestModel()`).\n"
    )
    db_section = database_readme_section(graph)
    if db_section:
        text += "\n" + db_section
    return text


def _render_env_example(graph: SwarmGraph, resolved_model: ResolvedModel) -> str:
    """Render ``.env.example``, or an empty string when nothing needs a value.

    **The rule, stated once.** The file is the inherited route's lines, with the
    database lines appended for a graph that uses a database node. The route's
    lines come first and are never replaced; the database block is what a reader
    needs to go live, so it is emitted for a database graph **even when the route
    contributes nothing** -- dry run mode is a documented first-class workflow
    with no route at all, and a database graph compiled in it still has to name
    ``SWARM_DB_MODE`` and its engine's variables. A graph with neither a route
    nor a database node still emits the empty file it always did, which is what
    keeps a no-database document byte-identical.
    """
    if not resolved_model.env_lines and not used_db_kinds(graph):
        return ""
    lines = list(resolved_model.env_lines)
    db_lines = database_env_lines(graph)
    if db_lines and lines:
        # Only a separator when there is something on both sides of it.
        lines.append("")
    lines.extend(db_lines)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# src/swarm_workflow/{__init__,state,deps}.py
# ---------------------------------------------------------------------------


def _render_package_init() -> str:
    """Render a package ``__init__.py`` (all three are identical)."""
    return '"""Generated Swarm Builder workflow package."""\n\nfrom __future__ import annotations\n'


def _render_state(graph: SwarmGraph) -> str:
    """Render ``state.py``: the dataclass threaded through every step.

    A field's annotation comes from :data:`PORT_TYPE_ANNOTATIONS` rather
    than from the ``PortType`` label itself, because the label is not
    always valid Python (``json`` is a port type, not an annotation).
    ``StateField.default`` is literal source, already rendered by the
    frontend, so it is emitted verbatim.
    """
    lines = [
        '"""Graph state, generated from the canvas document\'s stateFields."""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "",
    ]
    needed_imports = sorted(
        {imp for f in graph.state_fields if (imp := PORT_TYPE_IMPORTS[f.type]) is not None}
    )
    lines.extend(needed_imports)
    if needed_imports:
        lines.append("")
    lines.append("")
    lines.append("@dataclass")
    lines.append("class State:")
    if not graph.state_fields:
        lines.append("    pass")
    else:
        for state_field in graph.state_fields:
            annotation = PORT_TYPE_ANNOTATIONS[state_field.type]
            if state_field.default is not None:
                lines.append(f"    {state_field.name}: {annotation} = {state_field.default}")
            else:
                lines.append(f"    {state_field.name}: {annotation}")
    lines.append("")
    return "\n".join(lines)


def _render_deps(resolved_model: ResolvedModel) -> str:
    """Render ``deps.py``: the seam that carries the model into every step.

    The model-resolution helper and its imports come ready-rendered from
    :class:`ResolvedModel`; this function adds the ``Deps`` dataclass with
    the resolved default factory, so no provider knowledge lives here.
    """
    lines = [
        '"""Dependency seam carrying the model into every step."""',
        "",
        "from __future__ import annotations",
        "",
        "import os",
        "from dataclasses import dataclass, field",
        "",
        "from pydantic_ai.models import Model",
    ]
    for extra_import in resolved_model.extra_imports:
        lines.append(extra_import)
    lines.append("")
    lines.append("")
    lines.append(resolved_model.helper_source.rstrip())
    lines.append("")
    lines.append("")
    lines.append("@dataclass")
    lines.append("class Deps:")
    lines.append(
        f"    model: Model | str = field(default_factory={resolved_model.default_factory_name})"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# steps/<id>.py
# ---------------------------------------------------------------------------


#: Delimiters for a generated step module's one-line docstring, plus the
#: escapes :func:`_docstring_body` applies so that delimiter pair is safe.
#: Single quotes are used rather than ``"""`` because the body is emitted
#: in ``repr``-style double-quote form: with a ``"""`` delimiter, an
#: intent ending in a quote collides with the closing delimiter (producing
#: four quotes in a row), and an intent holding ``"""`` cannot be escaped
#: without also escaping the backslashes that introduce those escapes.
#: Nothing else depends on the choice -- the emitted docstring is a
#: docstring either way.
_STEP_DOCSTRING_DELIMITER = "'''"
_STEP_DOCSTRING_ESCAPED_QUOTE = "'"

#: Characters that are escaped explicitly rather than emitted raw, so the
#: generated docstring stays on one physical line and round-trips through
#: ``ast.get_docstring`` exactly. A bare newline in a source string
#: literal, or a bare tab (outside a string, and stripped as insignificant
#: whitespace inside a triple-quoted one), would otherwise be lost.
_STEP_DOCSTRING_ESCAPES: dict[str, str] = {
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _docstring_body(intent: str) -> str:
    """Escape ``intent`` for the body of a generated step docstring.

    ``intent`` is free text typed into the canvas Inspector, so it can
    hold quotes, backslashes or newlines -- and an intent holding any of
    those, spliced between a docstring's delimiters unescaped, makes the
    emitted module invalid Python rather than merely ugly. Backslashes are
    escaped first (so the escapes introduced by this function are not
    themselves re-escaped), then control characters, then the delimiter
    quote. Ordinary intent text comes out byte-identical to what earlier
    versions emitted.

    Args:
        intent: The node's free-text intent.

    Returns:
        The escaped body text, without delimiters.
    """
    escaped = intent
    for character, replacement in _STEP_DOCSTRING_ESCAPES.items():
        escaped = escaped.replace(character, replacement)
    return escaped.replace(_STEP_DOCSTRING_ESCAPED_QUOTE, f"\\{_STEP_DOCSTRING_ESCAPED_QUOTE}")


def _render_step(graph: SwarmGraph, node: SwarmNode) -> str:
    """Render one ``steps/<id>.py`` module, markers included.

    Three shapes, one per kind family:

    - ``agent``: the body builds the node's agent from ``ctx.deps.model`` and
      returns its output, so it is already complete when it is scaffolded;
    - ``programmatic``: the placeholder body the fill stage replaces -- the only
      kind whose body is ever model-authored;
    - the three database kinds: a short, deterministic body built from the
      node's *declared* operation, with the seed path and that operation emitted
      as module-level constants **outside** the body marker region, so a
      recompile re-reads the document rather than whatever a model wrote while
      the body region stays non-empty (``check_boundary`` refuses an empty one
      even for a file nobody fills).

    Args:
        graph: The document being scaffolded (read for port types).
        node: The node this module implements.

    Returns:
        The complete step module source.

    Raises:
        ValueError: If a database node carries no spec for its kind. Phase 1
            reports that as ``db_empty_operation`` long before here, and
            inventing an operation for it is exactly the hidden default this
            design forbids.
    """
    input_annotation = PORT_TYPE_ANNOTATIONS[node.io.input_type]
    output_annotation = PORT_TYPE_ANNOTATIONS[node.io.output_type]
    is_database = node.kind in DATABASE_KINDS
    # Even though `from __future__ import annotations` defers these two
    # annotations (so a missing import would not actually NameError
    # here), still import to match the PORT_TYPE_IMPORTS table per fact
    # 17/contract rule 9 -- a type checker or future annotation-eval
    # helper should not need special-casing this file.
    needed_imports = {
        imp
        for imp in (
            PORT_TYPE_IMPORTS[node.io.input_type],
            PORT_TYPE_IMPORTS[node.io.output_type],
        )
        if imp is not None
    }
    if is_database:
        # The emitted SEED_PATH constant is built from `Path`.
        needed_imports.add("from pathlib import Path")
    needed_imports = sorted(needed_imports)

    lines = [
        f"{_STEP_DOCSTRING_DELIMITER}``{node.id}`` step: "
        f"{_docstring_body(node.intent)}{_STEP_DOCSTRING_DELIMITER}",
        "",
        "from __future__ import annotations",
        "",
        *needed_imports,
        *([""] if needed_imports else []),
        imports_marker_begin(node.id),
        imports_marker_end(node.id),
        "",
        "from pydantic_graph import StepContext",
        "",
        "from swarm_workflow.deps import Deps",
        "from swarm_workflow.state import State",
    ]
    if node.kind == "agent":
        lines.append(f"from swarm_workflow.agents.{node.id} import build_agent")
    elif is_database:
        lines.append(
            f"from swarm_workflow.repositories import {_DB_REPOSITORY_GETTERS[node.kind]}"
        )
        portshape_helpers = "as_port"
        if node.kind == "sql" and node.io.input_type == "list[str]":
            portshape_helpers = "as_port, expand_list_param"
        elif node.kind == "nosql" and _nosql_binds_a_filter(node):
            # The sentinel binding is a *shared* rule: the langgraph emitter calls
            # the same function, so the two targets cannot disagree about what a
            # declared filter selects. Skipped for a write: `insert_one` takes its
            # document from the input and reads no filter, so importing it there
            # would be an unused import in the generated module.
            portshape_helpers = "as_port, bind_input_filter"
        lines.append(
            f"from swarm_workflow.repositories.portshape import {portshape_helpers}"
        )
    lines.append("")
    if is_database:
        lines.extend(
            _render_database_constants(
                node, _database_constant_names(node, in_agent_module=False), step=True
            )
        )
        lines.append("")
    lines.append("")
    lines.append(
        f"async def {node.id}(ctx: StepContext[State, Deps, {input_annotation}]) "
        f"-> {output_annotation}:"
    )
    lines.append(f"    {body_marker_begin(node.id)}")
    if node.kind == "agent":
        lines.append("    agent = build_agent(ctx.deps.model)")
        if node.reads:
            # A node that declares `reads` expects the agent to see those
            # state fields: without this the declaration is decorative and
            # the agent answers "I don't have the ticket" (seen in a real
            # run). The prompt stays `ctx.inputs` alone when nothing is read.
            context_parts = ", ".join(
                f'f"- {read_field}: {{ctx.state.{read_field}!r}}"' for read_field in node.reads
            )
            lines.append(
                '    prompt = f"{ctx.inputs}\\n\\nContext from state:\\n" + "\\n".join(['
                f"{context_parts}])"
            )
            lines.append("    result = await agent.run(prompt)")
        else:
            lines.append("    result = await agent.run(ctx.inputs)")
        for write_field in node.writes:
            lines.append(f"    ctx.state.{write_field} = result.output")
        lines.append("    return result.output")
    elif is_database:
        lines.extend(_render_database_step_body(node))
    else:
        lines.append('    raise NotImplementedError("swarm_builder: unfilled step body")')
    lines.append(f"    {body_marker_end(node.id)}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Database steps and agent repository tools
#
# One implementation per question, shared by both callers: an emitted step body
# and an agent's repository tool must run *the same* declared operation, and a
# second copy of the binding rules would be the copy that silently stops
# matching the first.
# ---------------------------------------------------------------------------

#: kind -> the factory getter the emitted step/tool imports and calls. All three
#: are re-exported by the generated ``repositories/__init__.py``, which is also
#: where the mock/live seam and the per-seed instance cache live.
_DB_REPOSITORY_GETTERS: dict[str, str] = {
    "sql": "get_sql_repository",
    "nosql": "get_document_repository",
    "vector": "get_vector_repository",
}

#: kind -> the tool name's suffix: a SQL or NoSQL tool *queries*, a vector tool
#: *searches*. The plan fixes both spellings.
_DB_TOOL_SUFFIXES: dict[str, str] = {"sql": "query", "nosql": "query", "vector": "search"}

#: kind -> the seed file's extension. Mirrors the renderer's own map
#: (``compile/database.py`` writes ``seed/<node_id>.sql`` for SQL and
#: ``seed/<node_id>.json`` for the other two). The table itself lives in
#: ``compile/database.py`` and is *imported*, never re-declared: it is the one
#: thing that decides which seed file a step's ``SEED_PATH`` addresses, and the
#: langgraph emitter reads the same name, so a second copy here would let the two
#: targets disagree about a filename. A test asserts the path an emitted step
#: names is a file the same scaffold really wrote.

#: A ``:name`` placeholder in a declared SQL query, ignoring ``::type`` casts:
#: the lookbehind skips a cast's second colon, so ``total::numeric`` yields no
#: placeholder while ``WHERE name = :input`` yields ``input``. Phase 1 parses the
#: same shape to decide whether a query's placeholders match its declared input
#: type; the emitter needs it for a different reason -- a ``json`` input binds
#: its own keys, and ``input`` for the whole dict only when the query really
#: declares that placeholder, because binding a parameter a query does not have
#: is a driver error at run time.
_SQL_PLACEHOLDER_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def _database_spec(node: SwarmNode) -> SqlSpec | NosqlSpec | VectorSpec:
    """The spec of ``node``'s own kind.

    Args:
        node: A node whose kind is one of :data:`DATABASE_KINDS`.

    Returns:
        The node's validated spec.

    Raises:
        ValueError: If the node carries no spec for its kind -- a document Phase
            1 rejects with ``db_empty_operation``.
    """
    spec = getattr(node, node.kind, None)
    if spec is None:
        raise ValueError(
            f"{node.kind} node {node.id!r} carries no {node.kind} spec, so it declares no "
            "operation to emit; Phase 1 reports this as db_empty_operation"
        )
    return spec


def _comment_lines(text: str) -> list[str]:
    """Free text as ``#`` comment lines, one per input line.

    A note is typed in the Inspector, so it may hold newlines; splicing it in
    raw would end the comment and emit whatever followed as code.
    """
    return [f"# {line}".rstrip() for line in text.splitlines()]


def _seed_path_literal(node: SwarmNode) -> str:
    """The ``Path(...)`` expression for a database node's own seed file.

    Two levels up from a module in ``steps/`` or ``agents/`` is the generated
    package, and ``repositories/seed/`` sits inside it for both.
    """
    file_name = f"{node.id}.{SEED_SUFFIX[node.kind]}"
    return (
        'Path(__file__).resolve().parent.parent / "repositories" / '
        f'"seed" / "{file_name}"'
    )


#: kind -> the prefix a *step* module's constants carry. A step module holds
#: exactly one database node, so its constants are named after the kind
#: (``NOSQL_FILTER``); an agent module may hold one tool per attached node, so
#: those constants are named after the node (``TICKETS_FILTER``) instead.
_STEP_CONSTANT_PREFIXES: dict[str, str] = {
    "sql": "SQL_",
    "nosql": "NOSQL_",
    "vector": "VECTOR_",
}


def _database_constant_names(node: SwarmNode, *, in_agent_module: bool) -> dict[str, str]:
    """The constant names one database node's emitted code reads.

    One function so the constants a body *reads* and the constants that are
    *emitted* can never disagree -- the failure mode being a generated module
    that names a constant nobody defined. ``SEED_PATH`` is the same name in both
    module shapes: it is the one constant whose meaning does not depend on the
    kind, and a step holds one database node either way.
    """
    prefix = _tool_constant_prefix(node) if in_agent_module else _STEP_CONSTANT_PREFIXES[node.kind]
    return {
        "seed_path": f"{prefix}SEED_PATH" if in_agent_module else "SEED_PATH",
        "query": f"{prefix}QUERY",
        "collection": f"{prefix}COLLECTION",
        "filter": f"{prefix}FILTER",
        "limit": f"{prefix}LIMIT",
        "top_k": f"{prefix}TOP_K",
        "min_score": f"{prefix}MIN_SCORE",
    }


def _render_database_constants(
    node: SwarmNode, names: dict[str, str], *, step: bool
) -> list[str]:
    """The module-level constant lines for one database node, in a fixed order.

    ``step`` selects how much of the kind's set is emitted: a step body only
    reads the constants its declared operation uses (an ``insert_one`` node has
    no filter to bind, and only a ``find`` passes a limit), while a tool is
    always read-only and needs the whole kind's set for its read fallback.

    Emitted *outside* the body marker region on purpose: the seed path and the
    declared operation are the document's values, so the fill stage can never
    rewrite what a database node runs. A step module's note is emitted as the
    comment above them -- the same text the Inspector shows as help.
    """
    spec = _database_spec(node)
    lines: list[str] = []
    if step and spec.note:
        lines.extend(_comment_lines(spec.note))
    lines.append(f"{names['seed_path']} = {_seed_path_literal(node)}")
    if isinstance(spec, SqlSpec):
        lines.append(f"{names['query']} = {spec.query!r}")
    elif isinstance(spec, NosqlSpec):
        lines.append(f"{names['collection']} = {spec.collection!r}")
        if not step or spec.operation != "insert_one":
            lines.append(f"{names['filter']} = {spec.filter!r}")
        if not step or spec.operation == "find":
            lines.append(f"{names['limit']} = {spec.limit}")
    else:
        lines.append(f"{names['collection']} = {spec.collection!r}")
        lines.append(f"{names['top_k']} = {spec.top_k}")
        lines.append(f"{names['min_score']} = {spec.min_score!r}")
    return lines


def _sql_params_expression(spec: SqlSpec, input_type: str, value_expression: str) -> str:
    """The parameter mapping one declared SQL query's input type binds.

    One rule per input type: a ``str`` input binds exactly one parameter named
    ``input`` (Phase 1 guarantees the query declares it); a ``json`` input's keys
    bind directly, plus ``input`` for the whole dict when the query declares that
    placeholder. A ``list[str]`` input is handled by the caller, which rewrites
    the query as well as the parameters.
    """
    if input_type == "str":
        return f'{{"input": {value_expression}}}'
    if input_type == "json" and "input" in _SQL_PLACEHOLDER_RE.findall(spec.query or ""):
        return f'{{**{value_expression}, "input": {value_expression}}}'
    # A `json` input whose query declares other keys, and any input type Phase 1
    # rejects (`db_input_type_unsupported`): pass the value through as the
    # mapping rather than inventing parameter names for it.
    return f"dict({value_expression})"


def _nosql_binds_a_filter(node: SwarmNode, *, read_only: bool = False) -> bool:
    """Whether this node's emitted body reads a declared filter.

    False only for an ``insert_one`` write, which takes its document from the input
    and binds no filter -- so the module must not import the binding helper. A
    read-only *tool* built on a write-declared node falls back to ``find`` (see
    :func:`_database_rows_lines`), so it binds one after all.
    """
    if node.kind != "nosql" or node.nosql is None:
        return False
    operation = node.nosql.operation
    return read_only or operation != "insert_one"


def _nosql_filter_lines(
    names: dict[str, str], value_expression: str, input_type: str, indent: str
) -> list[str]:
    """Bind a declared NoSQL filter against the incoming value.

    A filter *value* of the literal string ``"$input"`` is the sentinel for "the
    value the upstream node produced", so the document can say "match the field
    the user's input names" without carrying an expression language into JSON. A
    declared ``json`` input is merged over the filter's top level afterwards, so
    a filter key the input also names takes the input's value -- the binding rule
    for that input type.
    """
    lines = [
        f"{indent}filters = bind_input_filter({names['filter']}, {value_expression})",
    ]
    if input_type == "json":
        lines.append(f"{indent}filters.update({value_expression})")
    return lines


def _database_rows_lines(
    node: SwarmNode,
    *,
    value_expression: str,
    names: dict[str, str],
    indent: str,
    read_only: bool,
) -> list[str]:
    """The lines that call the node's repository and bind the result to ``rows``.

    Shared by an emitted step body and an agent's repository tool, because both
    must run the same declared operation; the parameters are the four things the
    two callers differ in (the constant names, the expression that supplies the
    value, the indentation, and ``read_only``).

    ``read_only`` is a tool's guarantee rather than a nicety: with it set, a node
    whose spec declares a write runs the *read* operation instead, so no path
    exists by which a model-supplied argument reaches ``execute()`` or
    ``insert_one()``. There is deliberately no free-form query tool in v1.

    Args:
        node: The database node being emitted.
        value_expression: Source for the parameter value (``ctx.inputs``, or a
            tool's argument name).
        names: The node's constant names, from
            :func:`_database_constant_names`.
        indent: The indentation of the emitted lines.
        read_only: Whether the call must be read-only whatever the spec says.

    Returns:
        Indented source lines; the caller appends the port coercion and return.
    """
    getter = _DB_REPOSITORY_GETTERS[node.kind]
    spec = _database_spec(node)
    if isinstance(spec, SqlSpec):
        write = spec.write and not read_only
        call = "execute" if write else "query"
        lines = [f"{indent}repo = {getter}({names['seed_path']})"]
        if node.io.input_type == "list[str]":
            # One `:input` placeholder expands to one placeholder per value:
            # neither SQLite nor the live driver can bind a list to a single one.
            call_expression = (
                f"repo.{call}(*expand_list_param({names['query']}, {value_expression}))"
            )
        else:
            params = _sql_params_expression(spec, node.io.input_type, value_expression)
            call_expression = f"repo.{call}({names['query']}, {params})"
        if write:
            lines.append(f'{indent}rows = [{{"rows_affected": {call_expression}}}]')
        else:
            lines.append(f"{indent}rows = {call_expression}")
        return lines

    if isinstance(spec, NosqlSpec):
        lines = [
            f"{indent}repo = {getter}({names['seed_path']}, collection={names['collection']})"
        ]
        operation = spec.operation
        if read_only and operation == "insert_one":
            # A tool only reads. A write-declared node is rejected as a tool
            # (`db_write_as_tool`); this fallback keeps the module the emitter
            # writes valid even for a document Phase 1 refused.
            operation = "find"
        if operation == "insert_one":
            lines.append(
                f'{indent}document = {value_expression} if isinstance({value_expression}, dict) '
                f'else {{"value": {value_expression}}}'
            )
            lines.append(f'{indent}rows = [{{"id": repo.insert_one(document)}}]')
            return lines
        lines.extend(_nosql_filter_lines(names, value_expression, node.io.input_type, indent))
        if operation == "find_one":
            # The declared output is a list of rows, so one document (or none)
            # arrives as a zero-or-one-element list.
            lines.append(f"{indent}document = repo.find_one(filters)")
            lines.append(f"{indent}rows = [document] if document is not None else []")
        elif operation == "count":
            lines.append(f'{indent}rows = [{{"count": repo.count(filters)}}]')
        else:
            lines.append(f"{indent}rows = repo.find(filters, limit={names['limit']})")
        return lines

    return [
        f"{indent}repo = {getter}({names['seed_path']}, collection={names['collection']})",
        f"{indent}rows = repo.search({value_expression}, top_k={names['top_k']}, "
        f"min_score={names['min_score']})",
    ]


def _render_database_step_body(node: SwarmNode) -> list[str]:
    """The complete (never model-filled) body of a database step function.

    Short and deterministic: the operation it runs is the one the document
    declares, so the body is a call plus the port coercion the node's declared
    output type promises. ``ctx.inputs`` is the only parameter source, which is
    why a declared ``reads`` field is not consulted here; a declared ``writes``
    field *is* assigned the coerced rows, so a downstream step that reads it does
    not observe an unset field (a database node is a step like any other).
    """
    lines = _database_rows_lines(
        node,
        value_expression="ctx.inputs",
        names=_database_constant_names(node, in_agent_module=False),
        indent="    ",
        read_only=False,
    )
    lines.append(f'    result = as_port(rows, "{node.io.output_type}")')
    for write_field in node.writes:
        lines.append(f"    ctx.state.{write_field} = result")
    lines.append("    return result")
    return lines


def _render_repository_tool(node: SwarmNode) -> str:
    """Render one read-only tool for a database node an agent attaches.

    ``agent.tools`` names a database node as ``<kind>:<id>``; each such entry
    becomes a tool whose signature is that node's *own* declared I/O and whose
    body runs that node's own declared read operation. Deliberately not built on
    :func:`_render_delegate_tool`, which hardcodes ``async def <child>(query: str)
    -> str`` for a child *agent*: a database node's port types come from the
    document, and the tool keeps the node's name rather than the child's.

    Args:
        node: The database node being attached.

    Returns:
        The tool definition, indented one level for the body of ``build_agent``.

    Raises:
        ValueError: If the node carries no spec for its kind (see
            :func:`_database_spec`).
    """
    spec = _database_spec(node)
    names = _database_constant_names(node, in_agent_module=True)
    value_name, guidance = database_tool_parameter(node, spec)
    description = database_tool_description(node, spec, value_name, guidance)
    lines = [
        "    @agent.tool_plain",
        f"    async def {node.id}_{_DB_TOOL_SUFFIXES[node.kind]}("
        f"{value_name}: {PORT_TYPE_ANNOTATIONS[node.io.input_type]}) "
        f"-> {PORT_TYPE_ANNOTATIONS[node.io.output_type]}:",
        f"        {_docstring_literal(description)}",
    ]
    lines.extend(
        _database_rows_lines(
            node,
            value_expression=value_name,
            names=names,
            indent="        ",
            read_only=True,
        )
    )
    lines.append(f'        return as_port(rows, "{node.io.output_type}")')
    return "\n".join(lines)


def _docstring_literal(text: str) -> str:
    """A one-line ``\"\"\"``-delimited docstring for text that may hold free text.

    Whitespace is folded to single spaces (the docstring is one physical line by
    contract) and backslashes and triple quotes are neutralized, so a collection
    name typed in the Inspector cannot end the literal early. Ordinary text comes
    out byte-identical to itself.
    """
    cleaned = " ".join(text.replace("\\", "\\\\").replace('"""', "'''").split())
    return f'"""{cleaned}"""'


def _tool_constant_prefix(node: SwarmNode) -> str:
    """The prefix one database node's tool constants carry.

    Node-prefixed rather than the step module's kind prefix: one agent may attach
    several database nodes as tools, and two tools over two nodes must not share
    a constant name.
    """
    return f"{node.id.upper()}_"


def _database_tool_nodes(graph: SwarmGraph, node: SwarmNode) -> tuple[SwarmNode, ...]:
    """The database nodes one agent attaches, in ``agent.tools`` order.

    Only the namespaced ``<kind>:<node_id>`` form is acted on. ``agent.tools``
    keeps its existing members -- a template default such as ``web_search`` -- and
    its existing role as prompt metadata, so a bare name is ignored exactly as it
    was before database nodes existed.

    An entry naming a node that does not exist, or one of another kind, is
    skipped rather than raising: Phase 1 reports it as ``db_tool_unknown_node``,
    and Phase 2 must not turn a validation finding into a crash. A repeated entry
    yields one tool, since a module cannot define the same tool name twice.
    """
    if node.agent is None:
        return ()
    node_by_id = {candidate.id: candidate for candidate in graph.nodes}
    resolved: list[SwarmNode] = []
    seen: set[str] = set()
    for entry in node.agent.tools:
        kind, separator, node_id = entry.partition(":")
        if not separator or kind not in DATABASE_KINDS:
            continue
        target = node_by_id.get(node_id)
        if target is None or target.kind != kind or target.id in seen:
            continue
        seen.add(target.id)
        resolved.append(target)
    return tuple(resolved)


def _render_repository_tool_support(graph: SwarmGraph, node: SwarmNode) -> list[str]:
    """The import lines and tool constants one agent's tools need, or ``[]``.

    Emitted inside the agent module's ``imports`` marker region: that region is
    the module's scaffolding-owned top-of-file block, and the fill stage only
    ever writes the region of a *programmatic* step module, so nothing competes
    for it here. ``Any`` is imported for the tools' annotations because
    pydantic-ai resolves a tool's type hints at run time; it is skipped when the
    template already emitted it for the node's own output type.
    """
    nodes = _database_tool_nodes(graph, node)
    if not nodes:
        return []
    already_imported = PORT_TYPE_IMPORTS[node.io.output_type]
    typing_imports = {
        imp
        for target in nodes
        for port_type in (target.io.input_type, target.io.output_type)
        if (imp := PORT_TYPE_IMPORTS[port_type]) is not None
    }
    lines = ["", "from pathlib import Path"]
    lines.extend(sorted(imp for imp in typing_imports if imp != already_imported))
    lines.extend(
        sorted(
            f"from swarm_workflow.repositories import {_DB_REPOSITORY_GETTERS[target.kind]}"
            for target in nodes
        )
    )
    tool_helpers = {"as_port"}
    for target in nodes:
        if target.kind == "nosql" and _nosql_binds_a_filter(target, read_only=True):
            tool_helpers.add("bind_input_filter")
        if target.kind == "sql" and target.io.input_type == "list[str]":
            tool_helpers.add("expand_list_param")
    lines.append(
        "from swarm_workflow.repositories.portshape import "
        + ", ".join(sorted(tool_helpers))
    )
    constants: list[str] = []
    for target in nodes:
        constants.extend(
            _render_database_constants(
                target, _database_constant_names(target, in_agent_module=True), step=False
            )
        )
    if constants:
        lines.append("")
        lines.extend(constants)
    return lines


def _render_repository_tools(graph: SwarmGraph, node: SwarmNode) -> str:
    """Every repository tool one agent attaches, blank-line separated (``""`` none)."""
    return "\n\n".join(
        _render_repository_tool(target) for target in _database_tool_nodes(graph, node)
    )


def _inject_repository_tools(rendered: str, node_id: str, tool_defs: str) -> str:
    """Splice repository tools into a template that builds its agent inline.

    ``chat`` and ``websearch`` build their agent in a single ``return Agent(...)``
    statement, so there is no local name to register a tool on. The rewrite is
    mechanical and asserted: the leading ``return`` becomes a binding, the tool
    definitions follow the expression, and the bound agent is returned. A
    template whose body is shaped differently fails the compile loudly here
    instead of emitting a tool nobody registered.

    Args:
        rendered: The already-substituted agent module.
        node_id: The node whose body region is rewritten.
        tool_defs: The tool definitions to insert.

    Returns:
        The module with its body region rewritten.

    Raises:
        ValueError: If the body region is missing, empty, or does not start with a
            single ``return`` statement.
    """
    begin = body_marker_begin(node_id)
    end = body_marker_end(node_id)
    lines = rendered.splitlines()
    begin_index = next((i for i, line in enumerate(lines) if line.strip() == begin), None)
    end_index = next(
        (
            i
            for i, line in enumerate(lines)
            if begin_index is not None and i > begin_index and line.strip() == end
        ),
        None,
    )
    if begin_index is None or end_index is None:
        raise ValueError(f"no body marker region for node {node_id!r}")
    body = lines[begin_index + 1 : end_index]
    filled = [index for index, line in enumerate(body) if line.strip()]
    if not filled:
        raise ValueError(f"the body region of node {node_id!r} is empty")
    first_index, last_index = filled[0], filled[-1]
    first_line = body[first_index]
    indent = first_line[: len(first_line) - len(first_line.lstrip())]
    if not first_line.strip().startswith("return "):
        raise ValueError(
            f"the body region of node {node_id!r} does not build its agent with a single "
            "`return` statement, so repository tools cannot be attached to it"
        )
    body[first_index] = f"{indent}agent = {first_line.strip().removeprefix('return ')}"
    new_body = [
        *body[: last_index + 1],
        "",
        *tool_defs.splitlines(),
        "",
        f"{indent}return agent",
    ]
    rewritten = "\n".join([*lines[: begin_index + 1], *new_body, *lines[end_index:]])
    return rewritten + "\n" if rendered.endswith("\n") else rewritten


# ---------------------------------------------------------------------------
# agents/<id>.py
# ---------------------------------------------------------------------------


def _render_agent_factory(graph: SwarmGraph, node: SwarmNode) -> str:
    """Render ``agents/<id>.py`` from the node's template.

    The template text is read from
    ``templates/<template_id>/agent.py.tmpl`` and filled with the intent
    (as a ``repr``, so free text cannot break the emitted literal) plus
    the four marker strings. An ``orchestrator`` template additionally
    gets one import and one tool definition per delegated child.

    **Repository tools are available to every template**, not only to
    ``orchestrator``: an agent attaches a database node by naming it in
    ``agent.tools`` as ``<kind>:<id>``, and each such entry becomes one read-only
    tool typed by that node's own declared I/O (see
    :func:`_render_repository_tool`). ``chat`` and ``websearch`` carry no
    ``$delegate_tool_defs`` slot, so their tools are spliced in mechanically by
    :func:`_inject_repository_tools` rather than by adding a placeholder to every
    template -- ``templates/**`` belongs to the agent-template catalog, and a
    second copy of each template per tool shape is the drift that avoids. The
    repository imports and the tools' constants go inside the ``imports`` marker
    region, which is the module's scaffolding-owned top-of-file block.
    """
    template_id = node.template or infer_template(node.intent).suggestion
    template_text = (
        Path(__file__).resolve().parent.parent / "templates" / template_id / "agent.py.tmpl"
    ).read_text()

    instructions_repr = repr(node.intent)
    imports_begin = imports_marker_begin(node.id)
    imports_end = imports_marker_end(node.id)
    body_begin = body_marker_begin(node.id)
    body_end = body_marker_end(node.id)

    # The agent's output_type must be the node's declared output port
    # type, not a hardcoded `str`: a node declaring a `json` port whose
    # agent returned `str` passed scaffolding and then failed the Phase-5
    # dry run's output-type assertion. Both values come from the shared
    # tables (fact 17), never from interpolating the PortType verbatim.
    output_annotation = PORT_TYPE_ANNOTATIONS[node.io.output_type]
    output_type_import_line = PORT_TYPE_IMPORTS[node.io.output_type]
    output_type_import = output_type_import_line or ""

    repository_support = _render_repository_tool_support(graph, node)
    repository_tools = _render_repository_tools(graph, node)
    if repository_support:
        # Inside the region, below its opening marker, so the marker pair still
        # encloses exactly the module's scaffolding-owned import block.
        imports_begin = imports_begin + "\n" + "\n".join(repository_support)

    if template_id == "orchestrator":
        delegate_ids = node.agent.delegates_to if node.agent else []
        delegate_imports = "\n".join(
            f"from swarm_workflow.agents.{child_id} import build_agent as _build_{child_id}"
            for child_id in delegate_ids
        )
        tool_defs = "\n\n".join(
            block
            for block in (
                "\n\n".join(_render_delegate_tool(child_id) for child_id in delegate_ids),
                repository_tools,
            )
            if block
        )
        rendered = Template(template_text).substitute(
            node_id=node.id,
            instructions_repr=instructions_repr,
            delegate_imports=delegate_imports,
            delegate_tool_defs=tool_defs,
            imports_begin=imports_begin,
            imports_end=imports_end,
            body_begin=body_begin,
            body_end=body_end,
            output_annotation=output_annotation,
            output_type_import=output_type_import,
        )
    else:
        rendered = Template(template_text).substitute(
            node_id=node.id,
            instructions_repr=instructions_repr,
            imports_begin=imports_begin,
            imports_end=imports_end,
            body_begin=body_begin,
            body_end=body_end,
            output_annotation=output_annotation,
            output_type_import=output_type_import,
        )
        if repository_tools:
            rendered = _inject_repository_tools(rendered, node.id, repository_tools)

    return rendered


def _render_delegate_tool(child_id: str) -> str:
    """Render one orchestrator tool that calls a child agent.

    The child is built from the orchestrator's own model and called with
    the tool argument as its query, which is why a delegate target is
    never also emitted as a graph step.
    """
    return (
        f"    @agent.tool_plain\n"
        f"    async def {child_id}(query: str) -> str:\n"
        f'        """Delegate to the "{child_id}" child agent."""\n'
        f"        child = _build_{child_id}(model)\n"
        f"        result = await child.run(query)\n"
        f"        return result.output"
    )


# ---------------------------------------------------------------------------
# validate/dry_run.py
# ---------------------------------------------------------------------------


def _render_dry_run(graph: SwarmGraph, structure: GraphStructure) -> str:
    """Render ``validate/dry_run.py``, the generated project's own gate.

    The script is emitted rather than shipped as a static template
    because several of its assertions are derived from this specific
    document: the expected node-id set, which node is the exit node (and
    so what ``graph.run`` should return), and the sample input for the
    entry node's port type.

    Args:
        graph: The document being emitted.
        structure: Its analysis, reused rather than recomputed.

    Returns:
        The complete ``dry_run.py`` source, starting with its module
        docstring -- which must stay the very first statement for
        ``dry_run.py``'s own docstring to exist at all.
    """
    non_delegate_ids = sorted(
        node.id for node in graph.nodes if node.id not in structure.delegate_only_node_ids
    )
    fork_ids = structure.broadcast_fork_ids()
    exit_node = structure.node_by_id[graph.exit_node_id]
    origin_type = PORT_TYPE_RUNTIME_ORIGIN[exit_node.io.output_type]
    entry_node = structure.node_by_id[graph.entry_node_id]
    sample_input = PORT_TYPE_SAMPLE_INPUT[entry_node.io.input_type]
    has_database = bool(used_db_kinds(graph))

    # The "joined collection, not one arbitrary branch" assertion only
    # makes sense when the JOIN NODE ITSELF is what the graph outputs --
    # i.e. it is the exit node. A join whose result flows into a further
    # downstream step (which itself becomes the exit node) does not make
    # `out` a collection at all, and asserting `len(out) == <arm count>`
    # unconditionally whenever any join exists anywhere in the graph is
    # wrong for that shape and for a dict_update reducer, where `len`
    # counts keys rather than joined arms.
    exit_is_join = exit_node.kind == "join"

    expected_nodes_literal = ", ".join(f'"{node_id}"' for node_id in non_delegate_ids)

    lines = [
        '"""Validation gate for the generated project.',
        "",
        "Asserts, in order:",
        "1. graph.render() equals the golden diagram emitted at scaffold time.",
        "2. graph.nodes keys equal the expected node-id set (canvas node ids",
        "   + __start__/__end__ + any synthetic *_broadcast_fork ids).",
        "3. graph.run(...) returns without raising, matching the declared",
        "   output_type (no .output access, no End assertion).",
    ]
    if has_database:
        lines.extend(
            [
                "4. every database node runs against the seeded in-memory mock:",
                "   SWARM_DB_MODE selects 'mock' (unset counts), so this gate needs",
                "   no credentials and no driver.",
            ]
        )
    lines.extend(
        [
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "import asyncio",
            "from pathlib import Path",
            "",
            "from pydantic_ai.models.test import TestModel",
            "",
            "from swarm_workflow.deps import Deps",
            "from swarm_workflow.graph import graph",
            "from swarm_workflow.state import State",
        ]
    )
    if fork_ids:
        lines.append("from swarm_workflow.graph import BROADCAST_FORK_NODE_IDS")
    if has_database:
        # Imported only for a graph that has a database node: this module's
        # bytes are the keyless gate's own contract, and a graph without one
        # emits exactly the file it emitted before database nodes existed.
        lines.append("from swarm_workflow.repositories import MOCK, db_mode")
    lines.extend(
        [
            "",
            "",
            "class _KeylessTestModel(TestModel):",
            '    """TestModel subclass that declares no native-tool support, so an',
            "    optional native tool (e.g. a websearch template's",
            "    ``NativeTool(WebSearchTool(optional=True))``) is silently dropped",
            "    before TestModel's own unconditional native-tool rejection would",
            "    otherwise fire. Re-probed against the installed 2.43.0 wheel: plain",
            "    ``TestModel()`` raises ``UserError: TestModel does not support",
            "    built-in tools`` for ANY native tool, optional or not -- this",
            "    subclass is what makes the keyless dry run work for every",
            '    template, not just non-websearch ones."""',
            "",
            "    @classmethod",
            "    def supported_native_tools(cls):",
            "        return frozenset()",
            "",
            "GOLDEN_PATH = Path(__file__).parent / 'golden_render.txt'",
            f"EXPECTED_NODES = {{'__start__', '__end__', {expected_nodes_literal}}}",
        ]
    )
    if fork_ids:
        lines.append("EXPECTED_NODES |= set(BROADCAST_FORK_NODE_IDS.values())")
    if has_database:
        lines.extend(
            [
                "",
                "",
                "def check_db_mode() -> None:",
                '    """The keyless gate runs the seeded in-memory mock, never a live',
                "    engine: this script has no credentials and no driver installed, so",
                "    a SWARM_DB_MODE that selects live (or any unrecognized value,",
                '    which raises) has to fail here, before a step reads anything."""',
                "    assert db_mode() == MOCK, (",
                "        f'validate/dry_run.py expects the mock database ({MOCK}); '",
                "        f'SWARM_DB_MODE selects {db_mode()!r}'",
                "    )",
                "    print(f'OK database mode is {MOCK}')",
            ]
        )
    lines.extend(
        [
            "",
            "",
            "def check_render() -> None:",
            "    golden = GOLDEN_PATH.read_text()",
            "    actual = graph.render()",
            "    assert actual == golden, (",
            "        f'render() drifted from golden.\\n--- golden ---\\n{golden}\\n'",
            "        f'--- actual ---\\n{actual}'",
            "    )",
            "    print('OK render() matches golden')",
            "",
            "",
            "def check_nodes() -> None:",
            "    actual = set(graph.nodes.keys())",
            "    assert actual == EXPECTED_NODES, (",
            "        f'graph.nodes keys {actual} != expected {EXPECTED_NODES}'",
            "    )",
            "    print(f'OK graph.nodes == {sorted(actual)}')",
            "",
            "",
            "async def check_run() -> None:",
            "    out = await graph.run(",
            f"        inputs={sample_input},",
            "        state=State(),",
            "        deps=Deps(model=_KeylessTestModel()),",
            "    )",
            f"    assert isinstance(out, {origin_type}), (",
            f"        f'expected {origin_type} output, got {{type(out)}}: {{out!r}}'",
            "    )",
        ]
    )
    if (
        exit_is_join
        and exit_node.join is not None
        and exit_node.join.reducer
        in (
            "list_append",
            "list_extend",
        )
    ):
        inbound = len(structure.structural_predecessors.get(exit_node.id, []))
        lines.append(
            f"    assert len(out) == {inbound}, ("
            f"f'expected {inbound} joined values, got {{len(out)}}: {{out!r}}')"
        )
    lines.extend(
        [
            "    print(f'OK graph.run() returned: {out!r}')",
            "",
            "",
            "def main() -> None:",
        ]
    )
    if has_database:
        lines.append("    check_db_mode()")
    lines.extend(
        [
            "    check_render()",
            "    check_nodes()",
            "    asyncio.run(check_run())",
            "    print('ALL CHECKS PASSED')",
            "",
            "",
            "if __name__ == '__main__':",
            "    main()",
            "",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# run/stream_run.py
# ---------------------------------------------------------------------------

#: Key of the JSON input file Swarm Builder's run job passes the workflow
#: input through, and the env var that swaps the real model for a keyless
#: ``TestModel`` (so the runner itself can be exercised without
#: credentials -- same trick as ``validate/dry_run.py``).
STREAM_RUN_INPUT_KEY = "input"
STREAM_RUN_TEST_MODEL_ENV_VAR = "SWARM_RUN_TEST_MODEL"

#: Longest rendered value (inputs, outputs, state fields) one run event
#: carries. Longer values are truncated with a marker; the run panel is a
#: trace, not a data export.
STREAM_RUN_MAX_VALUE_CHARS = 4000

#: The tracer script body, with ``$step_ids_literal`` and the constants
#: above substituted by :func:`_render_stream_run`. A ``string.Template``
#: rather than an f-string so the script's own braces stay literal.
_STREAM_RUN_TEMPLATE = Template(
    '''"""Streaming tracer for the generated project (emitted by swarm_builder).

Usage::

    uv run python run/stream_run.py <input.json>

``<input.json>`` holds ``{"$input_key": <value>}``. Every step
start/finish/failure and the final result are printed as one JSON object
per line on stdout, which Swarm Builder's Run panel streams to the canvas.
Set ``$test_model_env=1`` to run against a keyless ``TestModel`` instead of
the project's real model.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import importlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

STEP_NODE_IDS = [$step_ids_literal]
MAX_VALUE_CHARS = $max_value_chars


def _render(value):
    """A JSON-safe, bounded rendering of any value."""
    try:
        text = json.dumps(value)
    except (TypeError, ValueError):
        text = None
    if text is not None and len(text) <= MAX_VALUE_CHARS:
        return value
    preview = text if text is not None else repr(value)
    truncated = len(preview) > MAX_VALUE_CHARS
    return {"__preview__": preview[:MAX_VALUE_CHARS], "__truncated__": truncated}


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, default=repr) + "\\n")
    sys.stdout.flush()


def _state_dict(state) -> dict:
    if dataclasses.is_dataclass(state):
        return {f.name: _render(getattr(state, f.name)) for f in dataclasses.fields(state)}
    return {"__preview__": repr(state)[:MAX_VALUE_CHARS], "__truncated__": False}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _wrap(node_id: str, step):
    @functools.wraps(step)
    async def traced(ctx):
        before = _state_dict(ctx.state)
        started = time.monotonic()
        _emit({"event": "node_started", "nodeId": node_id, "inputs": _render(ctx.inputs)})
        try:
            output = await step(ctx)
        except BaseException as exc:
            _emit(
                {
                    "event": "node_failed",
                    "nodeId": node_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-MAX_VALUE_CHARS:],
                    "durationMs": _elapsed_ms(started),
                }
            )
            raise
        after = _state_dict(ctx.state)
        delta = {key: after[key] for key in after if before.get(key) != after[key]}
        _emit(
            {
                "event": "node_finished",
                "nodeId": node_id,
                "output": _render(output),
                "stateDelta": delta,
                "durationMs": _elapsed_ms(started),
            }
        )
        return output

    return traced


def _describe_model(model) -> str:
    if isinstance(model, str):
        return model
    name = getattr(model, "model_name", None)
    return name if isinstance(name, str) else type(model).__name__


async def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _emit({"event": "run_failed", "error": "usage: stream_run.py <input.json>"})
        return 2
    payload = json.loads(Path(argv[1]).read_text())
    inputs = payload["$input_key"]

    # Wrap BEFORE importing graph.py: it binds each step by importing the
    # name from its module, so the wrapper is what builder.step() sees.
    for node_id in STEP_NODE_IDS:
        module = importlib.import_module(f"swarm_workflow.steps.{node_id}")
        setattr(module, node_id, _wrap(node_id, getattr(module, node_id)))

    from swarm_workflow.deps import Deps
    from swarm_workflow.graph import graph
    from swarm_workflow.state import State

    if os.environ.get("$test_model_env") == "1":
        from pydantic_ai.models.test import TestModel

        class _KeylessTestModel(TestModel):
            @classmethod
            def supported_native_tools(cls):
                return frozenset()

        deps = Deps(model=_KeylessTestModel())
    else:
        deps = Deps()

    state = State()
    started = time.monotonic()
    _emit({"event": "run_started", "model": _describe_model(deps.model), "input": _render(inputs)})
    try:
        output = await graph.run(inputs=inputs, state=state, deps=deps)
    except BaseException as exc:
        _emit(
            {
                "event": "run_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-MAX_VALUE_CHARS:],
                "state": _state_dict(state),
                "durationMs": _elapsed_ms(started),
            }
        )
        return 1
    _emit(
        {
            "event": "run_finished",
            "output": _render(output),
            "state": _state_dict(state),
            "durationMs": _elapsed_ms(started),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
'''
)


def _render_stream_run(graph: SwarmGraph, structure: GraphStructure) -> str:
    """Render ``run/stream_run.py``, the generated project's own tracer.

    The script is what Swarm Builder's *Run* feature executes in a
    subprocess: it reads the workflow input from a JSON file named on the
    command line, wraps every step function so that starting, finishing
    and failing each emit one JSON line on stdout, runs the graph with the
    project's real ``Deps`` (real model, real credentials from the
    environment), and ends with a ``run_finished`` or ``run_failed`` line
    carrying the output and the final ``State``.

    Wrapping happens *before* ``swarm_workflow.graph`` is imported:
    ``graph.py`` does ``from swarm_workflow.steps.<id> import <id>`` at
    import time, so replacing the attribute on each step module first
    makes ``builder.step(...)`` register the wrapper. ``functools.wraps``
    keeps ``__wrapped__`` pointing at the real step, which is what lets
    pydantic-graph's ``get_type_hints`` still resolve the step's
    ``StepContext[State, Deps, ...]`` annotation against the step
    module's globals rather than this script's (``typing.get_type_hints``
    follows ``__wrapped__`` to find the globals it evaluates against).

    Like ``dry_run.py`` this is scaffolded rather than static because the
    step-node id list is document-specific. It is :data:`STEP_KINDS`' list -- a
    database step included, or it would run while the Run panel showed nothing
    for it. It lives outside the marker-bearing ``steps/``/``agents/`` trees, so
    Phase 4 hashes it whole and the fill agent can never alter what the tracer
    reports.

    Args:
        graph: The document being emitted.
        structure: Its analysis, reused rather than recomputed.

    Returns:
        The complete ``stream_run.py`` source.
    """
    step_node_ids = sorted(
        node.id
        for node in graph.nodes
        if node.kind in STEP_KINDS and node.id not in structure.delegate_only_node_ids
    )
    step_ids_literal = ", ".join(f'"{node_id}"' for node_id in step_node_ids)
    return _STREAM_RUN_TEMPLATE.substitute(
        step_ids_literal=step_ids_literal,
        input_key=STREAM_RUN_INPUT_KEY,
        test_model_env=STREAM_RUN_TEST_MODEL_ENV_VAR,
        max_value_chars=STREAM_RUN_MAX_VALUE_CHARS,
    )


# ---------------------------------------------------------------------------
# Golden-render generation. Captured in a subprocess rather than by
# importing the freshly written graph in-process: repeated scaffolds in one
# long-lived server process would otherwise collide in ``sys.modules``.
# Only valid before the fill stage adds a programmatic dependency the
# scaffolding interpreter does not have.
# ---------------------------------------------------------------------------


def _capture_golden_render(project_dir: Path) -> str:
    """Capture ``graph.render()`` from the project just written.

    Args:
        project_dir: Root of the freshly scaffolded project.

    Returns:
        The rendered diagram, stored alongside the project so Phase 5 can
        assert the generated graph still renders identically.

    Raises:
        RuntimeError: If the subprocess fails, which means the tree just
            written does not import.
    """
    script = "import swarm_workflow.graph as g; import sys; sys.stdout.write(g.graph.render())"
    env = {**os.environ, "PYTHONPATH": str(project_dir / "src")}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"golden-render subprocess failed (exit {result.returncode}):\n{result.stderr}"
        )
    return result.stdout


__all__ = [
    "STREAM_RUN_INPUT_KEY",
    "STREAM_RUN_TEST_MODEL_ENV_VAR",
    "ScaffoldResult",
    "scaffold",
]
