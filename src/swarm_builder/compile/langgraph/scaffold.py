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
from swarm_builder.models import PORT_TYPE_ANNOTATIONS, SwarmGraph, SwarmNode
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
    write(".env.example", "\n".join(model_source.env_lines) + "\n")
    write(f"src/{PACKAGE_NAME}/__init__.py", _render_package_init())
    write(f"src/{PACKAGE_NAME}/nodes/__init__.py", _render_package_init())
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
    return (
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


def _render_readme(graph: SwarmGraph, model_source: LangChainModelSource) -> str:
    return (
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


def _default_agent_body(graph: SwarmGraph, node: SwarmNode) -> list[str]:
    """Deterministic agent body: one chat call, output coerced to the port type.

    An orchestrator additionally wraps each delegated child as a ``@tool``
    and runs a bounded tool loop -- pure LangChain model/tool primitives,
    no ``create_agent``/``create_react_agent``.
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
        tools = ", ".join(delegates)
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


def _coerce_expression(port_type: str) -> str:
    """Turn ``reply.text`` into the declared output port type, safely."""
    if port_type == "json":
        return "_parse_json_reply(reply.text)"
    if port_type == "list[str]":
        return "[line.strip() for line in reply.text.splitlines() if line.strip()]"
    return "reply.text"


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
    ``lg_convert`` to replace; every other kind is complete.
    """
    in_ann = PORT_TYPE_ANNOTATIONS[node.io.input_type]
    out_ann = PORT_TYPE_ANNOTATIONS[node.io.output_type]
    is_agent = node.kind == "agent"
    is_orchestrator = is_agent and bool(node.agent and node.agent.delegates_to)

    # Same delimiter/escaping as the pydantic-graph step modules: an intent
    # holding a quote must not be able to terminate the docstring early.
    header = [
        f"{_STEP_DOCSTRING_DELIMITER}``{node.id}`` node: "
        f"{_docstring_body(node.intent)}{_STEP_DOCSTRING_DELIMITER}",
        "",
        "from __future__ import annotations",
        "",
        "import json",
        "from typing import Any",
        "",
        imports_marker_begin(node.id),
    ]
    if is_agent:
        header.append("from langchain.messages import HumanMessage, SystemMessage")
        if is_orchestrator:
            header.append("from langchain.tools import tool")
    header.append(imports_marker_end(node.id))
    header.extend(
        [
            "",
            "from langchain_core.language_models import BaseChatModel",
            "from langgraph.runtime import Runtime",
            "",
            f"from {PACKAGE_NAME}.context import Context",
            f"from {PACKAGE_NAME}.state import State",
        ]
    )
    if is_orchestrator and node.agent is not None:
        for child in node.agent.delegates_to:
            header.append(f"from {PACKAGE_NAME}.nodes.{child} import {child}_body")
    if is_orchestrator:
        header.extend(["", "MAX_TOOL_ROUNDS = 6"])
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
    elif is_agent:
        body = _default_agent_body(graph, node)
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
    return "\n".join(
        [
            '"""Validation gate for the LangGraph export.',
            "",
            "Asserts, in order: the Mermaid rendering equals the golden captured at",
            "scaffold time; the compiled graph's node set equals the canvas node set;",
            "and ``ainvoke`` completes with a keyless fake model, producing the",
            "declared output type.",
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
