"""Phase 7: the conversion agent (pydantic-graph step bodies -> LangGraph node bodies).

The agent gets the same confined toolset as the fill agent -- ``read_file``
and ``write_region``/``parse_check`` over the LangGraph project -- plus one
read-only tool, ``read_source_file``, over the *validated* pydantic-graph
project it converts from. Its job is deliberately narrow: for each
``programmatic`` node, read ``src/swarm_workflow/steps/<id>.py`` (a body
that already passed Phase 5) and write the equivalent body into
``src/swarm_workflow_lg/nodes/<id>.py``. Agent, decision and join modules
are complete deterministic templates; the prompt says so and the tools
refuse anything outside ``nodes/``.

The prompt states the LangGraph/LangChain contract for the pinned
versions (``langgraph`` 1.2.12, ``langchain`` 1.4.2) and the translation
table from pydantic-graph idioms, and asks for pure LangGraph with
LangChain used only where a model, message or tool is involved -- no
``langchain.agents``, no ``create_react_agent``, no LCEL chains.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model

from swarm_builder.compile.agent import (
    AGENT_RETRIES,
    STEPS_DIR_PARTS,
    FillError,
    FillSession,
    _graph_shape_lines,
    _model_and_description,
    _port,
    _state_lines,
)
from swarm_builder.compile.fake_fill import FILLABLE_NODE_KINDS
from swarm_builder.compile.langgraph import (
    FORBIDDEN_DIRECTORIES,
    FORBIDDEN_FILES,
    NODES_DIR_PARTS,
    PACKAGE_NAME,
    PAYLOAD_KEY,
    PINNED_LANGCHAIN_VERSION,
    PINNED_LANGGRAPH_VERSION,
)
from swarm_builder.compile.langgraph.fake_convert import apply_fake_convert
from swarm_builder.compile.pipeline import FillResult
from swarm_builder.models import SwarmGraph, SwarmNode

logger = logging.getLogger(__name__)

#: Request/tool-call bounds for one conversion run. Each node needs about
#: one read of the source, one read of the target, one write and one
#: parse check.
REQUEST_LIMIT = 60
TOOL_CALLS_LIMIT = 120


def _convert_targets(graph: SwarmGraph) -> list[SwarmNode]:
    return [node for node in graph.nodes if node.kind in FILLABLE_NODE_KINDS]


def _source_file(node: SwarmNode) -> str:
    return "/".join((*STEPS_DIR_PARTS, f"{node.id}.py"))


def _target_file(node: SwarmNode) -> str:
    return "/".join((*NODES_DIR_PARTS, f"{node.id}.py"))


def _describe_target(graph: SwarmGraph, node: SwarmNode) -> str:
    node_by_id = {n.id: n for n in graph.nodes}
    lines = [
        f"- source (read with read_source_file): {_source_file(node)}",
        f"  target (edit with write_region):     {_target_file(node)}",
        f"  node id: {node.id}",
        f"  intent: {node.intent}",
        f"  input:  {_port(node, 'in')}",
        f"  output: {_port(node, 'out')}",
    ]
    if node.reads:
        lines.append(f"  reads state fields: {', '.join(node.reads)}")
    if node.writes:
        lines.append(f"  MUST set writes[...] for: {', '.join(node.writes)}")
    for edge in graph.edges:
        if edge.source == node.id:
            target = node_by_id.get(edge.target)
            if target is not None and target.decision is not None:
                matches = ", ".join(repr(b.match) for b in target.decision.branches)
                lines.append(
                    f"  feeds decision {target.id!r}: the return value MUST be one of {matches}"
                )
    return "\n".join(lines)


def build_convert_instructions(
    project_dir: Path,
    source_dir: Path,
    graph: SwarmGraph,
    *,
    model_description: str,
    previous_failure: str | None = None,
) -> str:
    """Assemble the conversion agent's instruction text (pure)."""
    targets = _convert_targets(graph)
    example_id = targets[0].id if targets else "<node_id>"
    sections = [
        "You are converting a validated PydanticAI / pydantic-graph workflow project "
        "into a LangGraph project, node by node.\n"
        f"The source (read-only) project is {source_dir}.\n"
        f"The target LangGraph project is {project_dir}.\n"
        f"{model_description}\n"
        "The target's wiring (graph.py), state (state.py), model context (context.py), "
        "every agent/decision/join node module, and the validation gate are already "
        "generated and correct. Your ONLY job is to translate the body of each "
        "programmatic step listed below from the source project into the marked body "
        "region of the matching target node module.",
        "## Your tools\n\n"
        "- read_source_file(name): read one file of the SOURCE project (project-relative, "
        f"e.g. {'/'.join(STEPS_DIR_PARTS)}/{example_id}.py). Read-only.\n"
        "- read_file(name): read one file of the TARGET project (project-relative, e.g. "
        f"{'/'.join(NODES_DIR_PARTS)}/{example_id}.py).\n"
        "- write_region(name, node_id, region, body): the ONLY way to change anything. "
        "It replaces one marker region (`imports` or `body`) of one target node module.\n"
        "- parse_check(names=None): parse the target node modules and report syntax "
        "errors. It never runs code.\n\n"
        "Paths outside either project are refused. You cannot create, delete or rename "
        "files, install packages, or reach the network.",
        "## Files you may edit\n\n"
        f"Only {'/'.join(NODES_DIR_PARTS)}/<node_id>.py, and only its two marker regions:\n"
        "- `imports` (between `# --- swarm:imports <node_id> ---` and "
        "`# --- swarm:end-imports <node_id> ---`): module-level imports your body needs.\n"
        "- `body` (between the indented `# --- swarm:begin <node_id> ---` and "
        "`# --- swarm:end <node_id> ---`): the body of\n"
        "  `async def <node_id>_body(inputs, state, model, writes) -> <output annotation>`.\n"
        "  Every body line must already be indented four spaces.\n\n"
        f"Never edit {', '.join(FORBIDDEN_FILES)}, anything under "
        f"{', '.join(d + '/' for d in FORBIDDEN_DIRECTORIES)}, any __init__.py, or any "
        "agent/decision/join node module -- those are complete.",
        "## The target contract (LangGraph "
        f"{PINNED_LANGGRAPH_VERSION}, LangChain {PINNED_LANGCHAIN_VERSION})\n\n"
        "Prefer pure LangGraph. LangChain appears only where a model, a message or a "
        "tool is involved. Concretely:\n"
        "1. `inputs` is the previous node's output (the pydantic-graph `ctx.inputs`).\n"
        f'2. Read a state field with `state["<field>"]` (or `state.get("<field>")`); '
        'set one with `writes["<field>"] = value`. Never assign into `state` directly '
        "and never return a dict -- the generated wrapper builds the state update from "
        "your return value and `writes`.\n"
        "3. `model` is a LangChain `BaseChatModel`. If the source body ran a PydanticAI "
        "`Agent`, translate it to `reply = await model.ainvoke([SystemMessage(...), "
        "HumanMessage(...)])` and use `reply.text`; import messages from "
        "`langchain.messages`. Do not import `pydantic_ai` anywhere.\n"
        "4. Tools, if the source used them, are LangChain `@tool` functions from "
        "`langchain.tools` bound with `model.bind_tools([...])` and executed by "
        "iterating `reply.tool_calls` and calling `await tool.ainvoke(call)`. Do not "
        "use `langchain.agents`, `create_agent`, `create_react_agent`, or LCEL chains.\n"
        "5. `return` a value of exactly the declared output annotation. Do not change "
        "the signature, the wrapper below it, or the markers.\n"
        "6. If the source body feeds a decision, it returns one of that decision's match "
        "values; keep that exactly -- the target's routing function compares "
        f'`state["{PAYLOAD_KEY}"]` against the same strings.\n'
        "7. Keep the source body's behaviour and its standard-library imports; put any "
        "import in the `imports` region, not in the body.",
        "## The whole graph\n\n" + "\n".join(_graph_shape_lines(graph)),
        "\n".join(_state_lines(graph)).replace("State dataclass", "State TypedDict"),
    ]
    if targets:
        rendered = "\n".join(_describe_target(graph, node) for node in targets)
        sections.append(
            f"## Nodes to convert ({len(targets)})\n\n"
            "For each: read the source step module, read the target node module, "
            "translate the body, write it with write_region, then parse_check.\n\n"
            f"{rendered}"
        )
    else:
        sections.append(
            "## Nodes to convert (0)\n\nEvery node module is already complete. Verify by "
            "reading one target module, then reply that there is nothing to convert."
        )
    sections.append(
        "## How to finish\n\n"
        "When every listed node is converted and parse_check reports no failures, reply "
        "with a one-line summary of what you converted."
    )
    if previous_failure is not None:
        sections.append(
            "## The previous attempt failed -- fix this\n\n"
            "The converted project failed its own checks. Your earlier edits are still on "
            "disk. The failure text follows; correct the bodies and verify again.\n\n"
            f"```\n{previous_failure}\n```"
        )
    return "\n\n".join(sections)


