"""Emit a complete LangGraph project for a canvas document (Phase 6).

Everything here is deterministic. The only text a model later writes is
the body region of ``nodes/<id>.py`` for ``programmatic`` nodes; every
other file -- and every other node module -- is complete as emitted.

**How the canvas maps to LangGraph** (verified against the hand-written
``spike/langgraph_probe`` on langgraph 1.2.12):

- ``State`` is a ``TypedDict``: ``payload`` carries the value flowing
  between nodes (pydantic-graph's ``ctx.inputs``), one key per canvas
  ``stateField``, and one reducer channel ``<join>_inbox`` per join node.
- A step node (``agent``/``programmatic``) is ``add_node(id, fn)``; its
  module defines ``<id>_body(inputs, state, model, writes) -> output`` (the
  editable part) and the generated wrapper ``<id>(state, runtime)`` that
  calls it and returns the state update.
- A ``seq`` edge is ``add_edge(a, b)``; a fan-out is one ``add_edge`` per
  arm (``add_edge`` accepts a list only as the *start*); a fan-in from a real
  fan-out is ``add_edge([arms], join)`` (wait for all arms); a join fed by
  mutually exclusive decision branches gets one edge per source instead, or
  it would wait for arms that never run. Arms feeding a join write their
  output into the join's reducer channel rather than ``payload`` -- two arms
  writing ``payload`` in one superstep would raise ``InvalidUpdateError``.
- A ``decision`` is a pass-through node plus ``add_conditional_edges``
  with ``ROUTES`` (match value -> target) as the path map and a routing
  function that returns ``state["payload"]`` -- the match value the previous
  step returned, exactly as in pydantic-graph -- after checking it is a
  known key, raising otherwise. (The routing function returns the *key*;
  LangGraph does the key -> node lookup.)
- ``delegate`` edges emit nothing: a delegate-only child agent has a node
  module (so its orchestrator can call it as a ``@tool``) but no graph node.
- The model reaches nodes through ``Runtime[Context]``
  (``context_schema=Context`` on the builder, ``context=Context(model=...)``
  on ``ainvoke``), so the dry run can inject a keyless fake exactly as the
  pydantic-graph dry run injects ``TestModel``.
- A ``sql``/``nosql``/``vector`` node is a complete deterministic node module,
  never a conversion target: its body runs the operation its Inspector spec
  declares, against the repository layer ``compile/database.py`` renders under
  ``src/swarm_workflow_lg/repositories/``. The input arrives as ``inputs`` (the
  ``PAYLOAD_KEY`` payload), exactly like every other node body, and the node
  publishes the rows it read through ``writes["<field>"] = result``. An agent
  that names a database node in ``agent.tools`` gets one read-only ``@tool``
  for it, and that tool calls the *database node's own body function* -- so the
  emitted project contains one copy of each declared operation and one copy of
  the binding rule, not one per caller.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from swarm_builder.compile import (
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
from swarm_builder.compile.graph_ir import GraphStructure, analyze
from swarm_builder.compile.langgraph import (
    JOIN_INBOX_SUFFIX,
    KEYLESS_IMPORT_SNIPPET,
    PACKAGE_NAME,
    PAYLOAD_KEY,
    PINNED_LANGCHAIN_CORE_VERSION,
    PINNED_LANGCHAIN_VERSION,
    PINNED_LANGGRAPH_VERSION,
)
from swarm_builder.compile.langgraph.models import LangChainModelSource
from swarm_builder.compile.scaffold import (
    _STEP_DOCSTRING_DELIMITER,
    GENERATED_PROJECT_REQUIRES_PYTHON,
    PORT_TYPE_RUNTIME_ORIGIN,
    PORT_TYPE_SAMPLE_INPUT,
    _docstring_body,
    _programmatic_needs_for_graph,
    _read_python_version_pin,
    _render_package_init,
)
from swarm_builder.models import (
    PORT_TYPE_ANNOTATIONS,
    NosqlSpec,
    SqlSpec,
    SwarmGraph,
    SwarmNode,
    VectorSpec,
)
from swarm_builder.templates.registry import infer_template

#: The body ``lg_convert`` must replace in every ``programmatic`` node.
UNCONVERTED_BODY_SENTINEL = 'raise NotImplementedError("swarm_builder: unconverted node body")'

#: What the dry run's fake model answers. Replaced by a decision's first
#: match value when an agent node feeds that decision, so the keyless run
#: still takes a real branch.
DEFAULT_FAKE_REPLY = "fake reply"

#: Reducer id -> (state annotation, initial value literal, how an arm wraps
#: its output for the channel).
_REDUCER_CHANNELS: dict[str, tuple[str, str, str]] = {
    "list_append": ("Annotated[list[Any], operator.add]", "[]", "[output]"),
    "list_extend": ("Annotated[list[Any], operator.add]", "[]", "list(output)"),
    "dict_update": ("Annotated[dict[str, Any], _merge_dicts]", "{}", "dict(output)"),
    "sum": ("Annotated[int, operator.add]", "0", "output"),
}

_BODY_INDENT = "    "

#: The value a NoSQL node's filter uses to mean "the upstream node's value".
#: Restated from the plan's binding rule: the sentinel lives in the *document*,
#: so it is read back by the emitted module rather than by this scaffold.

#: Database kinds whose read-only tool an agent can bind, keyed by the prefix
#: ``agent.tools`` uses (``sql:<node_id>``). Deliberately the same set as
#: :data:`~swarm_builder.compile.database.DATABASE_KINDS`: an entry whose prefix
#: is not one of these is an ordinary catalog tool name (``web_search``) and is
#: ignored exactly as it is today.
_DATABASE_TOOL_PREFIXES: frozenset[str] = DATABASE_KINDS

#: kind -> the repository getter the node's body calls, and the module it comes
#: from. One row per kind so the emitted import line and the emitted call can
#: never disagree.
_REPOSITORY_GETTERS: dict[str, str] = {
    "sql": "get_sql_repository",
    "nosql": "get_document_repository",
    "vector": "get_vector_repository",
}


@dataclass(frozen=True)
class LangGraphScaffoldResult:
    """What one :func:`scaffold_langgraph` call produced."""

    project_dir: Path
    written_paths: tuple[Path, ...]
    golden_mermaid: str


def join_inbox_key(join_id: str) -> str:
    """The reducer channel a fan-out's arms write for ``join_id``."""
    return f"{join_id}{JOIN_INBOX_SUFFIX}"


