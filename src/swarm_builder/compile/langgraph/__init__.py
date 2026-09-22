"""The LangGraph compile target (``PLAN-V2-FEATURES.md``, Feature 3).

A LangGraph export is produced *from* the validated pydantic-graph
project, at compile time, by four extra phases that run after the five
standard ones when a compile is started with ``target="langgraph"``:

6. ``lg_scaffold`` -- deterministic. Emits a complete LangGraph project
   (``pyproject.toml`` with pinned ``langgraph``/``langchain`` and the
   provider package the inherited route needs, ``state.py`` as a
   ``TypedDict`` with one reducer channel per join, ``context.py`` with a
   ``Runtime`` context carrying the LangChain chat model, ``graph.py`` with
   the ``StateGraph`` wiring, one ``nodes/<id>.py`` shell per canvas node,
   and ``validate/dry_run.py`` plus the Mermaid golden), then captures the
   boundary baseline.
7. ``lg_convert`` -- the only model call. A conversion agent reads each
   filled ``steps/<id>.py`` of the pydantic-graph project and writes the
   equivalent LangGraph node body into the marker region of
   ``nodes/<id>.py``. Only ``programmatic`` bodies need converting: agent,
   decision and join nodes are complete deterministic templates.
8. ``lg_boundary`` -- the same two-tier check as Phase 4, over ``nodes/``.
9. ``lg_validate`` -- ``uv sync``, keyless import, and the project's own
   dry run against a keyless fake chat model, asserting the Mermaid golden,
   the node set, and a successful ``ainvoke``.

The pinned versions below were resolved with ``uv`` on Python 3.14 on
2026-09-22 (``spike/langgraph_probe``) and are the versions the spike's
hand-written project was validated against. LangGraph 1.x froze the core
graph API at 1.0 (October 2025), and ``langchain`` 1.x re-exports models,
messages and tools under the ``langchain.*`` namespace, which is what the
generated code imports.
"""

from __future__ import annotations

#: Pinned versions for the generated LangGraph project.
PINNED_LANGGRAPH_VERSION = "1.2.12"
PINNED_LANGCHAIN_VERSION = "1.4.2"
PINNED_LANGCHAIN_CORE_VERSION = "1.6.4"

#: The generated package name. Distinct from the pydantic-graph project's
#: ``swarm_workflow`` so both can be installed side by side.
PACKAGE_NAME = "swarm_workflow_lg"

#: Project-relative directory of the editable node modules.
NODES_DIR_PARTS: tuple[str, ...] = ("src", PACKAGE_NAME, "nodes")

#: Files/directories the conversion agent must never touch (restated in
#: the prompt; the tools are what enforce it).
FORBIDDEN_FILES: tuple[str, ...] = ("graph.py", "state.py", "context.py", "pyproject.toml")
FORBIDDEN_DIRECTORIES: tuple[str, ...] = ("validate",)

#: The state key carrying the value that flows between nodes -- the
#: LangGraph counterpart of pydantic-graph's ``ctx.inputs``.
PAYLOAD_KEY = "payload"

#: Suffix of the reducer channel a fan-out's arms write into for a join.
JOIN_INBOX_SUFFIX = "_inbox"

#: The keyless import Phase 9 proves.
KEYLESS_IMPORT_SNIPPET = f"import {PACKAGE_NAME}.graph"

#: Phase slugs, continuing the standard five.
PHASE_LG_SCAFFOLD = "lg_scaffold"
PHASE_LG_CONVERT = "lg_convert"
PHASE_LG_BOUNDARY = "lg_boundary"
PHASE_LG_VALIDATE = "lg_validate"
LANGGRAPH_PHASE_NAMES: tuple[str, ...] = (
    PHASE_LG_SCAFFOLD,
    PHASE_LG_CONVERT,
    PHASE_LG_BOUNDARY,
    PHASE_LG_VALIDATE,
)

#: Compile targets a ``POST /api/compile`` may name.
TARGET_PYDANTIC_GRAPH = "pydantic-graph"
TARGET_LANGGRAPH = "langgraph"
COMPILE_TARGETS: tuple[str, ...] = (TARGET_PYDANTIC_GRAPH, TARGET_LANGGRAPH)

__all__ = [
    "COMPILE_TARGETS",
    "FORBIDDEN_DIRECTORIES",
    "FORBIDDEN_FILES",
    "JOIN_INBOX_SUFFIX",
    "KEYLESS_IMPORT_SNIPPET",
    "LANGGRAPH_PHASE_NAMES",
    "NODES_DIR_PARTS",
    "PACKAGE_NAME",
    "PAYLOAD_KEY",
    "PHASE_LG_BOUNDARY",
    "PHASE_LG_CONVERT",
    "PHASE_LG_SCAFFOLD",
    "PHASE_LG_VALIDATE",
    "PINNED_LANGCHAIN_CORE_VERSION",
    "PINNED_LANGCHAIN_VERSION",
    "PINNED_LANGGRAPH_VERSION",
    "TARGET_LANGGRAPH",
    "TARGET_PYDANTIC_GRAPH",
]