def build_convert_agent(
    session: FillSession, *, model: Model | str, instructions: str
) -> Agent[None, str]:
    """Bind the four confined tools to a fresh agent (never at import time)."""
    agent: Agent[None, str] = Agent(
        model, deps_type=type(None), instructions=instructions, retries=AGENT_RETRIES
    )
    agent.tool_plain(session.read_source_file)
    agent.tool_plain(session.read_file)
    agent.tool_plain(session.write_region)
    agent.tool_plain(session.parse_check)
    return agent


#: Test seam: swap the agent factory for a scripted one.
ConvertAgentBuilder = Callable[..., Agent[None, str]]


async def convert(
    *,
    project_dir: Path,
    source_dir: Path,
    graph: SwarmGraph,
    live_model: object | None,
    previous_failure: str | None,
    agent_builder: ConvertAgentBuilder | None = None,
) -> FillResult:
    """Run one conversion attempt.

    Args:
        project_dir: The scaffolded LangGraph project.
        source_dir: The validated pydantic-graph project.
        graph: The canvas document both were generated from.
        live_model: A ``LiveModel`` (its ``.model`` runs the agent).
        previous_failure: Phase-8/9 failure text on the single retry.
        agent_builder: Test seam; defaults to :func:`build_convert_agent`.

    Returns:
        The converted node ids.

    Raises:
        FillError: No model, usage limit hit, or nothing converted when
            something needed converting.
    """
    if live_model is None:
        raise FillError("the conversion agent needs a live model")
    model, description = _model_and_description(live_model)
    session = FillSession(
        project_dir,
        editable_dir_parts=NODES_DIR_PARTS,
        forbidden_files=FORBIDDEN_FILES,
        forbidden_directories=FORBIDDEN_DIRECTORIES,
        source_root=source_dir,
    )
    instructions = build_convert_instructions(
        project_dir,
        source_dir,
        graph,
        model_description=description,
        previous_failure=previous_failure,
    )
    builder = agent_builder if agent_builder is not None else build_convert_agent
    agent = builder(session, model=model, instructions=instructions)
    logger.info(
        "lg_convert: %d node(s) to convert into %s", len(_convert_targets(graph)), PACKAGE_NAME
    )
    try:
        result = await agent.run(
            instructions,
            usage_limits=UsageLimits(
                request_limit=REQUEST_LIMIT, tool_calls_limit=TOOL_CALLS_LIMIT
            ),
        )
    except UsageLimitExceeded as exc:
        raise FillError(f"conversion run exceeded its usage limit: {exc}") from exc
    if not session.written_node_ids and _convert_targets(graph):
        raise FillError(
            f"the conversion agent finished without writing any region (output: {result.output!r})"
        )
    return FillResult(filled_node_ids=tuple(session.written_node_ids))


async def fake_convert(
    *,
    project_dir: Path,
    source_dir: Path,
    graph: SwarmGraph,
    live_model: object | None,
    previous_failure: str | None,
) -> FillResult:
    """The ``SWARM_FAKE_FILL=1`` stand-in: deterministic stub bodies."""
    del source_dir, live_model, previous_failure
    return FillResult(filled_node_ids=apply_fake_convert(project_dir, graph))


__all__ = [
    "REQUEST_LIMIT",
    "TOOL_CALLS_LIMIT",
    "build_convert_agent",
    "build_convert_instructions",
    "convert",
    "fake_convert",
]