def scaffold_langgraph(
    graph: SwarmGraph, project_dir: Path, model_source: LangChainModelSource
) -> LangGraphScaffoldResult:
    """Write the whole LangGraph project tree for ``graph``.

    Args:
        graph: The canvas document (already passed Phase 1).
        project_dir: Directory to write into; created if absent.
        model_source: The route rendered for LangChain
            (:func:`~swarm_builder.compile.langgraph.models.to_langchain_model_source`).

    Returns:
        The written paths and the captured Mermaid golden.

    Raises:
        RuntimeError: If capturing the golden fails, which means the tree
            just written does not import.
    """
    structure = analyze(graph)
    project_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def write(rel_path: str, content: str) -> None:
        path = project_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written.append(path)

    write("pyproject.toml", _render_pyproject(graph, model_source))
    write(".python-version", _read_python_version_pin())
    write("README.md", _render_readme(graph, model_source))
    write(".env.example", _render_env_example(graph, model_source))
    write(f"src/{PACKAGE_NAME}/__init__.py", _render_package_init())
    write(f"src/{PACKAGE_NAME}/nodes/__init__.py", _render_package_init())
    # The repository layer -- Protocols, the seeded mocks, the lazily imported
    # live adapters, the port helpers and one seed per database node. Rendered
    # once by compile/database.py with *this* target's package name and placed
    # here: the two targets share one template set rather than one copy each.
    # With no database node the mapping is empty and nothing is written, which
    # is what keeps a no-database tree byte-identical to the pre-database one.
    for relative_path, content in database_files_for(graph, PACKAGE_NAME).items():
        write(f"src/{PACKAGE_NAME}/{relative_path}", content)
    write(f"src/{PACKAGE_NAME}/state.py", _render_state(graph))
    write(f"src/{PACKAGE_NAME}/context.py", _render_context(model_source))
    write(f"src/{PACKAGE_NAME}/graph.py", emit_langgraph(graph, structure))
    for node in graph.nodes:
        write(f"src/{PACKAGE_NAME}/nodes/{node.id}.py", render_node_module(graph, structure, node))
    write("validate/dry_run.py", _render_dry_run(graph, structure))

    golden = _capture_golden_mermaid(project_dir)
    write("validate/golden_mermaid.txt", golden)
    return LangGraphScaffoldResult(
        project_dir=project_dir, written_paths=tuple(written), golden_mermaid=golden
    )


# ---------------------------------------------------------------------------
# pyproject / README
# ---------------------------------------------------------------------------


def _render_pyproject(graph: SwarmGraph, model_source: LangChainModelSource) -> str:
    deps = [
        f"langgraph=={PINNED_LANGGRAPH_VERSION}",
        f"langchain=={PINNED_LANGCHAIN_VERSION}",
        f"langchain-core=={PINNED_LANGCHAIN_CORE_VERSION}",
        model_source.dependency,
        *_programmatic_needs_for_graph(graph),
    ]
    dep_lines = "\n".join(f'    "{dep}",' for dep in deps)
    description = graph.name.replace('"', "'")
    base = (
        "[project]\n"
        f'name = "{PACKAGE_NAME.replace("_", "-")}"\n'
        'version = "0.1.0"\n'
        f'description = "{description} (LangGraph export)"\n'
        'readme = "README.md"\n'
        f'requires-python = "{GENERATED_PROJECT_REQUIRES_PYTHON}"\n'
        "dependencies = [\n"
        f"{dep_lines}\n"
        "]\n"
        "\n"
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
        "\n"
        "[tool.hatch.build.targets.wheel]\n"
        f'packages = ["src/{PACKAGE_NAME}"]\n'
    )
    # Appended, never interleaved: the ``[project]`` table and the wheel table
    # above are byte-identical for a document with no database node, and a
    # project without one never grows an empty table. TOML allows this table
    # after ``[tool.hatch...]`` -- ``[project.optional-dependencies]`` is its
    # own key, and ``[project]`` itself is neither reopened nor repeated.
    return base + _database_optional_dependencies(graph)


def _database_optional_dependencies(graph: SwarmGraph) -> str:
    """The ``[project.optional-dependencies]`` table, or ``""`` when unused.

    The live drivers are extras, never base dependencies: a plain ``uv sync``
    must install none of them, which is what keeps the keyless gate's import
    honest. The body (one ``live-<kind>`` line per *used* kind) comes from
    ``compile/database.py``, so the extra names here and the
    ``uv sync --extra ...`` commands in the README cannot drift apart.
    """
    lines = database_extra_lines(graph)
    if not lines:
        return ""
    return "\n[project.optional-dependencies]\n" + "\n".join(lines) + "\n"


def _render_env_example(graph: SwarmGraph, model_source: LangChainModelSource) -> str:
    """Render ``.env.example``: the model route, then the database variables.

    The model lines are emitted exactly as before for a document with no
    database node (same lines, same order, same trailing newline); the database
    lines are *appended* behind a blank line, and only the used engines'
    variables appear -- a ``SWARM_VECTOR_DSN`` in a project with no vector node
    would suggest a driver nothing in this workflow can reach.
    """
    database_lines = database_env_lines(graph)
    # A blank separator only when there is something on both sides of it -- the
    # same rule the pydantic-graph emitter applies, so one document's two exports
    # cannot disagree about the shape of this file.
    separator = ("",) if database_lines and model_source.env_lines else ()
    lines = [*model_source.env_lines, *separator, *database_lines]
    return "\n".join(lines) + "\n"


def _render_readme(graph: SwarmGraph, model_source: LangChainModelSource) -> str:
    base = (
        f"# {PACKAGE_NAME} ({graph.name}) -- LangGraph export\n"
        "\n"
        "Generated by Swarm Builder from the validated PydanticAI project of the\n"
        "same graph. Orchestration is pure LangGraph (`StateGraph`, `START`/`END`,\n"
        "`add_conditional_edges`, reducer channels for fan-in). LangChain is used only\n"
        "where a model is called: `init_chat_model`/`ChatOpenAI`, `langchain.messages`,\n"
        "and `@tool` for orchestrator delegation.\n"
        "\n"
        "Do not hand-edit `graph.py`, `state.py`, `context.py` or anything under\n"
        "`validate/`; they are regenerated on every recompile. The editable parts are\n"
        "the marker regions inside `nodes/<id>.py`.\n"
        "\n"
        f"{model_source.readme_note}\n"
        "\n"
        "## Run the validation gate\n"
        "\n"
        "```bash\n"
        "export UV_CACHE_DIR=/path/to/writable/cache\n"
        "uv sync\n"
        f'uv run python -c "{KEYLESS_IMPORT_SNIPPET}"\n'
        "uv run python validate/dry_run.py\n"
        "```\n"
        "\n"
        "All three succeed with no API key present: the dry run injects a keyless\n"
        "fake chat model through `Context(model=...)`.\n"
        "\n"
        "## Run it for real\n"
        "\n"
        "```python\n"
        f"from {PACKAGE_NAME}.context import Context, default_model\n"
        f"from {PACKAGE_NAME}.graph import graph\n"
        f"from {PACKAGE_NAME}.state import initial_state\n"
        "\n"
        "result = await graph.ainvoke(\n"
        '    initial_state("your input"), context=Context(model=default_model())\n'
        ")\n"
        'print(result["payload"])\n'
        "```\n"
        "\n"
        "Known limitation: a `websearch` agent node is emitted as a plain chat call.\n"
        "Bind a search tool in its node body (`model.bind_tools([...])`) for live results.\n"
    )
    # The database section is the shared text both targets append, so the file
    # to edit, the variables and the `uv sync --extra ...` commands are stated
    # once. Nothing is appended for a document with no database node.
    section = database_readme_section(graph)
    if not section:
        return base
    return f"{base}\n{section}"


