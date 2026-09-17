"""Phase 3 fill agent -- the real, model-backed filler (PLAN.md "Phase 3
-- Fill (in-process PydanticAI agent)", Group 5).

One in-process PydanticAI :class:`~pydantic_ai.Agent` run *inside the
server process*, constructed from the resolved route's
:class:`~swarm_builder.inherit.routes.LiveModel` (facts 24-26: no
subprocess, no JSON-RPC, no harness, no ``dsh``). Phase 2's
``scaffold.py`` already renders complete bodies for ``agent`` nodes, so
the only genuinely unfilled bodies are the ``programmatic``,
``decision``, and ``join`` step bodies it emits as
``raise NotImplementedError(...)`` -- exactly the set
:data:`~swarm_builder.compile.fake_fill.FILLABLE_NODE_KINDS` names for
the ``SWARM_FAKE_FILL=1`` stub.

**The security invariant (PLAN.md: "the one security-relevant invariant
in v1").** The agent gets exactly three tools and nothing else: no
general-purpose file write, no shell, no network access of its own.

* :meth:`FillSession.read_file` -- read one file inside the project.
* :meth:`FillSession.write_region` -- the **only** mutator: replace the
  text of one ``imports`` or ``body`` marker region of
  ``src/swarm_workflow/steps/<node_id>.py``.
* :meth:`FillSession.parse_check` -- parse the generated
  project to self-verify.

Every path passes through
:func:`~swarm_builder.compile.confine.PathEscapesRootError`'s guard --
:func:`~swarm_builder.compile.confine.confine_path`, which resolves the
candidate and verifies containment *before any I/O* (fact 26) -- so
confinement is enforced in the tool, never merely requested in the
prompt. Because the only mutator is marker-scoped, PLAN.md's Phase-4
check stays cheap: every byte outside the two regions is provably
untouched, and :mod:`swarm_builder.compile.boundary` re-verifies it
independently.

**Underspecified in PLAN.md, resolved here:**

* **No tool executes model-authored code.** ``parse_check`` reads the
  step modules and runs :func:`ast.parse` in this process, so the
  agent's filesystem reach really is the project directory (PLAN.md
  assumption 9). An earlier version ran arbitrary snippets through a
  subprocess, which defeated that invariant: a snippet could write
  outside the project, where Phase 4 cannot see it.
* **Tools hand errors back with :class:`~pydantic_ai.ModelRetry`, not by
  raising.** Verified against the installed 2.43.0 wheel: a raw
  exception raised inside a tool propagates out of ``Agent.run``
  entirely, so a tool that raises is an immediate *compile failure*
  rather than something the agent can react to. ``ModelRetry`` is what
  produces the "tool returns the error to the agent" behavior PLAN.md's
  failure-mode table requires, and it is what ``Agent(retries=...)``
  budgets. Note that ``retries`` is the agent-level default; each tool
  keeps pydantic-ai's own per-tool budget unless `retries=` is passed to
  ``tool_plain`` as well.
* **``reasoning_effort`` from the resolved route is *not* forwarded.**
  The installed 2.43.0 ``ModelSettings`` has no ``reasoning_effort``
  field (only ``thinking``), so forwarding the route's value would fail
  or silently set nothing. The route still reaches the prompt via
  ``source_description``.
* **No progress event seam exists.** PLAN.md Group 5 mentions streaming
  tool calls and messages "as coarse phases", but the ``Filler``
  protocol takes no event callback and ``run_pipeline`` emits only its
  own per-phase events, so this module reports coarse progress through
  :mod:`logging` ("fill: wrote body region ...", "parse_check parsed 2 module(s)").
  Wiring it into the SSE stream would need a protocol change owned by
  the pipeline, not by this module.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model

from swarm_builder.compile import (
    ResolvedModel,
    body_marker_begin,
    body_marker_end,
    imports_marker_begin,
    imports_marker_end,
)
from swarm_builder.compile.confine import PathEscapesRootError, confine_path
from swarm_builder.compile.fake_fill import FILLABLE_NODE_KINDS as _FILLABLE_NODE_KINDS
from swarm_builder.compile.pipeline import FillResult
from swarm_builder.models import PORT_TYPE_ANNOTATIONS, SwarmGraph, SwarmNode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounds (PLAN.md fact 26: "Bounds come from PydanticAI directly")
# ---------------------------------------------------------------------------

#: ``Agent(retries=...)`` -- how many times a single tool call may fail
#: validation or raise :class:`~pydantic_ai.ModelRetry` before the run
#: itself fails. Covers ``llm-retry``'s job for the tool-call layer.
AGENT_RETRIES: int = 3

#: Maximum model requests (i.e. provider round trips) for one fill run.
#: This is the primary spend bound: a graph with N fillable nodes needs
#: roughly 3N requests, so a 40-node graph fits with headroom.
REQUEST_LIMIT: int = 80

#: Maximum tool calls for one fill run. The agent needs one ``read_file``
#: plus one ``write_region`` per fillable node, plus optional
#: ``parse_check`` calls; this is request-limit headroom expressed at the
#: tool layer.
TOOL_CALLS_LIMIT: int = 120

#: Token caps, deliberately left ``None``: the prompt legitimately grows
#: with graph size (PLAN.md edge case ">40 nodes"), so a token cap would
#: truncate a *large-but-valid* graph rather than a runaway one, while
#: request/tool-call counts bound the runaway case exactly.
INPUT_TOKENS_LIMIT: int | None = None
OUTPUT_TOKENS_LIMIT: int | None = None
TOTAL_TOKENS_LIMIT: int | None = None

#: Monetary cap, also left ``None``: the installed 2.43.0
#: :class:`~pydantic_ai.usage.UsageLimits` *does* accept ``cost_limit``
#: (verified: it is the first field of the dataclass), but a positive
#: value can only be compared against usage a provider actually
#: reports, which is model- and price-table-dependent -- and a
#: mis-guessed cap would fail compiles for the wrong reason. The
#: request/tool-call caps are deterministic and provider-independent.
COST_LIMIT: object | None = None

#: Characters kept per ``parse_check`` output stream. Generous enough for
#: a full list of syntax errors, small enough that a project with many
#: broken modules cannot flood the model's context.
PARSE_CHECK_OUTPUT_LIMIT: int = 6000

#: The two regions ``write_region`` accepts. Anything else -- including
#: the marker names ``begin``/``end``/``end-imports`` -- is refused, so
#: the tool's vocabulary is exactly PLAN.md's.
REGION_IMPORTS = "imports"
REGION_BODY = "body"
VALID_REGIONS: frozenset[str] = frozenset({REGION_IMPORTS, REGION_BODY})

#: Project-relative directory holding every editable node module. The
#: ``agents/`` directory is deliberately *not* editable: ``scaffold.py``
#: renders complete agent factories there, so every region it can reach
#: is already filled (see ``fake_fill.py``'s module docstring).
STEPS_DIR_PARTS: tuple[str, ...] = ("src", "swarm_workflow", "steps")

#: Project-relative files the agent must never touch (PLAN.md Phase 3:
#: "the files it cannot touch"). Restated to the model for clarity; the
#: tools are what actually enforce it.
FORBIDDEN_FILES: tuple[str, ...] = (
    "graph.py",
    "state.py",
    "deps.py",
    "pyproject.toml",
)

#: Project-relative directories the agent must never touch.
FORBIDDEN_DIRECTORIES: tuple[str, ...] = ("validate",)

#: Node kinds whose step body is genuinely unfilled after Phase 2,
#: re-exported from ``fake_fill`` so the real and stub fillers can never
#: disagree about what needs filling.
#:
#: This module previously kept its own copy naming ``decision`` and
#: ``join`` as well, which was wrong: Phase 2 emits no ``steps/<id>.py``
#: for either kind (both are wired entirely in ``graph.py``). The stub
#: filler skipped the missing file, but the real agent instead put a
#: nonexistent path in the prompt, so the model burned retries on an
#: impossible edit -- and a graph of agents plus a decision failed Phase 3
#: outright, because that impossible edit was the *only* one requested.
FILLABLE_NODE_KINDS = _FILLABLE_NODE_KINDS


class FillError(RuntimeError):
    """Raised when a fill run cannot complete.

    Carries no partial state: the project directory is left exactly as
    the run left it, for Phase 4/the developer to inspect. The compile
    pipeline wraps this as a ``fill`` phase failure and does **not**
    retry it (only a phase-4/5 failure triggers the fill retry).
    """


class CheckOutcome(BaseModel):
    """What one :meth:`FillSession.parse_check` call produced.

    A non-zero exit is data, not an exception: the whole point of the
    ``parse_check`` tool is to let the agent see its own mistakes.
    """

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool


# ---------------------------------------------------------------------------
# Marker-region splice -- the single mutator behind write_region
# ---------------------------------------------------------------------------


def _region_marker_text(node_id: str, region: str) -> str:
    """Return the marker text that opens ``region`` for ``node_id``."""
    if region == REGION_IMPORTS:
        return imports_marker_begin(node_id)
    return body_marker_begin(node_id)


def _region_end_marker_text(node_id: str, region: str) -> str:
    """Return the marker text that closes ``region`` for ``node_id``."""
    if region == REGION_IMPORTS:
        return imports_marker_end(node_id)
    return body_marker_end(node_id)


class RegionNotFoundError(ValueError):
    """Raised when a node's marker region cannot be addressed unambiguously.

    A subclass of :class:`ValueError` because it is a *malformed input*
    condition rather than a confinement refusal, and
    :meth:`FillSession.write_region` turns it into a
    :class:`~pydantic_ai.ModelRetry` for the agent.
    """


@dataclass(frozen=True)
class RegionLocation:
    """Where one marker region sits in a module's lines.

    Attributes:
        first: Index of the opening marker line.
        last: Index of the closing marker line.
        indent: The opening marker's leading whitespace, preserved on
            the way back out (body markers are indented one level).
    """

    first: int
    last: int
    indent: str


def _find_marker_line(lines: Sequence[str], marker_text: str) -> int | None:
    """Return the index of the first line equal to ``marker_text``.

    Compares on the stripped line, exactly as
    :func:`swarm_builder.compile.boundary._find_marker_line` does, so the
    two modules agree about where a region begins and ends. Deliberately
    **not** a regular expression: a ``(?s)``/``re.DOTALL`` pattern of the
    shape ``begin\n.*?\nend`` was measured to fail to match the
    zero-content ``imports`` region -- two consecutive marker lines --
    under Python 3.14.5's ``re``, which silently made every
    ``imports``-region write impossible. Line arithmetic cannot have
    that class of bug.

    Args:
        lines: The module source, split with ``keepends=True``.
        marker_text: The marker line's exact text, unindented.

    Returns:
        The line index, or ``None`` when the marker is absent.
    """
    for index, line in enumerate(lines):
        if line.strip() == marker_text:
            return index
    return None


def _locate_region(text: str, node_id: str, region: str) -> RegionLocation:
    """Find ``region``'s marker block in ``text``.

    Args:
        text: Current source of the node module.
        node_id: Node whose marker region is wanted.
        region: ``"imports"`` or ``"body"``.

    Returns:
        The :class:`RegionLocation` of that region.

    Raises:
        RegionNotFoundError: If the region's markers are absent, out of
            order, or ambiguous -- which means either the node id is
            wrong for this file or Phase 2 emitted a module the fill
            stage cannot address.
    """
    begin = _region_marker_text(node_id, region)
    end = _region_end_marker_text(node_id, region)
    lines = text.splitlines(keepends=True)
    first = _find_marker_line(lines, begin)
    last = _find_marker_line(lines, end)
    if first is None or last is None or last <= first:
        raise RegionNotFoundError(
            f"no {region!r} marker region for node {node_id!r} in this file. "
            f"Expected a line reading {begin!r} followed later by {end!r}. "
            "Check the node id against the editable-files list in your instructions."
        )
    if _find_marker_line(lines[first + 1 : last], begin) is not None:
        raise RegionNotFoundError(
            f"the file for node {node_id!r} contains more than one {region!r} region; "
            "refusing to guess which one to replace."
        )
    begin_line = lines[first]
    indent = begin_line[: len(begin_line) - len(begin_line.lstrip())]
    return RegionLocation(first=first, last=last, indent=indent)


def splice_region(text: str, node_id: str, region: str, body: str) -> str:
    """Replace exactly one marker region, leaving every other line alone.

    The replacement re-emits the marker lines verbatim (with the
    indentation they already had) around ``body``, so every byte outside
    the two marker *lines* -- and the marker lines themselves -- is
    byte-identical by construction: the property ``boundary.py`` hashes
    and compares. ``body`` is emitted as its own lines with a trailing
    newline, so the closing marker keeps its own line.

    Args:
        text: Current source of the node module.
        node_id: Node whose region is replaced.
        region: ``"imports"`` or ``"body"``.
        body: Replacement region source, without a trailing newline.

    Returns:
        The module source with that one region replaced.

    Raises:
        RegionNotFoundError: If the region's markers are absent or
            ambiguous.
        ValueError: If ``body`` is empty/whitespace-only, which would
            leave the region empty and fail Phase 4's
            ``empty_body_region`` check.
    """
    if not body.strip():
        raise ValueError(
            f"refusing to write an empty {region!r} region for node {node_id!r}: "
            "Phase 4 rejects an empty body region"
        )
    location = _locate_region(text, node_id, region)
    lines = text.splitlines(keepends=True)
    begin_line = lines[location.first]
    end_line = lines[location.last]
    # Normalize the marker lines' own newlines so the region cannot
    # inherit a stray absence of one on a final line.
    if not begin_line.endswith("\n"):
        begin_line += "\n"
    if not end_line.endswith("\n"):
        end_line += "\n"
    replacement = [begin_line, *body.splitlines(keepends=True), end_line]
    # A body whose last line has no newline must not glue itself to the
    # closing marker.
    if not replacement[-2].endswith("\n"):
        replacement[-2] += "\n"
    return "".join([*lines[: location.first], *replacement, *lines[location.last + 1 :]])


# ---------------------------------------------------------------------------
# The fill session: three tools, one project, no escape hatch
# ---------------------------------------------------------------------------


class FillSession:
    """One fill attempt against one scaffolded project directory.

    Holds the only state the tools need and is constructed fresh per
    attempt, so a retry starts from whatever the previous attempt left
    on disk (PLAN.md retry policy: the retry appends the phase-4/5
    failure rather than rescaffolding).
    """

    def __init__(
        self, project_root: Path
    ) -> None:
        """Bind a session to a scaffolded project directory.

        Args:
            project_root: The project directory every tool path is
                confined to. Stored already resolved, so the guard is
                computed once and cannot drift mid-run.
        """
        self.project_root = project_root.resolve()
        self.written_node_ids: list[str] = []

    # -- confinement --------------------------------------------------

    def _resolve(self, name: str) -> Path:
        """Resolve ``name`` inside the project or refuse the call.

        Args:
            name: A project-relative (or absolute-inside-root) path.

        Returns:
            The resolved absolute path.

        Raises:
            ModelRetry: If the resolved path escapes
                :attr:`project_root` -- the guard runs before any read
                or write, so a traversal attempt touches nothing.
        """
        try:
            return confine_path(self.project_root, name)
        except PathEscapesRootError as exc:
            raise ModelRetry(
                f"{exc}. Every path must resolve inside the generated project "
                f"directory {self.project_root}."
            ) from exc

    def _editable_path(self, name: str) -> Path:
        """Confine ``name`` and verify it is an editable node module.

        Args:
            name: The file path the model asked for.

        Returns:
            The resolved path of an existing ``steps/<node_id>.py``.

        Raises:
            ModelRetry: If the path escapes the project, is not exactly
                ``src/swarm_workflow/steps/<node_id>.py``, or does not
                exist.
        """
        resolved = self._resolve(name)
        relative = resolved.relative_to(self.project_root)
        expected_parent = Path(*STEPS_DIR_PARTS)
        if relative.parent != expected_parent or relative.suffix != ".py":
            raise ModelRetry(
                f"{name!r} is not editable. The only editable files are "
                f"{'/'.join(STEPS_DIR_PARTS)}/<node_id>.py. Everything else -- "
                f"in particular {' and '.join(FORBIDDEN_FILES)}, everything under "
                f"{'/'.join(FORBIDDEN_DIRECTORIES)}/, and every agents/ module -- is off limits."
            )
        if not resolved.is_file():
            raise ModelRetry(f"{name!r} does not exist in this project.")
        return resolved

    # -- tool 1: read_file --------------------------------------------

    def read_file(self, name: str) -> str:
        """Read one file of the generated project.

        Args:
            name: Project-relative path, e.g.
                ``src/swarm_workflow/steps/intake.py``.

        Returns:
            The file's full text.

        Raises:
            ModelRetry: If the path escapes the project directory or is
                not a readable regular file.
        """
        resolved = self._resolve(name)
        if not resolved.is_file():
            raise ModelRetry(f"{name!r} is not a file in this project.")
        try:
            return resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ModelRetry(f"{name!r} is not UTF-8 text: {exc}") from exc

    # -- tool 2: write_region (the only mutator) -----------------------

    def write_region(self, name: str, node_id: str, region: str, body: str) -> str:
        """Replace ONE marker region of one step module.

        ``body`` replaces everything between the region's two marker
        lines. The marker lines themselves and every byte elsewhere in
        the file are preserved exactly, which is what Phase 4 verifies.

        Args:
            name: Project-relative path of the step module, e.g.
                ``src/swarm_workflow/steps/intake.py``.
            node_id: The node whose region is replaced; must match the
                module's ``<node_id>`` filename and its markers.
            region: ``"imports"`` (top-of-file import region) or
                ``"body"`` (the step function's body).
            body: The replacement source. For ``"body"`` every line must
                already be indented one level (4 spaces), because the
                region sits inside ``async def``.

        Returns:
            A short confirmation naming the file, node, and region.

        Raises:
            ModelRetry: If the path is not an editable step module, the
                region name is unknown, the markers are absent or
                ambiguous, the region already had content and ``body``
                is empty, or the write would leave the file unbalanced.
        """
        if region not in VALID_REGIONS:
            raise ModelRetry(
                f"unknown region {region!r}; expected one of "
                f"{', '.join(sorted(VALID_REGIONS))}"
            )
        resolved = self._editable_path(name)
        if resolved.stem != node_id:
            raise ModelRetry(
                f"node id {node_id!r} does not match file name {resolved.name!r}. "
                f"Write to src/swarm_workflow/steps/{node_id}.py instead."
            )

        original = resolved.read_text(encoding="utf-8")
        try:
            updated = splice_region(original, node_id, region, body)
        except (RegionNotFoundError, ValueError) as exc:
            # An absent marker or an empty replacement body is a model
            # mistake, so the agent gets the chance to correct its
            # arguments rather than failing the whole compile.
            raise ModelRetry(str(exc)) from exc

        resolved.write_text(updated, encoding="utf-8")
        if node_id not in self.written_node_ids:
            self.written_node_ids.append(node_id)
        logger.info(
            "fill: wrote %s region of node %r in %s (%d line(s))",
            region,
            node_id,
            name,
            len(body.splitlines()),
        )
        return f"replaced the {region} region for node {node_id!r} in {name}"

    # -- tool 3: parse_check ------------------------------------------

    def parse_check(self, names: list[str] | None = None) -> CheckOutcome:
        """Parse the filled step modules and report any syntax error.

        This is the agent's self-verification tool. It reads each module
        through the same confinement guard as every other tool and runs
        :func:`ast.parse` **in this process** -- it executes nothing, so
        it cannot write a file, install a package, or reach the network.

        That containment is the point. The tool this replaced ran
        arbitrary model-authored Python in a subprocess, which made the
        agent's filesystem reach the whole machine: a snippet calling
        ``pathlib.Path('/somewhere/else').write_text(...)`` succeeded,
        and a write *outside* the project is invisible to Phase 4, which
        only walks the project directory. Since the only verification the
        agent was ever instructed to perform was ``ast.parse`` over the
        modules it had just written, narrowing the tool to exactly that
        loses no real capability and makes the confinement invariant true
        by construction rather than by prompt instruction.

        Phase 5 remains what actually executes the built graph; a parse
        error is simply much cheaper to catch here than there.

        Args:
            names: Project-relative step-module paths to parse. Defaults
                to every module the fill targets, which is what the agent
                should check in practice.

        Returns:
            A :class:`CheckOutcome` whose ``exit_code`` is 0 when every
            module parsed and 1 otherwise, with the offending file, line
            and message on ``stderr``. A failure is data, not an
            exception, so the agent can read it and fix its body.

        Raises:
            ModelRetry: If a requested path escapes the project or is not
                an editable step module.
        """
        targets = list(names) if names else sorted(self._fillable_module_names())
        if not targets:
            return CheckOutcome(
                exit_code=0,
                stdout="no step modules to parse",
                stderr="",
                truncated=False,
                timed_out=False,
            )

        parsed: list[str] = []
        failures: list[str] = []
        for name in targets:
            path = self._editable_path(name)
            source = path.read_text(encoding="utf-8")
            try:
                ast.parse(source, filename=str(path))
            except SyntaxError as exc:
                failures.append(f"{name}:{exc.lineno}: {exc.__class__.__name__}: {exc.msg}")
            else:
                parsed.append(name)

        stdout, stdout_cut = _truncate(
            "\n".join(f"parsed {name}" for name in parsed), PARSE_CHECK_OUTPUT_LIMIT
        )
        stderr, stderr_cut = _truncate("\n".join(failures), PARSE_CHECK_OUTPUT_LIMIT)
        logger.info(
            "fill: parse_check parsed %d module(s), %d failure(s)", len(parsed), len(failures)
        )
        return CheckOutcome(
            exit_code=1 if failures else 0,
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_cut or stderr_cut,
            timed_out=False,
        )

    def _fillable_module_names(self) -> set[str]:
        """Return the project-relative paths of the existing step modules."""
        steps_dir = self.project_root.joinpath(*STEPS_DIR_PARTS)
        if not steps_dir.is_dir():
            return set()
        return {
            "/".join((*STEPS_DIR_PARTS, path.name))
            for path in steps_dir.glob("*.py")
            if path.name != "__init__.py"
        }


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Clamp ``text`` to ``limit`` characters, keeping the head and tail.

    A traceback's useful part is its end and a problem's useful part is
    often its beginning, so both ends are kept with an elision marker
    between them.

    Args:
        text: The captured stream.
        limit: Maximum characters to keep.

    Returns:
        The (possibly shortened) text and whether anything was dropped.
    """
    if len(text) <= limit:
        return text, False
    half = limit // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n... [{dropped} characters elided] ...\n{text[-half:]}", True


def _port(node: SwarmNode, which: str) -> str:
    """Return ``node``'s declared port as ``PortType (annotation)``.

    Args:
        node: The node to describe.
        which: ``"in"`` or ``"out"``.

    Returns:
        A human-readable description of the port type, always carrying
        the Python annotation (fact 17: a ``PortType`` is never used as
        an annotation verbatim).
    """
    port_type = node.io.input_type if which == "in" else node.io.output_type
    return f"{port_type} -> {PORT_TYPE_ANNOTATIONS[port_type]}"


def _fill_targets(graph: SwarmGraph) -> list[SwarmNode]:
    """The nodes whose ``steps/<id>.py`` body is still unfilled."""
    return [node for node in graph.nodes if node.kind in FILLABLE_NODE_KINDS]


def _target_file(node: SwarmNode) -> str:
    """The project-relative path of ``node``'s editable step module."""
    return "/".join((*STEPS_DIR_PARTS, f"{node.id}.py"))


def _describe_node(graph: SwarmGraph, node: SwarmNode) -> str:
    """Render one fillable node's full spec for the prompt.

    Includes every per-node field PLAN.md Phase 3 lists: ``title``,
    ``intent``, ports mapped through
    :data:`~swarm_builder.models.PORT_TYPE_ANNOTATIONS`, tools,
    ``reads``/``writes`` ownership, ``delegates_to``, and -- for
    programmatic nodes -- ``signature_hint`` and ``needs``.
    """
    lines = [
        f"- file: {_target_file(node)}",
        f"  node id (also the step function name): {node.id}",
        f"  kind: {node.kind}",
        f"  title: {node.title}",
        f"  intent: {node.intent}",
        f"  input port:  {_port(node, 'in')}",
        f"  output port: {_port(node, 'out')}",
    ]
    if node.reads:
        lines.append(f"  state fields this step may read: {', '.join(node.reads)}")
    if node.writes:
        lines.append(
            f"  state fields this step MUST assign before returning: {', '.join(node.writes)}"
        )
    if node.programmatic is not None:
        if node.programmatic.signature_hint:
            lines.append(f"  signature hint: {node.programmatic.signature_hint}")
        if node.programmatic.needs:
            lines.append(
                f"  extra packages already declared in pyproject.toml: "
                f"{', '.join(node.programmatic.needs)}"
            )
    if node.agent is not None:
        lines.append(f"  agent instructions: {node.agent.instructions}")
        if node.agent.tools:
            lines.append(f"  tools: {', '.join(node.agent.tools)}")
        if node.agent.delegates_to:
            lines.append(f"  delegates to (called as a tool): {', '.join(node.agent.delegates_to)}")
    lines.extend(_decision_lines(graph, node))
    return "\n".join(lines)


def _decision_lines(graph: SwarmGraph, node: SwarmNode) -> list[str]:
    """Return the decision/join-specific prompt lines for ``node``.

    A ``decision`` node's *inbound* neighbour is the step whose return
    value actually selects the branch (fact 27), so the constraint is
    stated against that neighbour too -- otherwise the agent writes a
    classifier whose return values no branch matches, and the graph runs
    straight into ``RuntimeError: No branch matched inputs`` (fact 14).
    """
    node_by_id = {other.id: other for other in graph.nodes}
    lines: list[str] = []
    if node.decision is not None:
        matches = ", ".join(repr(branch.match) for branch in node.decision.branches)
        lines.append(f"  branch match values: {matches}")
        if node.decision.note:
            lines.append(f"  decision note: {node.decision.note}")
    if node.join is not None:
        lines.append(f"  reducer: {node.join.reducer}")

    for edge in graph.edges:
        if node.decision is None:
            downstream = node_by_id.get(edge.target)
            if (
                edge.source == node.id
                and downstream is not None
                and downstream.decision is not None
            ):
                targeted = ", ".join(
                    f"{branch.match!r} -> {branch.target_node_id!r}"
                    for branch in downstream.decision.branches
                )
                lines.append(
                    f"  NOTE: your return value is the input of decision node "
                    f"{downstream.id!r}; it MUST equal one of {targeted}"
                )
                # Fact 27 is stated on the step that FEEDS the decision,
                # because that is the only node the agent fills: a
                # decision node has no steps/<id>.py of its own, so
                # guidance attached to it would never reach the prompt.
                for branch in downstream.decision.branches:
                    branch_target = node_by_id.get(branch.target_node_id)
                    target_desc = (
                        f"{branch.target_node_id!r} (input port "
                        f"{_port(branch_target, 'in')})"
                        if branch_target is not None
                        else repr(branch.target_node_id)
                    )
                    lines.append(
                        f"  branch {branch.match!r} dispatches to {target_desc}, which "
                        "receives the match value itself via ctx.inputs -- never your "
                        "incoming payload. Carry any payload a branch target needs "
                        "through State."
                    )
        if edge.kind == "branch" and edge.source == node.id:
            target = node_by_id.get(edge.target)
            target_desc = (
                f"{edge.target!r} (input port {_port(target, 'in')})"
                if target is not None
                else repr(edge.target)
            )
            lines.append(
                f"  branch {edge.match!r} dispatches to {target_desc}, which receives "
                f"the match value itself via ctx.inputs -- never your incoming payload. "
                "Carry any payload a branch target needs through State."
            )
    return lines


def _graph_shape_lines(graph: SwarmGraph) -> list[str]:
    """Render the whole canvas graph -- nodes and edges -- for the prompt.

    The per-node sections only cover the nodes the agent must fill, but
    the prompt also has to state relationships a step module cannot
    reveal on its own: an ``agent`` node's instructions, tools, and
    delegation targets, and the edge kinds that decide who receives what
    (facts 13 and 27). Without this section, a graph whose only
    delegation lives on a non-fillable agent node would describe no
    delegation at all.
    """
    lines = [
        f"Graph {graph.name!r} (entry {graph.entry_node_id!r}, exit {graph.exit_node_id!r}):"
    ]
    for node in graph.nodes:
        detail = f"{node.id} [{node.kind}] {node.title!r}: {node.intent}"
        if node.agent is not None:
            detail += f" (agent instructions: {node.agent.instructions!r}"
            if node.agent.tools:
                detail += f"; tools=[{', '.join(node.agent.tools)}]"
            if node.agent.delegates_to:
                detail += (
                    "; calls these child agents as tools: "
                    + ", ".join(node.agent.delegates_to)
                )
            detail += ")"
        if node.decision is not None:
            detail += " dispatches on " + ", ".join(
                f"{branch.match!r}->{branch.target_node_id}" for branch in node.decision.branches
            )
        if node.join is not None:
            detail += f" joins with {node.join.reducer}"
        lines.append(f"- {detail}")
    if graph.edges:
        lines.append("Edges:")
        for edge in graph.edges:
            descriptor = f"- {edge.source} --{edge.kind}--> {edge.target}"
            if edge.kind == "branch":
                descriptor += f" (when the value is {edge.match!r})"
            elif edge.kind == "fanout":
                descriptor += f" (converges on {edge.join_node_id})"
            elif edge.kind == "delegate":
                descriptor += " (a tool call, not a graph step)"
            lines.append(descriptor)
    return lines


def _state_lines(graph: SwarmGraph) -> list[str]:
    """Render the generated ``State`` dataclass fields for the prompt."""
    if not graph.state_fields:
        return ["The generated State dataclass has no fields."]
    lines = ["The generated State dataclass has exactly these fields:"]
    for state_field in graph.state_fields:
        default = "" if state_field.default is None else f" = {state_field.default}"
        lines.append(
            f"- {state_field.name}: {PORT_TYPE_ANNOTATIONS[state_field.type]}{default}"
        )
    return lines


def build_fill_instructions(
    project_dir: Path,
    graph: SwarmGraph,
    *,
    model_description: str,
    previous_failure: str | None = None,
) -> str:
    """Assemble the fill agent's full instruction text from the graph.

    Pure (no I/O beyond nothing at all): every fact the prompt asserts
    comes from the graph document, the project's fixed Phase-2 layout,
    and PLAN.md's pinned API. Kept separate from the agent so the
    instruction text itself is inspectable and testable.

    Args:
        project_dir: The scaffolded project root, quoted verbatim.
        graph: The canvas document being filled.
        model_description: Human-readable description of the route this
            compile runs on, for the agent's own orientation.
        previous_failure: The phase-4/5 failure text of the attempt this
            one retries, or ``None`` on the first attempt.

    Returns:
        The complete instruction text.
    """
    targets = _fill_targets(graph)
    # Quoted verbatim in the region guidance below: a concrete marker line
    # beats a `<node_id>` placeholder.
    example_id = targets[0].id if targets else "<node_id>"
    sections: list[str] = []
    sections.append(
        "You are filling in the hand-written parts of a generated PydanticAI "
        "workflow project on disk.\n"
        f"The project directory is {project_dir}.\n"
        f"{model_description}\n"
        "The wiring, state, dependency seam, and validation gate of this project "
        "are already generated and correct. Your job is ONLY to replace the "
        "placeholder bodies listed below with real, working Python."
    )

    sections.append(
        "## Your tools\n"
        "\n"
        "You have exactly three tools and no others:\n"
        "\n"
        "- read_file(name): read one scaffolded file. `name` is project-relative, "
        "e.g. src/swarm_workflow/steps/intake.py.\n"
        "- write_region(name, node_id, region, body): the ONLY way you may change "
        "anything. It replaces one marker region of one step module. `region` is "
        "\"imports\" or \"body\".\n"
        "- parse_check(names=None): parse the step modules and report any syntax "
        "error, with its file and line, as text for you to read. Called with no "
        "argument it checks every step module. It only parses -- it does not run "
        "your code.\n"
        "\n"
        "Paths outside the project directory are refused before anything is "
        "touched, so do not attempt them. You cannot create, delete, or rename "
        "files, install packages, or reach the network. Do not try to work around "
        "that; if a body seems to need a package that is not installed, implement "
        "it with the standard library instead."
    )

    sections.append(
        "## Files you may edit (and how)\n"
        "\n"
        "For each node below, edit "
        "src/swarm_workflow/steps/<node_id>.py and nothing else:\n"
        "\n"
        "- the `imports` region, between the `# --- swarm:imports <node_id> ---` "
        "and `# --- swarm:end-imports <node_id> ---` lines, holds any module-level "
        "import your body needs. It is already written with the imports the "
        "declared port types require; keep those and add what you need.\n"
        "- the `body` region, between the indented "
        "`# --- swarm:begin <node_id> ---` and `# --- swarm:end <node_id> ---` "
        "lines, is the body of the step function. Every line you pass as `body` "
        "must already be indented by four spaces, because it sits inside "
        "`async def <node_id>(ctx) -> <output annotation>`.\n"
        "\n"
        f"Spelled out for the first node, `{example_id}`: its module is "
        f"src/swarm_workflow/steps/{example_id}.py, and the four marker lines in "
        f"it read `# --- swarm:imports {example_id} ---`, "
        f"`# --- swarm:end-imports {example_id} ---`, "
        f"`# --- swarm:begin {example_id} ---` and "
        f"`# --- swarm:end {example_id} ---`.\n"
        "\n"
        "Never edit, create, or delete anything else. In particular: graph.py, "
        "state.py, deps.py, pyproject.toml, everything under validate/, every "
        "agents/ module, and every __init__.py are off limits -- agents/ modules "
        "are already complete and rewiring them would break the generated graph."
    )

    sections.append(
        "## The API contract you must follow\n"
        "\n"
        "This project is pinned to pydantic-graph / pydantic-ai 2.43, whose API "
        "differs from older releases you may remember. These rules are checked:\n"
        "\n"
        "1. Inside a step, the incoming value is `ctx.inputs` -- NEVER "
        "`ctx.input`, which does not exist.\n"
        "2. State fields are reached through `ctx.state.<field>`; the fields that "
        "exist are listed below.\n"
        "3. Never construct an `Agent` at import time, and never hardcode an API "
        "key or a model id in a body. Any agent must be built inside the step "
        "from `ctx.deps.model`.\n"
        "4. If you build an agent, run it with `result = await agent.run(...)` and "
        "use `result.output`.\n"
        "5. Native tools are attached as "
        "`capabilities=[NativeTool(WebSearchTool(optional=True))]` with "
        "`from pydantic_ai.capabilities import NativeTool` and "
        "`from pydantic_ai import WebSearchTool`. There is no `builtin_tools=` "
        "parameter, and passing a `WebSearchTool` through `tools=` or "
        "`toolsets=[...]` fails at run time.\n"
        "6. Your body must `return` a value of exactly the declared output "
        "annotation. The declared annotation is already on the function "
        "signature; do not change the signature, the decorators, or the markers.\n"
        "7. The graph's own runner calls the step as `await graph.run(inputs=..., "
        "state=..., deps=...)` and uses the returned value directly. There is no "
        "`.output` on the graph result and no `End` object to assert on, so do "
        "not add such checks."
    )

    sections.append(
        "## Data flow rule for decisions (this one is easy to get wrong)\n"
        "\n"
        "No data passes *through* a decision node. A decision dispatches on the "
        "value returned by the step immediately before it, and each branch target "
        "receives **that match value** through `ctx.inputs` -- not the payload "
        "that flowed into the classifying step.\n"
        "\n"
        "So: if a step feeds a decision, its return value MUST be one of that "
        "decision's match values, or the graph raises "
        "`RuntimeError: No branch matched inputs` at run time. And if a branch "
        "target needs the original payload, the classifying step must save it to "
        "a State field and the branch target must read it from `ctx.state`."
    )

    sections.append(
        "## The whole graph (context for who sends what to whom)\n\n"
        + "\n".join(_graph_shape_lines(graph))
    )
    sections.append("\n".join(_state_lines(graph)))

    if targets:
        rendered = "\n".join(_describe_node(graph, node) for node in targets)
        sections.append(
            f"## Nodes to fill ({len(targets)})\n"
            "\n"
            "Each entry gives the file, the node id, the intent, the declared "
            "input and output port types (with the Python annotation you must "
            "use), any state fields you own, and the relevant control-flow facts.\n"
            "\n"
            f"{rendered}"
        )
    else:
        sections.append(
            "## Nodes to fill (0)\n"
            "\n"
            "Every step body in this project is already complete. Verify that by "
            "reading a step module, then reply that there is nothing to fill."
        )

    sections.append(
        "## How to finish\n"
        "\n"
        "1. Read each step module before editing it, so you keep the surrounding "
        "code intact.\n"
        "2. Write the body with `write_region`.\n"
        "3. Check your work with `parse_check`. Called with no arguments it "
        "parses every step module and reports any syntax error with its file and "
        "line; pass a list of paths to narrow it. It only parses -- it does not "
        "run your code, install anything, or import `swarm_workflow`. The "
        "project's own validation gate executes the real graph after you are "
        "done.\n"
        "4. When every listed node is filled and `parse_check` reports no "
        "failures, reply with a one-line summary. Do not describe the code; just "
        "say what you filled."
    )

    if previous_failure is not None:
        sections.append(
            "## The previous attempt failed -- fix this\n"
            "\n"
            "Your last attempt produced a project that failed its own checks. The "
            "failure text follows. Read it, correct the bodies, and verify again "
            "with `parse_check`. Note that your earlier edits are still on disk.\n"
            "\n"
            "```\n"
            f"{previous_failure}\n"
            "```"
        )

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Agent construction and the Filler entry point
# ---------------------------------------------------------------------------


def build_fill_agent(
    session: FillSession, *, model: Model | str, instructions: str
) -> Agent[None, str]:
    """Build (never at import time) the fill agent for one session.

    Binds the session's three confined tools to a fresh
    :class:`~pydantic_ai.Agent`. Called once per fill attempt and
    exposed so a test can substitute a scripted model without reaching
    into this module's internals.

    Args:
        session: The session whose tools the agent may call.
        model: The model to run on, from ``LiveModel.model`` (fact 22: a
            prefixed known-name string or a configured ``Model``
            instance; never a bare model id).
        instructions: The assembled instruction text.

    Returns:
        The configured agent, with exactly three tools.
    """
    agent: Agent[None, str] = Agent(
        model,
        deps_type=type(None),
        instructions=instructions,
        retries=AGENT_RETRIES,
    )
    agent.tool_plain(session.read_file)
    agent.tool_plain(session.write_region)
    agent.tool_plain(session.parse_check)
    return agent


def usage_limits() -> UsageLimits:
    """Build the per-run :class:`~pydantic_ai.usage.UsageLimits` bound.

    Returns:
        Limits built from the module's ``UPPER_SNAKE_CASE`` constants, so
        a test can assert the run's caps equal the documented ones.
    """
    return UsageLimits(
        request_limit=REQUEST_LIMIT,
        tool_calls_limit=TOOL_CALLS_LIMIT,
        input_tokens_limit=INPUT_TOKENS_LIMIT,
        output_tokens_limit=OUTPUT_TOKENS_LIMIT,
        total_tokens_limit=TOTAL_TOKENS_LIMIT,
    )


#: Signature of the agent-factory seam :func:`_run_fill` drives: the
#: production value is :func:`build_fill_agent`, and the tests substitute
#: it to run a scripted model through the module's real tool binding.
AgentBuilder = Callable[..., Agent[None, str]]


def _default_agent_builder() -> AgentBuilder:
    """Resolve the production agent builder lazily.

    A ``default_factory`` rather than an eager class-level default, so
    the seam resolves :func:`build_fill_agent` through the *module
    attribute* at call time: patching that attribute (the documented way
    to script a model in a test) then actually takes effect, instead of
    being shadowed by a reference captured when this class was defined.
    """
    return build_fill_agent


@dataclass(frozen=True)
class _FillRunConfig:
    """The two injectable knobs of one fill run.

    Both exist so a test can script the model and tighten a cap without
    reaching past this module's public surface; production always uses
    the module defaults.
    """

    build_agent: AgentBuilder = field(default_factory=_default_agent_builder)
    limits: UsageLimits = field(default_factory=usage_limits)


def _model_and_description(live_model: object) -> tuple[Model | str, str]:
    """Read the run model and its description off a ``LiveModel``.

    Duck-typed rather than imported: ``compile/`` must not gain a
    dependency on ``inherit/`` (the ``ResolvedModel`` seam in
    ``compile/__init__.py`` exists precisely to keep them apart), and
    the pipeline only promises a ``LiveModel``-shaped object.

    Args:
        live_model: The object the pipeline passed as ``live_model``.

    Returns:
        ``(model, description)`` for ``Agent(...)`` and the prompt.

    Raises:
        FillError: If ``live_model`` carries no usable ``model``.
    """
    model = getattr(live_model, "model", None)
    if isinstance(model, (str, Model)) and (not isinstance(model, str) or model.strip()):
        description = getattr(live_model, "source_description", None)
        described = description if isinstance(description, str) and description else repr(model)
        return model, f"This compile runs on: {described}."
    raise FillError(
        f"the pipeline handed the fill agent a live_model with no usable .model "
        f"(got {type(live_model).__name__}); the compile agent needs a "
        "pydantic_ai Model or a prefixed known-name string"
    )


async def _run_fill(
    *,
    project_dir: Path,
    graph: SwarmGraph,
    model: Model | str,
    model_description: str,
    previous_failure: str | None,
    config: _FillRunConfig | None = None,
) -> FillResult:
    """Run one fill attempt and report the nodes it wrote.

    The core of :func:`fill`, split out so a test can supply a scripted
    model without monkeypatching :func:`_model_and_description`.

    Args:
        project_dir: The scaffolded project to fill.
        graph: The document the project was scaffolded from.
        model: The model to run the agent on.
        model_description: Description of the route, for the prompt.
        previous_failure: The prior phase-4/5 failure text, or ``None``.
        config: Injectable agent-builder/limits seam, or ``None`` for
            the production defaults.

    Returns:
        The ids of the nodes whose regions were written, in write order.

    Raises:
        FillError: If the run trips a usage limit, or the agent itself
            failed. The message always names the offending limit when
            one tripped, because PLAN.md's failure-mode table requires
            the compile to report which cap was hit.
        asyncio.CancelledError: Never caught -- a cancelled compile must
            propagate, and no process needs killing (fact 26).
    """
    resolved_config = config if config is not None else _FillRunConfig()
    session = FillSession(project_dir)
    instructions = build_fill_instructions(
        project_dir,
        graph,
        model_description=model_description,
        previous_failure=previous_failure,
    )
    agent = resolved_config.build_agent(session, model=model, instructions=instructions)
    logger.info(
        "fill: %d fillable node(s), attempt with previous_failure=%s",
        len(_fill_targets(graph)),
        "yes" if previous_failure else "no",
    )

    try:
        result = await agent.run(instructions, usage_limits=resolved_config.limits)
    except UsageLimitExceeded as exc:
        # AgentRunError subclass: pydantic_ai already names the limit
        # ("The next request would exceed the request_limit of 80"), so
        # the message is re-raised as a FillError with that text intact.
        raise FillError(f"fill run exceeded its usage limit: {exc}") from exc

    if not session.written_node_ids and _fill_targets(graph):
        # Only a failure when there was something to write. A graph whose
        # every step is an agent node is already complete after Phase 2,
        # so a model that correctly writes nothing must not fail the
        # compile -- `build_fill_instructions` already handles the
        # zero-target case, and this assertion used to contradict it.
        raise FillError(
            "the fill agent finished without writing any region "
            f"(agent output: {result.output!r})"
        )
    return FillResult(filled_node_ids=tuple(session.written_node_ids))


async def fill(
    *,
    project_dir: Path,
    graph: SwarmGraph,
    resolved_model: ResolvedModel,
    live_model: object | None,
    previous_failure: str | None,
) -> FillResult:
    """Fill every unfilled step body of a scaffolded project.

    The module-level :class:`~swarm_builder.compile.pipeline.Filler`
    implementation: ``run_compile(..., filler=fill)`` accepts it
    unchanged (or use :func:`~swarm_builder.compile.pipeline.fake_filler`
    via ``SWARM_FAKE_FILL=1`` instead).

    Args:
        project_dir: The scaffolded project to fill; the only directory
            any tool may touch.
        graph: The document the project was scaffolded from, and the
            source of the instruction text.
        resolved_model: The generated project's own default model. Read
            only for reporting -- the compile agent runs on
            ``live_model``, and the generated project's model seam is
            Phase 2's business.
        live_model: A
            :class:`~swarm_builder.inherit.routes.LiveModel` describing
            the route this compile spends (fact 24-25). Never ``None``
            on the pipeline path.
        previous_failure: The phase-4/5 failure text when this is the
            single retry, else ``None``.

    Returns:
        The ids of the nodes whose regions were written, in write order.

    Raises:
        FillError: If no live model was supplied, if the run trips a
            usage limit, or if the agent produced nothing. A raised
            failure fails the compile directly and is never retried by
            the pipeline.
        asyncio.CancelledError: Propagated untouched.
    """
    if live_model is None:
        raise FillError(
            "the fill agent needs a live model: the pipeline always supplies a "
            "LiveModel on the real Phase-3 path (SWARM_FAKE_FILL=1 is the "
            "model-free path)"
        )
    model, description = _model_and_description(live_model)
    logger.info("fill: %s (project %s)", description, project_dir)
    return await _run_fill(
        project_dir=project_dir,
        graph=graph,
        model=model,
        model_description=description,
        previous_failure=previous_failure,
    )


__all__ = [
    "AGENT_RETRIES",
    "COST_LIMIT",
    "CheckOutcome",
    "FILLABLE_NODE_KINDS",
    "FillError",
    "FillSession",
    "FORBIDDEN_DIRECTORIES",
    "FORBIDDEN_FILES",
    "INPUT_TOKENS_LIMIT",
    "OUTPUT_TOKENS_LIMIT",
    "REGION_BODY",
    "REGION_IMPORTS",
    "REQUEST_LIMIT",
    "RegionLocation",
    "RegionNotFoundError",
    "PARSE_CHECK_OUTPUT_LIMIT",
    "STEPS_DIR_PARTS",
    "TOOL_CALLS_LIMIT",
    "TOTAL_TOKENS_LIMIT",
    "VALID_REGIONS",
    "build_fill_agent",
    "build_fill_instructions",
    "fill",
    "splice_region",
    "usage_limits",
]
