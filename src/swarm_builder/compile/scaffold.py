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
from swarm_builder.compile.emit_graph import emit_graph
from swarm_builder.compile.graph_ir import GraphStructure, analyze
from swarm_builder.models import PORT_TYPE_ANNOTATIONS, PORT_TYPE_IMPORTS, SwarmGraph, SwarmNode
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
}

#: PortType -> literal Python source for a representative graph.run(inputs=...)
#: sample value.
PORT_TYPE_SAMPLE_INPUT: dict[str, str] = {
    "str": '"swarm builder"',
    "list[str]": '["a", "b"]',
    "json": '{"key": "value"}',
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


def scaffold(
    graph: SwarmGraph, project_dir: Path, resolved_model: ResolvedModel
) -> ScaffoldResult:
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
    write(".env.example", _render_env_example(resolved_model))

    write("src/swarm_workflow/__init__.py", _render_package_init())
    write("src/swarm_workflow/steps/__init__.py", _render_package_init())
    write("src/swarm_workflow/agents/__init__.py", _render_package_init())
    write("src/swarm_workflow/state.py", _render_state(graph))
    write("src/swarm_workflow/deps.py", _render_deps(resolved_model))
    write("src/swarm_workflow/graph.py", emit_graph(graph))

    for node in graph.nodes:
        is_step_node = (
            node.kind in ("agent", "programmatic")
            and node.id not in structure.delegate_only_node_ids
        )
        if is_step_node:
            write(f"src/swarm_workflow/steps/{node.id}.py", _render_step(graph, node))
        if node.kind == "agent":
            write(f"src/swarm_workflow/agents/{node.id}.py", _render_agent_factory(graph, node))

    write("validate/dry_run.py", _render_dry_run(graph, structure))

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
    deps_block = "\n".join(dep_lines)
    return (
        "[project]\n"
        'name = "swarm-workflow"\n'
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
    )


def _render_readme(graph: SwarmGraph, resolved_model: ResolvedModel) -> str:
    """Render the generated project's ``README.md``.

    Names which files are generated and therefore must not be hand-edited,
    states the inherited model route, and gives the exact three commands
    that make up the keyless validation gate.
    """
    model_note = resolved_model.readme_model_note or "No inherited model route was resolved."
    return (
        f"# swarm-workflow ({graph.name})\n"
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


def _render_env_example(resolved_model: ResolvedModel) -> str:
    """Render ``.env.example``, or an empty string when the route needs none."""
    if not resolved_model.env_lines:
        return ""
    return "\n".join(resolved_model.env_lines) + "\n"


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
    return escaped.replace(
        _STEP_DOCSTRING_ESCAPED_QUOTE, f"\\{_STEP_DOCSTRING_ESCAPED_QUOTE}"
    )


def _render_step(graph: SwarmGraph, node: SwarmNode) -> str:
    """Render one ``steps/<id>.py`` module, markers included.

    Args:
        graph: The document being scaffolded (read for port types).
        node: The node this module implements.

    Returns:
        The complete step module source.
    """
    input_annotation = PORT_TYPE_ANNOTATIONS[node.io.input_type]
    output_annotation = PORT_TYPE_ANNOTATIONS[node.io.output_type]
    # Even though `from __future__ import annotations` defers these two
    # annotations (so a missing import would not actually NameError
    # here), still import to match the PORT_TYPE_IMPORTS table per fact
    # 17/contract rule 9 -- a type checker or future annotation-eval
    # helper should not need special-casing this file.
    needed_imports = sorted(
        {
            imp
            for imp in (
                PORT_TYPE_IMPORTS[node.io.input_type],
                PORT_TYPE_IMPORTS[node.io.output_type],
            )
            if imp is not None
        }
    )

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
    lines.append("")
    lines.append("")
    lines.append(
        f"async def {node.id}(ctx: StepContext[State, Deps, {input_annotation}]) "
        f"-> {output_annotation}:"
    )
    lines.append(f"    {body_marker_begin(node.id)}")
    if node.kind == "agent":
        lines.append("    agent = build_agent(ctx.deps.model)")
        lines.append("    result = await agent.run(ctx.inputs)")
        for write_field in node.writes:
            lines.append(f"    ctx.state.{write_field} = result.output")
        lines.append("    return result.output")
    else:
        lines.append('    raise NotImplementedError("swarm_builder: unfilled step body")')
    lines.append(f"    {body_marker_end(node.id)}")
    lines.append("")
    return "\n".join(lines)


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
    """
    template_id = node.template or infer_template(node.intent).suggestion
    template_text = (
        Path(__file__).resolve().parent.parent
        / "templates"
        / template_id
        / "agent.py.tmpl"
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

    if template_id == "orchestrator":
        delegate_ids = node.agent.delegates_to if node.agent else []
        delegate_imports = "\n".join(
            f"from swarm_workflow.agents.{child_id} import build_agent as _build_{child_id}"
            for child_id in delegate_ids
        )
        delegate_tool_defs = "\n\n".join(
            _render_delegate_tool(child_id) for child_id in delegate_ids
        )
        rendered = Template(template_text).substitute(
            node_id=node.id,
            instructions_repr=instructions_repr,
            delegate_imports=delegate_imports,
            delegate_tool_defs=delegate_tool_defs,
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
    if fork_ids:
        lines.append("from swarm_workflow.graph import BROADCAST_FORK_NODE_IDS")
    lines.extend(
        [
            "",
            "",
            "class _KeylessTestModel(TestModel):",
            "    \"\"\"TestModel subclass that declares no native-tool support, so an",
            "    optional native tool (e.g. a websearch template's",
            "    ``NativeTool(WebSearchTool(optional=True))``) is silently dropped",
            "    before TestModel's own unconditional native-tool rejection would",
            "    otherwise fire. Re-probed against the installed 2.43.0 wheel: plain",
            "    ``TestModel()`` raises ``UserError: TestModel does not support",
            "    built-in tools`` for ANY native tool, optional or not -- this",
            "    subclass is what makes the keyless dry run work for every",
            "    template, not just non-websearch ones.\"\"\"",
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
    if exit_is_join and exit_node.join is not None and exit_node.join.reducer in (
        "list_append",
        "list_extend",
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


__all__ = ["ScaffoldResult", "scaffold"]