# ---------------------------------------------------------------------------
# state.py / context.py
# ---------------------------------------------------------------------------


def _render_state(graph: SwarmGraph) -> str:
    join_nodes = [n for n in graph.nodes if n.kind == "join" and n.join is not None]
    needs_merge = any(n.join.reducer == "dict_update" for n in join_nodes)  # type: ignore[union-attr]
    lines = [
        '"""Graph state for the LangGraph export: a TypedDict with reducer channels."""',
        "",
        "from __future__ import annotations",
        "",
        "import operator",
        "from typing import Annotated, Any",
        "",
        "from typing_extensions import TypedDict",
        "",
    ]
    if needs_merge:
        lines.extend(
            [
                "",
                "def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:",
                '    """dict_update reducer: right-hand keys win."""',
                "    return {**left, **right}",
                "",
            ]
        )
    lines.extend(
        [
            "",
            "class State(TypedDict, total=False):",
            f'    """`{PAYLOAD_KEY}` is the value flowing between nodes (pydantic-graph\'s',
            "    ``ctx.inputs``); the other keys are the canvas state fields and one",
            '    reducer channel per join node."""',
            "",
            f"    {PAYLOAD_KEY}: Any",
        ]
    )
    for state_field in graph.state_fields:
        lines.append(f"    {state_field.name}: {PORT_TYPE_ANNOTATIONS[state_field.type]}")
    for node in join_nodes:
        annotation, _initial, _wrap = _REDUCER_CHANNELS[node.join.reducer]  # type: ignore[union-attr]
        lines.append(f"    {join_inbox_key(node.id)}: {annotation}")
    lines.extend(
        [
            "",
            "",
            "#: Initial values for every key a run starts with.",
            "INITIAL_STATE: dict[str, Any] = {",
        ]
    )
    for state_field in graph.state_fields:
        default = state_field.default if state_field.default is not None else "None"
        lines.append(f'    "{state_field.name}": {default},')
    for node in join_nodes:
        _annotation, initial, _wrap = _REDUCER_CHANNELS[node.join.reducer]  # type: ignore[union-attr]
        lines.append(f'    "{join_inbox_key(node.id)}": {initial},')
    lines.extend(
        [
            "}",
            "",
            "",
            "def initial_state(payload: Any) -> dict[str, Any]:",
            '    """The input dict for ``graph.ainvoke``: defaults plus the entry payload."""',
            f'    return {{**INITIAL_STATE, "{PAYLOAD_KEY}": payload}}',
            "",
        ]
    )
    return "\n".join(lines)


def _render_context(model_source: LangChainModelSource) -> str:
    return (
        '"""Runtime context: the chat model every node calls.\n'
        "\n"
        "Passed as ``context=Context(model=...)`` to ``graph.ainvoke``; nodes read it\n"
        "through ``runtime.context.model``. The dry run injects a keyless fake here.\n"
        '"""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import os\n"
        "from dataclasses import dataclass\n"
        "\n"
        "from langchain_core.language_models import BaseChatModel\n"
        "\n"
        f"{model_source.helper_source.rstrip()}\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class Context:\n"
        "    model: BaseChatModel\n"
    )


# ---------------------------------------------------------------------------
# graph.py
# ---------------------------------------------------------------------------


def emit_langgraph(graph: SwarmGraph, structure: GraphStructure | None = None) -> str:
    """Render ``graph.py``: the ``StateGraph`` wiring for ``graph``."""
    structure = structure if structure is not None else analyze(graph)
    node_by_id = structure.node_by_id
    graph_nodes = [n for n in graph.nodes if n.id not in structure.delegate_only_node_ids]
    decisions = [n for n in graph_nodes if n.kind == "decision" and n.decision is not None]

    lines = [
        '"""StateGraph wiring for the LangGraph export.',
        "",
        "Emitted deterministically by swarm_builder; never touched by the conversion",
        "agent. Decisions are pass-through nodes with conditional edges; fan-in goes",
        "through the join's reducer channel.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from langgraph.graph import END, START, StateGraph",
        "",
        f"from {PACKAGE_NAME}.context import Context",
        f"from {PACKAGE_NAME}.state import State",
    ]
    for node in graph_nodes:
        names = [node.id]
        if node.kind == "decision":
            names.append(f"route_{node.id}")
        lines.append(f"from {PACKAGE_NAME}.nodes.{node.id} import {', '.join(names)}")
    lines.extend(["", "builder = StateGraph(State, context_schema=Context)", ""])
    for node in graph_nodes:
        lines.append(f'builder.add_node("{node.id}", {node.id})')
    lines.append("")
    lines.append(f'builder.add_edge(START, "{graph.entry_node_id}")')

    join_sources: dict[str, list[str]] = defaultdict(list)
    fanout_joins = {edge.join_node_id for edge in graph.edges if edge.kind == "fanout"}
    for edge in graph.edges:
        if edge.kind == "seq" or edge.kind == "fanout":
            lines.append(f'builder.add_edge("{edge.source}", "{edge.target}")')
        elif edge.kind == "join":
            join_sources[edge.target].append(edge.source)
    for join_id, sources in join_sources.items():
        if join_id in fanout_joins:
            # Arms of a real fan-out all run: wait for every one of them.
            arms = ", ".join(f'"{source}"' for source in sorted(sources))
            lines.append(f'builder.add_edge([{arms}], "{join_id}")')
        else:
            # Mutually exclusive sources (decision branches converging on a
            # join): a list start would wait for arms that never run, so the
            # join must fire on whichever source completes -- one edge each,
            # which is how pydantic-graph treats a join edge outside a fork.
            for source in sorted(sources):
                lines.append(f'builder.add_edge("{source}", "{join_id}")')
    for node in decisions:
        path_map = ", ".join(
            f'"{branch.match}": "{branch.target_node_id}"'
            for branch in node.decision.branches  # type: ignore[union-attr]
        )
        lines.append(f'builder.add_conditional_edges("{node.id}", route_{node.id}, {{{path_map}}})')
    for node_id in sorted(structure.structural_sink_node_ids):
        if node_by_id[node_id].kind != "decision":
            lines.append(f'builder.add_edge("{node_id}", END)')
    lines.extend(["", "graph = builder.compile()", ""])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# nodes/<id>.py
# ---------------------------------------------------------------------------


def _join_fed_by(graph: SwarmGraph, node: SwarmNode) -> SwarmNode | None:
    """The join node ``node`` feeds through a ``join`` edge, if any."""
    node_by_id = {n.id: n for n in graph.nodes}
    for edge in graph.edges:
        if edge.kind == "join" and edge.source == node.id:
            return node_by_id.get(edge.target)
    return None


def _return_line(graph: SwarmGraph, node: SwarmNode) -> str:
    """How the wrapper turns the body's output into a state update."""
    join = _join_fed_by(graph, node)
    if join is not None and join.join is not None:
        _annotation, _initial, wrap = _REDUCER_CHANNELS[join.join.reducer]
        return f'    return {{**writes, "{join_inbox_key(join.id)}": {wrap}}}'
    return f'    return {{**writes, "{PAYLOAD_KEY}": output}}'


def _default_agent_body(
    graph: SwarmGraph, node: SwarmNode, database_tools: tuple[SwarmNode, ...] = ()
) -> list[str]:
    """Deterministic agent body: one chat call, output coerced to the port type.

    An orchestrator additionally wraps each delegated child as a ``@tool``, and
    any agent may wrap each database node it names in ``agent.tools`` as a
    read-only ``@tool``. Both kinds of tool are bound with
    ``model.bind_tools([...])`` and driven by the same bounded tool loop -- pure
    LangChain model/tool primitives, no
    ``create_agent``/``create_react_agent``.

    Args:
        graph: The document being emitted (kept in the signature alongside
            :func:`render_node_module`'s other body renderers).
        node: The agent node.
        database_tools: The database nodes this agent may call, in the order
            ``agent.tools`` declares them (:func:`database_tool_targets`).
    """
    instructions = node.agent.instructions if node.agent else node.intent
    template = node.template or infer_template(node.intent).suggestion
    delegates = list(node.agent.delegates_to) if node.agent else []
    body: list[str] = [f"    system = SystemMessage({instructions!r})"]
    if node.reads:
        # Declared `reads` reach the model as context lines, exactly as in
        # the pydantic-graph step template.
        context_parts = ", ".join(
            f'f"- {field}: {{state.get({field!r})!r}}"' for field in node.reads
        )
        body.append(
            '    user_text = f"{inputs}\\n\\nContext from state:\\n" + "\\n".join(['
            f"{context_parts}])"
        )
    else:
        body.append("    user_text = str(inputs)")

    tool_names: list[str] = []
    if template == "orchestrator" and delegates:
        for child in delegates:
            body.extend(
                [
                    "",
                    "    @tool",
                    f"    async def {child}(query: str) -> str:",
                    f'        """Delegate to the {child!r} child agent."""',
                    f"        return str(await {child}_body(query, state, model, {{}}))",
                ]
            )
        tool_names.extend(delegates)
    for database_node in database_tools:
        body.extend(_database_tool_lines(database_node))
        tool_names.append(database_tool_name(database_node))

    if tool_names:
        tools = ", ".join(tool_names)
        body.extend(
            [
                "",
                f"    tools_by_name = {{t.name: t for t in [{tools}]}}",
                "    bound = model.bind_tools(list(tools_by_name.values()))",
                "    messages: list[Any] = [system, HumanMessage(user_text)]",
                "    reply = await bound.ainvoke(messages)",
                "    for _ in range(MAX_TOOL_ROUNDS):",
                "        if not reply.tool_calls:",
                "            break",
                "        messages.append(reply)",
                "        for call in reply.tool_calls:",
                '            messages.append(await tools_by_name[call["name"]].ainvoke(call))',
                "        reply = await bound.ainvoke(messages)",
            ]
        )
    else:
        if template == "websearch":
            body.append("    # websearch template: bind a search tool here for live results.")
        body.append("    reply = await model.ainvoke([system, HumanMessage(user_text)])")
    body.append(f"    output = {_coerce_expression(node.io.output_type)}")
    for field in node.writes:
        body.append(f'    writes["{field}"] = output')
    body.append("    return output")
    return body


def database_tool_name(node: SwarmNode) -> str:
    """The name of the read-only tool an agent gets for database node ``node``.

    ``<node_id>_query`` for SQL and NoSQL, ``<node_id>_search`` for a vector
    node. Node ids are unique and already valid Python identifiers, so the id
    prefix is what keeps the emitted function names unique inside one agent
    module and needs no sanitization.
    """
    return f"{node.id}_search" if node.kind == "vector" else f"{node.id}_query"


def database_tool_targets(graph: SwarmGraph, node: SwarmNode) -> tuple[SwarmNode, ...]:
    """The database nodes ``node``'s agent may call, in declaration order.

    Only the *namespaced* entries of ``agent.tools`` are acted on --
    ``sql:<node_id>`` / ``nosql:<node_id>`` / ``vector:<node_id>``. An entry with
    no ``<kind>:`` prefix is a catalog tool name (``web_search``) and stays
    prompt metadata, exactly as before database nodes existed.

    An entry naming a node that does not exist, one of another kind, or one whose
    declared operation *writes* is not acted on. All three are Phase 1 findings
    (``db_tool_unknown_node``, ``db_write_as_tool``), and a deterministic emitter
    that runs on an already-validated document must not turn a validation finding
    into a crash -- the same reading the pydantic-graph emitter takes. Skipping a
    write-declared node is also what keeps a model-reachable write impossible
    here: this target's tool calls the node's *own* body, which for such a node
    is the ``execute``/``insert_one`` path, so there is no read variant of it to
    offer instead.

    Duplicate entries collapse to one tool: the declaration is identical, so
    emitting it twice would only repeat an import and a function definition.

    Args:
        graph: The document the agent belongs to.
        node: The agent node (any other kind yields no tools).

    Returns:
        The database nodes to emit one read-only tool for, in order.
    """
    if node.kind != "agent" or node.agent is None:
        return ()
    node_by_id = {candidate.id: candidate for candidate in graph.nodes}
    targets: list[SwarmNode] = []
    seen: set[str] = set()
    for entry in node.agent.tools:
        kind, separator, target_id = entry.partition(":")
        if not separator or kind not in _DATABASE_TOOL_PREFIXES:
            continue  # a catalog tool name such as `web_search`
        target = node_by_id.get(target_id)
        if target is None or target.kind != kind or target.id in seen:
            continue
        if _database_operation_writes(target):
            continue
        seen.add(target.id)
        targets.append(target)
    return tuple(targets)


def _database_operation_writes(node: SwarmNode) -> bool:
    """Whether ``node``'s declared operation writes (so it cannot be a tool).

    Phase 1's ``db_write_as_tool`` is the rule; this is the same declaration read
    again at emission time, so a write-declared node can never be wrapped as a
    tool even if the document reaches the emitter unvalidated.
    """
    if node.kind == "sql" and node.sql is not None:
        return bool(node.sql.write)
    if node.kind == "nosql" and node.nosql is not None:
        return node.nosql.operation == "insert_one"
    return False


def _database_tool_lines(node: SwarmNode) -> list[str]:
    """One read-only ``@tool`` for ``node``, as body lines.

    The tool calls the database node's *own* body function rather than
    re-deriving the operation and its parameter binding. That keeps one copy of
    each declared operation in the project (the node module), makes the tool's
    value exactly the step's value, and matches how an orchestrator already
    calls a delegated child's body. ``writes`` is a throwaway dict: a tool
    answers the model, it does not publish state.
    """
    name = database_tool_name(node)
    annotation = PORT_TYPE_ANNOTATIONS[node.io.input_type]
    output = PORT_TYPE_ANNOTATIONS[node.io.output_type]
    spec = _database_spec(node)
    # The parameter name and the docstring come from the shared renderer, so both
    # targets describe *what the argument is bound to* identically. A parameter
    # called `query` with a docstring about "the declared read query" made a real
    # model pass a SQL statement, which the tool bound as the declared parameter
    # and matched nothing -- a silent empty result (see database_tool_parameter).
    value_name, guidance = database_tool_parameter(node, spec)
    description = database_tool_description(node, spec, value_name, guidance)
    return [
        "",
        "    @tool",
        f"    async def {name}({value_name}: {annotation}) -> {output}:",
        f"        {description!r}",
        f"        return await {node.id}_body({value_name}, state, model, {{}})",
    ]


def _nosql_binds_a_filter(node: SwarmNode) -> bool:
    """Whether this node's emitted body reads a declared filter.

    False only for an ``insert_one`` write, which takes its document from the input
    and binds no filter, so the node module must not import the binding helper.
    ``scaffold.py`` has the read-only-aware twin of this predicate for its tool
    path; both are one line and describe the same emitted dispatch.
    """
    if node.kind != "nosql" or node.nosql is None:
        return False
    return node.nosql.operation != "insert_one"


def _coerce_expression(port_type: str) -> str:
    """Turn ``reply.text`` into the declared output port type, safely."""
    if port_type == "json":
        return "_parse_json_reply(reply.text)"
    if port_type == "list[json]":
        return "_parse_json_rows_reply(reply.text)"
    if port_type == "list[str]":
        return "[line.strip() for line in reply.text.splitlines() if line.strip()]"
    return "reply.text"


def _parse_json_rows_helper_lines() -> list[str]:
    """The ``list[json]`` reply coercion, emitted only where it is used.

    A sibling of ``_parse_json_reply`` rather than a call into the generated
    ``repositories/portshape.py``: a document with no database node has no
    repositories package, and a ``list[json]`` *agent* output is legal on its
    own. Emitted only for an agent whose declared output is ``list[json]``, so
    every other node module is byte-identical to what it was before ``list[json]``
    existed.
    """
    return [
        "",
        "",
        "def _parse_json_rows_reply(text: str) -> list[dict[str, Any]]:",
        '    """Best-effort JSON array of rows from a model reply."""',
        "    try:",
        "        value = json.loads(text)",
        "    except ValueError:",
        '        return [{"text": text}]',
        "    if isinstance(value, list):",
        '        return [item if isinstance(item, dict) else {"value": item} for item in value]',
        '    return [value if isinstance(value, dict) else {"value": value}]',
    ]


# ---------------------------------------------------------------------------
# nodes/<id>.py -- a database node's body
#
# The body is complete and deterministic, exactly like a join or decision
# body: a database node's value is *declared data* (its Inspector spec), so
# nothing here is model-authored and lg_convert never receives the module.
# ---------------------------------------------------------------------------


#: kind -> the input port types this emitter can bind.
#:
#: This mirrors the plan's per-kind binding table, which is deliberately wider
#: than Phase 1's gate: Phase 1 rejects ``list[str]`` for both SQL and NoSQL
#: (``db_input_type_unsupported``), but the binding rule for those pairs exists
#: and is emitted here, so a document that reaches this renderer with one of
#: them is emitted correctly rather than crashing. The cells the plan marks
#: *rejected* -- ``list[json]`` for every kind, ``list[str]`` for vector -- are
#: absent, and a node declaring one raises.
_DATABASE_BINDABLE_INPUTS: dict[str, tuple[str, ...]] = {
    "sql": ("str", "json", "list[str]"),
    "nosql": ("str", "json", "list[str]"),
    "vector": ("str", "json"),
}


def _database_spec(node: SwarmNode) -> SqlSpec | NosqlSpec | VectorSpec:
    """The node's declared spec, or a loud failure naming the node.

    Phase 1 rejects a database node with no spec (``db_empty_operation``) long
    before any emitter runs, so this guards the case where a renderer is called
    directly on an unvalidated document: emitting a body that reads nothing
    would be a silently wrong node.
    """
    spec: SqlSpec | NosqlSpec | VectorSpec | None = None
    if node.kind == "sql":
        spec = node.sql
    elif node.kind == "nosql":
        spec = node.nosql
    elif node.kind == "vector":
        spec = node.vector
    if spec is None:
        raise ValueError(
            f"{node.kind} node {node.id!r} carries no {node.kind!r} spec, so it declares "
            "no operation to emit (Phase 1 reports this as db_empty_operation)"
        )
    return spec


def _unsupported_input(node: SwarmNode) -> ValueError:
    """The error for a database node whose declared input has no binding rule."""
    expected = ", ".join(repr(item) for item in _DATABASE_BINDABLE_INPUTS[node.kind])
    return ValueError(
        f"{node.kind} node {node.id!r} declares input_type {node.io.input_type!r}, which "
        f"has no binding rule (expected one of {expected}); Phase 1 reports this as "
        "db_input_type_unsupported"
    )


def _database_import_lines(node: SwarmNode) -> list[str]:
    """The imports a database node module needs, in the generated region.

    The generated region, not the ``imports`` marker region: the conversion
    agent owns the marker regions of ``programmatic`` nodes, and a database
    node is never converted -- so these lines must not be somewhere an agent's
    ``write_region`` could replace them. This mirrors how an orchestrator's
    ``<child>_body`` imports are emitted.
    """
    getter = _REPOSITORY_GETTERS[node.kind]
    portshape = ["as_port"]
    if node.kind == "sql" and node.io.input_type == "list[str]":
        # The one binding that rewrites the query text before it is run.
        portshape.append("expand_list_param")
    elif node.kind == "nosql" and _nosql_binds_a_filter(node):
        # The sentinel binding is a *shared* rule (see
        # ``templates/database/portshape.py.tmpl``): the pydantic-graph emitter calls
        # the same function, so the two targets cannot disagree about what a
        # declared filter selects -- they did, and one of them silently returned no
        # rows for an ``$in`` shape. Skipped for a write, which binds no filter.
        portshape.append("bind_input_filter")
    return [
        f"from {PACKAGE_NAME}.repositories import {getter}",
        f"from {PACKAGE_NAME}.repositories.portshape import {', '.join(portshape)}",
    ]


def _database_constant_lines(node: SwarmNode) -> list[str]:
    """The module-level constants a database node's body reads, plus its note.

    They sit *outside* the body markers because the operation and the seed path
    are part of the node's declaration rather than of its implementation: the
    Inspector shows the same values, and a recompile regenerates both from the
    document. ``note`` becomes the comment above them.
    """
    spec = _database_spec(node)
    lines: list[str] = []
    if spec.note and spec.note.strip():
        lines.extend(f"# {line}" for line in spec.note.strip().splitlines())
    # The seed file compile/database.py renders for this node, addressed from
    # the node module's own location (``nodes/`` -> the package root). The
    # suffix comes from that module's map rather than being derived here, so a
    # seed path can never name a file the emitter did not write.
    suffix = SEED_SUFFIX[node.kind]
    lines.append(
        'SEED_PATH = Path(__file__).resolve().parent.parent / "repositories" / "seed" / '
        f'"{node.id}.{suffix}"'
    )
    if isinstance(spec, SqlSpec):
        lines.append(f"SQL_QUERY = {spec.query!r}")
    elif isinstance(spec, NosqlSpec):
        lines.append(f"COLLECTION = {spec.collection!r}")
        lines.append(f"FILTER = {spec.filter!r}")
        lines.append(f"LIMIT = {spec.limit!r}")
    else:
        lines.append(f"COLLECTION = {spec.collection!r}")
        lines.append(f"TOP_K = {spec.top_k!r}")
        lines.append(f"MIN_SCORE = {spec.min_score!r}")
    return lines


def _database_node_body(node: SwarmNode) -> list[str]:
    """The complete deterministic body of a database node, as body lines.

    Never :data:`UNCONVERTED_BODY_SENTINEL`: the operation, its parameters and
    the port coercion are all declared in the document, and the emitted call
    runs exactly that. The value the previous node produced arrives as
    ``inputs`` (the ``PAYLOAD_KEY`` payload) and is the only parameter source;
    rows are coerced into the declared ``list[json]`` port so they stay rows
    when they cross the next edge; and the result is published through
    ``writes["<field>"]`` exactly like every other node body.
    """
    spec = _database_spec(node)
    if isinstance(spec, SqlSpec):
        lines = _sql_body(node, spec)
    elif isinstance(spec, NosqlSpec):
        lines = _nosql_body(node, spec)
    else:
        lines = _vector_body(node, spec)
    for field in node.writes:
        lines.append(f'    writes["{field}"] = result')
    lines.append("    return result")
    return lines


def _sql_body(node: SwarmNode, spec: SqlSpec) -> list[str]:
    """Bind, run and coerce one SQL node's declared operation.

    One binding rule per declared input type, and no guessing: ``str`` binds
    ``:input``, ``json`` binds the dict's own keys plus ``:input`` for the whole
    dict, and ``list[str]`` expands a single ``:input`` into
    ``:input_0, :input_1, …`` (SQLite cannot bind a list to one placeholder).
    """
    input_type = node.io.input_type
    prelude: list[str] = []
    query = "SQL_QUERY"
    if input_type == "str":
        params = '{"input": inputs}'
    elif input_type == "json":
        prelude.append('    values = as_port(inputs, "json")')
        params = '{**values, "input": values}'
    elif input_type == "list[str]":
        prelude.append(
            '    sql, params = expand_list_param(SQL_QUERY, as_port(inputs, "list[str]"))'
        )
        query, params = "sql", "params"
    else:
        raise _unsupported_input(node)
    lines = [*prelude, "    repo = get_sql_repository(SEED_PATH)"]
    if spec.write:
        # The declared write path, and only because the Inspector said
        # `write: true`: no agent tool can reach it (an agent may not name a
        # write node), so no model-authored statement ever arrives here. The
        # declared output port is still `list[json]`, so the affected-row count
        # is published as one row -- the same row this target's pydantic-graph
        # counterpart emits.
        lines.append(f"    affected = repo.execute({query}, {params})")
        lines.append('    result = as_port([{"rows_affected": affected}], "list[json]")')
    else:
        lines.append(f"    rows = repo.query({query}, {params})")
        lines.append('    result = as_port(rows, "list[json]")')
    return lines


def _nosql_body(node: SwarmNode, spec: NosqlSpec) -> list[str]:
    """Bind, run and coerce one NoSQL node's declared operation."""
    input_type = node.io.input_type
    if spec.operation == "insert_one":
        if input_type not in ("str", "json"):
            raise _unsupported_input(node)
        # The document is the input itself when the input is a mapping, and a
        # one-key document otherwise: a write node's only declared parameter
        # source is the value the previous node produced.
        return [
            "    repo = get_document_repository(SEED_PATH, COLLECTION)",
            "    document = inputs if isinstance(inputs, dict) else {"
            + '"value": inputs}',
            "    inserted_id = repo.insert_one(document)",
            '    result = as_port([{"id": inserted_id}], "list[json]")',
        ]
    prelude: list[str] = []
    if input_type == "str":
        bound_filter = "bind_input_filter(FILTER, inputs)"
    elif input_type == "json":
        # The keys merge over the filter's top level, and a `$input` sentinel
        # that survives that merge still means "the whole input".
        prelude.append('    values = as_port(inputs, "json")')
        bound_filter = "bind_input_filter({**FILTER, **values}, values)"
    elif input_type == "list[str]":
        bound_filter = 'bind_input_filter(FILTER, as_port(inputs, "list[str]"))'
    else:
        raise _unsupported_input(node)
    lines = [*prelude, "    repo = get_document_repository(SEED_PATH, COLLECTION)"]
    if spec.operation == "find_one":
        # The port is `list[json]` for every database kind, so a single
        # document is published as a list of zero or one row rather than as
        # one row: the shape the next node declared is the shape it gets.
        lines.append(f"    row = repo.find_one({bound_filter})")
        lines.append('    result = as_port([row] if row is not None else [], "list[json]")')
    elif spec.operation == "count":
        lines.append(f"    count = repo.count({bound_filter})")
        lines.append('    result = as_port([{"count": count}], "list[json]")')
    else:  # find
        lines.append(f"    rows = repo.find({bound_filter}, limit=LIMIT)")
        lines.append('    result = as_port(rows, "list[json]")')
    return lines


def _vector_body(node: SwarmNode, spec: VectorSpec) -> list[str]:
    """Bind, run and coerce one vector node's declared search."""
    input_type = node.io.input_type
    repo = "    repo = get_vector_repository(SEED_PATH, COLLECTION)"
    if input_type == "str":
        # The input *is* the query text; a non-string is not coerced into one,
        # because hashing something that is not text would answer a question
        # nobody asked.
        return [
            repo,
            "    rows = repo.search(inputs, top_k=TOP_K, min_score=MIN_SCORE)",
            '    result = as_port(rows, "list[json]")',
        ]
    if input_type != "json":
        raise _unsupported_input(node)
    return [
        '    options = as_port(inputs, "json")',
        repo,
        "    rows = repo.search(",
        '        as_port(options["query"], "str"),',
        '        top_k=int(options.get("top_k", TOP_K)),',
        "        min_score=MIN_SCORE,",
        "    )",
        '    result = as_port(rows, "list[json]")',
    ]


def _decision_lines(node: SwarmNode) -> list[str]:
    branches = node.decision.branches if node.decision else []
    mapping = ", ".join(f"{b.match!r}: {b.target_node_id!r}" for b in branches)
    expected = ", ".join(repr(b.match) for b in branches)
    return [
        "",
        "",
        f"ROUTES: dict[str, str] = {{{mapping}}}",
        "",
        "",
        f"def route_{node.id}(state: State) -> str:",
        '    """Pick the branch from the match value the previous node returned.',
        "",
        "    Returns the match value itself: ``add_conditional_edges`` is given",
        "    ``ROUTES`` as its path map, so LangGraph maps that key to the target",
        "    node. Returning the node name here would be a KeyError in LangGraph's",
        "    branch (found by a real-model compile where match != node id).",
        '    """',
        f'    value = state.get("{PAYLOAD_KEY}")',
        "    if isinstance(value, str) and value in ROUTES:",
        "        return value",
        "    raise ValueError(",
        f'        f"decision {node.id}: no branch matched {{value!r}} "',
        f'        "(expected one of {expected})"',
        "    )",
    ]


def render_node_module(graph: SwarmGraph, structure: GraphStructure, node: SwarmNode) -> str:
    """Render ``nodes/<id>.py`` for any node kind.

    Programmatic bodies carry :data:`UNCONVERTED_BODY_SENTINEL` for
    ``lg_convert`` to replace; every other kind is complete, including the three
    database kinds, whose bodies run the operation their spec declares.

    Raises:
        ValueError: If the node is a database node with no spec, or one whose
            input port type the binding rules cannot supply. Both are Phase 1
            findings, so reaching here means a renderer was called on an
            unvalidated document -- and a database node with no spec is not a
            finding about a *tool entry* but about the node itself, which is why
            it is refused rather than skipped.
    """
    in_ann = PORT_TYPE_ANNOTATIONS[node.io.input_type]
    out_ann = PORT_TYPE_ANNOTATIONS[node.io.output_type]
    is_agent = node.kind == "agent"
    is_database = node.kind in DATABASE_KINDS
    is_orchestrator = is_agent and bool(node.agent and node.agent.delegates_to)
    # One read-only tool per `sql:<id>`-style entry, resolved once: the header
    # imports what the body calls, so both have to agree on the same set.
    database_tools = database_tool_targets(graph, node)
    needs_tool_import = is_orchestrator or bool(database_tools)

    # Same delimiter/escaping as the pydantic-graph step modules: an intent
    # holding a quote must not be able to terminate the docstring early.
    header = [
        f"{_STEP_DOCSTRING_DELIMITER}``{node.id}`` node: "
        f"{_docstring_body(node.intent)}{_STEP_DOCSTRING_DELIMITER}",
        "",
        "from __future__ import annotations",
        "",
        "import json",
        *(("from pathlib import Path",) if is_database else ()),
        "from typing import Any",
        "",
        imports_marker_begin(node.id),
    ]
    if is_agent:
        header.append("from langchain.messages import HumanMessage, SystemMessage")
    if needs_tool_import:
        header.append("from langchain.tools import tool")
    header.append(imports_marker_end(node.id))
    header.extend(
        [
            "",
            "from langchain_core.language_models import BaseChatModel",
            "from langgraph.runtime import Runtime",
            "",
            f"from {PACKAGE_NAME}.context import Context",
        ]
    )
    if is_database:
        header.extend(_database_import_lines(node))
    header.append(f"from {PACKAGE_NAME}.state import State")
    if is_orchestrator and node.agent is not None:
        for child in node.agent.delegates_to:
            header.append(f"from {PACKAGE_NAME}.nodes.{child} import {child}_body")
    for database_node in database_tools:
        header.append(
            f"from {PACKAGE_NAME}.nodes.{database_node.id} import {database_node.id}_body"
        )
    if needs_tool_import:
        header.extend(["", "MAX_TOOL_ROUNDS = 6"])
    if is_database:
        # The declared operation and seed path, outside the body markers.
        header.append("")
        header.extend(_database_constant_lines(node))
    header.extend(
        [
            "",
            "",
            "def _parse_json_reply(text: str) -> dict[str, Any]:",
            '    """Best-effort JSON object from a model reply."""',
            "    try:",
            "        value = json.loads(text)",
            "    except ValueError:",
            '        return {"text": text}',
            '    return value if isinstance(value, dict) else {"value": value}',
        ]
    )
    if is_agent and node.io.output_type == "list[json]":
        header.extend(_parse_json_rows_helper_lines())
    header.extend(
        [
            "",
            "",
            f"async def {node.id}_body(",
            f"    inputs: {in_ann}, state: State, model: BaseChatModel, writes: dict[str, Any]",
            f") -> {out_ann}:",
            '    """Compute this node\'s output. ``inputs`` is the previous node\'s output.',
            "",
            '    Read state with ``state["<field>"]``; write it with',
            '    ``writes["<field>"] = value``.',
            "    Call the model with ``await model.ainvoke([...])`` and read ``reply.text``.",
            '    """',
            f"    {body_marker_begin(node.id)}",
        ]
    )
    if node.kind == "programmatic":
        body = [f"    {UNCONVERTED_BODY_SENTINEL}"]
    elif is_database:
        body = _database_node_body(node)
    elif is_agent:
        body = _default_agent_body(graph, node, database_tools)
    elif node.kind == "join":
        body = [
            "    # Fan-in: the arms already reduced into this join's channel.",
            f'    output = state.get("{join_inbox_key(node.id)}")',
            "    return output  # type: ignore[return-value]",
        ]
    else:  # decision: pass-through; routing is route_<id> below
        body = ["    return inputs  # type: ignore[return-value]"]
    footer = [
        f"    {body_marker_end(node.id)}",
        "",
        "",
        f"async def {node.id}(state: State, runtime: Runtime[Context]) -> dict[str, Any]:",
        '    """Generated LangGraph node wrapper; edit the body function above instead."""',
        "    writes: dict[str, Any] = {}",
        f"    output = await {node.id}_body(",
        f'        state.get("{PAYLOAD_KEY}"), state, runtime.context.model, writes',
        "    )",
        _return_line(graph, node),
    ]
    if node.kind == "decision":
        footer.extend(_decision_lines(node))
    footer.append("")
    return "\n".join([*header, *body, *footer])


# ---------------------------------------------------------------------------
# validate/dry_run.py and the golden
# ---------------------------------------------------------------------------


def fake_reply_for(graph: SwarmGraph) -> str:
    """The fake model's canned reply: a decision's first match value when an
    agent node feeds that decision, else :data:`DEFAULT_FAKE_REPLY`."""
    node_by_id = {n.id: n for n in graph.nodes}
    for edge in graph.edges:
        source = node_by_id.get(edge.source)
        target = node_by_id.get(edge.target)
        if (
            source is not None
            and source.kind == "agent"
            and target is not None
            and target.kind == "decision"
            and target.decision is not None
            and target.decision.branches
        ):
            return target.decision.branches[0].match
    return DEFAULT_FAKE_REPLY


def _render_dry_run(graph: SwarmGraph, structure: GraphStructure) -> str:
    graph_node_ids = sorted(
        n.id for n in graph.nodes if n.id not in structure.delegate_only_node_ids
    )
    expected = ", ".join(f'"{node_id}"' for node_id in graph_node_ids)
    entry = structure.node_by_id[graph.entry_node_id]
    exit_node = structure.node_by_id[graph.exit_node_id]
    sample_input = PORT_TYPE_SAMPLE_INPUT[entry.io.input_type]
    origin = PORT_TYPE_RUNTIME_ORIGIN[exit_node.io.output_type]
    reply = json.dumps(fake_reply_for(graph))
    # Gated on the document using a database node: a project without one gets
    # exactly the file it got before the three kinds existed -- no extra import,
    # no extra function, no extra check in main().
    database_check = _database_mode_check_lines(graph)
    lines = [
        '"""Validation gate for the LangGraph export.',
        "",
        "Asserts, in order: the Mermaid rendering equals the golden captured at",
        "scaffold time; the compiled graph's node set equals the canvas node set;",
        "and ``ainvoke`` completes with a keyless fake model, producing the",
        "declared output type.",
    ]
    if database_check:
        lines.extend(
            [
                "A document that uses a database node asserts one more thing first:",
                "``SWARM_DB_MODE`` selects the seeded in-memory mock, so this gate",
                "needs no credentials and no driver -- the same assertion the",
                "pydantic-graph target's own dry run makes.",
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
            "from langchain_core.language_models import FakeListChatModel",
            "",
            f"from {PACKAGE_NAME}.context import Context",
            f"from {PACKAGE_NAME}.graph import graph",
            *((
                f"from {PACKAGE_NAME}.repositories import MOCK, db_mode",
            ) if database_check else ()),
            f"from {PACKAGE_NAME}.state import initial_state",
            "",
            "",
            "class KeylessFakeChatModel(FakeListChatModel):",
            '    """FakeListChatModel plus a no-op bind_tools, so orchestrator nodes run."""',
            "",
            "    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003",
            "        return self",
            "",
            "",
            "GOLDEN_PATH = Path(__file__).parent / 'golden_mermaid.txt'",
            f"EXPECTED_NODES = {{'__start__', '__end__', {expected}}}",
            f"EXIT_NODE = {graph.exit_node_id!r}",
            f"FAKE_REPLY = {reply}",
            *database_check,
            "",
            "",
            "def check_render() -> None:",
            "    golden = GOLDEN_PATH.read_text()",
            "    actual = graph.get_graph().draw_mermaid()",
            "    assert actual == golden, (",
            "        f'draw_mermaid() drifted from golden.\\n--- golden ---\\n{golden}\\n'",
            "        f'--- actual ---\\n{actual}'",
            "    )",
            "    print('OK draw_mermaid() matches golden')",
            "",
            "",
            "def check_nodes() -> None:",
            "    actual = set(graph.get_graph().nodes)",
            "    assert actual == EXPECTED_NODES, f'nodes {actual} != expected {EXPECTED_NODES}'",
            "    print(f'OK graph nodes == {sorted(actual)}')",
            "",
            "",
            "async def check_run() -> None:",
            "    context = Context(model=KeylessFakeChatModel(responses=[FAKE_REPLY]))",
            "    ran: list[str] = []",
            "    out: dict = {}",
            "    async for update in graph.astream(",
            f"        initial_state({sample_input}),",
            "        context=context,",
            "        stream_mode=['updates', 'values'],",
            "    ):",
            "        mode, chunk = update",
            "        if mode == 'updates':",
            "            ran.extend(chunk.keys())",
            "        else:",
            "            out = chunk",
            "    assert EXIT_NODE in ran, f'exit node {EXIT_NODE!r} never ran; executed: {ran}'",
            f'    payload = out["{PAYLOAD_KEY}"]',
            f"    assert isinstance(payload, {origin}), (",
            f"        f'expected {origin} output, got {{type(payload)}}: {{payload!r}}'",
            "    )",
            "    print(f'OK graph ran {ran} and returned: {payload!r}')",
            "",
            "",
            "def main() -> None:",
            *(("    check_db_mode()",) if database_check else ()),
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


def _database_mode_check_lines(graph: SwarmGraph) -> list[str]:
    """The gated mock-mode assertion ``validate/dry_run.py`` carries.

    Empty for a document with no database node, which is what keeps that file
    byte-identical to the pre-database one.

    The same assertion, in the same words, that the pydantic-graph target's dry
    run makes: ``SWARM_DB_MODE`` selects the repository implementation for every
    database node in the project, and this gate has no credentials, no DSN and no
    driver installed. A value that selects *live* -- or one the factory refuses
    as unrecognized, which raises -- therefore has to fail here, before a step
    reads anything, rather than reaching a real database or failing for a reason
    that has nothing to do with the generated code.
    """
    if not used_db_kinds(graph):
        return []
    return [
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


def _capture_golden_mermaid(project_dir: Path) -> str:
    """Capture ``graph.get_graph().draw_mermaid()`` from the project just written.

    Runs in a subprocess on the server's own interpreter (which has
    ``langgraph`` and ``langchain`` installed) so repeated scaffolds cannot
    collide in ``sys.modules``. Provider packages are not needed: the
    generated ``context.py`` imports them lazily.
    """
    script = (
        f"import {PACKAGE_NAME}.graph as g; import sys; "
        "sys.stdout.write(g.graph.get_graph().draw_mermaid())"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_dir,
        env={"PYTHONPATH": str(project_dir / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"golden-mermaid subprocess failed (exit {result.returncode}):\n{result.stderr}"
        )
    return result.stdout


__all__ = [
    "DEFAULT_FAKE_REPLY",
    "UNCONVERTED_BODY_SENTINEL",
    "LangGraphScaffoldResult",
    "emit_langgraph",
    "fake_reply_for",
    "join_inbox_key",
    "render_node_module",
    "scaffold_langgraph",
]
