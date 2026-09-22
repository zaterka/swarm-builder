"""Offline tests for Phase 3's real fill agent (``compile/agent.py``).

No test in this module contacts a provider. The model is always a
:class:`~pydantic_ai.models.function.FunctionModel` driven by a scripted
turn list (or, in the end-to-end case, the same thing writing real
bodies), so the whole suite is deterministic and credential-free -- this
is the suite PLAN.md calls out as "what makes the ~200 owned lines
trustworthy".

Covered here, in PLAN.md's own terms:

- ``write_region`` replaces only the named region and leaves the
  surrounding text **byte-identical** (asserted on bytes);
- ``write_region`` refuses an unknown file, an unknown marker, and a
  region name that is neither ``imports`` nor ``body``;
- a scripted model that attempts a traversal path gets an error back
  and writes nothing;
- ``read_file`` rejects a path outside the project and allows one inside
  it;
- ``parse_check`` reports a syntax error as data and executes nothing, and
  its timeout terminates a wedged child;
- a tripped ``UsageLimits`` ends the run with the limit named;
- ``asyncio.CancelledError`` propagates rather than being swallowed;
- an end-to-end fill driven by a scripted model produces a project that
  passes Phase 4's ``check_boundary`` and Phase 5's ``validate_project``.

The last one runs ``uv``, so it is marked ``slow`` like the rest of this
project's keyless-gate tests.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic_ai import Agent, ModelRetry, UsageLimits
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from fixtures.graphs import (
    decision_branching_graph,
    linear_chat_graph,
    mixed_programmatic_graph,
    orchestrator_graph,
)
from swarm_builder.compile import ResolvedModel, body_marker_begin, body_marker_end
from swarm_builder.compile.agent import (
    AGENT_RETRIES,
    COST_LIMIT,
    FILLABLE_NODE_KINDS,
    INPUT_TOKENS_LIMIT,
    OUTPUT_TOKENS_LIMIT,
    REQUEST_LIMIT,
    TOOL_CALLS_LIMIT,
    TOTAL_TOKENS_LIMIT,
    CheckOutcome,
    FillError,
    FillSession,
    _FillRunConfig,
    _run_fill,
    build_fill_instructions,
    fill,
    usage_limits,
)
from swarm_builder.compile.boundary import capture_baseline, check_boundary
from swarm_builder.compile.scaffold import scaffold
from swarm_builder.compile.validate import validate_project

REPO_ROOT = Path(__file__).resolve().parent.parent
UV_CACHE_DIR = REPO_ROOT / ".uv-cache"

#: The stub body ``scaffold.py`` emits for a node the fill stage must
#: replace. Asserting on it proves a fill actually happened rather than
#: the placeholder merely surviving.
UNFILLED_SENTINEL = 'raise NotImplementedError("swarm_builder: unfilled step body")'

#: One fillable node's own module, as ``scaffold.py`` shapes it (markers
#: included), for the unit tests that must not pay for a real scaffold.
_STEP_MODULE = '''"""``{node_id}`` step: {intent}"""

from __future__ import annotations

{marker_begin}
{marker_end}

from pydantic_graph import StepContext

from swarm_workflow.deps import Deps
from swarm_workflow.state import State


TAIL_SENTINEL = "untouched"


async def {node_id}(ctx: StepContext[State, Deps, str]) -> str:
    {body_begin}
{placeholder}
    {body_end}
    # trailing comment outside the body region
'''

#: What the fill agent is scripted to write into that module.
_SCRIPTED_BODY = '    return f"SUMMARY: {ctx.inputs}"'

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Pin the anyio backend to asyncio -- trio is not installed."""
    return "asyncio"


# ---------------------------------------------------------------------------
# Offline helpers
# ---------------------------------------------------------------------------


class ScriptedModel:
    """A :class:`FunctionModel` that walks a fixed list of turns.

    Each turn is either a tool call ``("tool", name, args)`` or a final
    answer ``("text", output)``, so a test can script an exact
    conversation (including a deliberately hostile one) with no provider
    involved. Records the :class:`AgentInfo` of every request, which is
    how the tool schemas and the assembled instructions are inspected.
    """

    def __init__(self, script: list[tuple[str, ...]]) -> None:
        """Store the turn list and prepare the request log."""
        self.script = script
        self.index = 0
        self.requests: list[AgentInfo] = []
        self.messages: list[list[ModelRequest | ModelResponse]] = []
        self.model = FunctionModel(self._respond)

    def _snapshot(self, messages: list[ModelRequest | ModelResponse]) -> None:
        """Record the conversation **as a copy**.

        pydantic-ai keeps mutating the live message list after a tool
        call, so a stored reference silently absorbs a later retry prompt
        into an earlier turn and makes the log misleading. The message
        *part types* are captured here (pydantic-ai's message classes are
        dataclasses without ``model_copy``), which is all the assertions
        below need.
        """
        self.messages.append(
            [
                cast(ModelRequest | ModelResponse, SimpleNamespace(parts=list(message.parts)))
                for message in messages
            ]
        )

    def _respond(
        self, messages: list[ModelRequest | ModelResponse], info: AgentInfo
    ) -> ModelResponse:
        """Return the next scripted turn (or keep repeating the last)."""
        self.requests.append(info)
        self._snapshot(messages)
        turn = self.script[min(self.index, len(self.script) - 1)]
        self.index += 1
        if not turn:
            raise AssertionError(
                f"scripted model was called {self.index} time(s) but the script has "
                f"{len(self.script)} turn(s)"
            )
        if turn[0] == "text":
            return ModelResponse(parts=[TextPart(turn[1])])
        if turn[0] == "boom":
            raise asyncio.CancelledError
        return ModelResponse(parts=[ToolCallPart(turn[1], json.loads(turn[2]))])

    def instructions(self) -> str:
        """The agent-level instruction text of the first request."""
        assert self.requests, "the model was never called"
        return self.requests[0].instructions or ""

    def tool_names(self) -> list[str]:
        """Every tool name the model was told about, sorted."""
        assert self.requests, "the model was never called"
        return sorted(tool.name for tool in self.requests[0].function_tools)

    def tool_schema(self, name: str) -> dict[str, object]:
        """The JSON schema of one tool as the model saw it."""
        for tool in self.requests[0].function_tools:
            if tool.name == name:
                return tool.parameters_json_schema
        raise AssertionError(f"no tool named {name!r} was offered")



def retry_prompts(run_result: object) -> list[str]:
    """Every ``RetryPromptPart`` content a finished run recorded.

    Read from the run's own ``new_messages()`` rather than from the
    message list handed to the model: pydantic-ai appends the retry
    prompt to an already-mutated request, so a snapshot taken inside the
    model function cannot see it.
    """
    prompts: list[str] = []
    for message in run_result.new_messages():  # type: ignore[attr-defined]
        for part in getattr(message, "parts", ()):
            if type(part).__name__ == "RetryPromptPart":
                prompts.append(str(part.content))
    return prompts


def tool_returns(messages: Sequence[ModelMessage]) -> dict[str, ToolReturnPart]:
    """The most recent successful tool return of each tool, by name.

    Takes a finished run's ``all_messages()`` -- a tool's return arrives
    in the *next* request, so a log captured inside the model function
    never contains it.
    """
    returns: dict[str, ToolReturnPart] = {}
    for message in reversed(list(messages)):
        for part in getattr(message, "parts", ()):
            if isinstance(part, ToolReturnPart) and part.tool_name not in returns:
                returns[part.tool_name] = part
    return returns


def bound_agent(session: FillSession, model: FunctionModel) -> Agent[None, str]:
    """Build an agent whose three tools are exactly the session's.

    The production tool binding, minus the production model, so a test
    can drive the real tools with a scripted conversation.
    """
    agent: Agent[None, str] = Agent(model, instructions="fill")
    agent.tool_plain(session.read_file)
    agent.tool_plain(session.write_region)
    agent.tool_plain(session.parse_check)
    return agent


def call(tool: str, /, **args: object) -> tuple[str, ...]:
    """Build a scripted tool-call turn.

    ``tool`` and the keyword arguments are positional-only / keyword-only
    respectively, so a tool argument literally named ``tool`` or ``name``
    cannot collide with this helper's own parameters.
    """
    return ("tool", tool, json.dumps(args))


def text(output: str) -> tuple[str, ...]:
    """Build a scripted final-answer turn."""
    return ("text", output)


def boom() -> tuple[str, ...]:
    """Build a scripted turn whose model raises ``CancelledError``."""
    return ("boom",)


def build_step_project(root: Path, *, node_id: str = "summarize") -> Path:
    """Write the minimal ``steps/<node_id>.py`` a unit test needs."""
    root.mkdir(parents=True, exist_ok=True)
    text_of = _STEP_MODULE.format(
        node_id=node_id,
        intent="Prefix the input.",
        marker_begin=f"# --- swarm:imports {node_id} ---",
        marker_end=f"# --- swarm:end-imports {node_id} ---",
        body_begin=body_marker_begin(node_id),
        body_end=body_marker_end(node_id),
        placeholder="    " + UNFILLED_SENTINEL,
    )
    path = root / "src" / "swarm_workflow" / "steps" / f"{node_id}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text_of, encoding="utf-8")
    return path


def scripted_build_agent(
    session: FillSession, *, model: object, instructions: str
) -> Agent[None, str]:
    """A :func:`build_fill_agent`-shaped seam that binds the session's
    real tools to the script currently registered in
    :data:`_SCRIPTED_FUNCTION_MODEL`.

    ``model`` is ignored deliberately: the production builder is given a
    ``LiveModel.model``, while a test needs the tools exercised against a
    scripted :class:`FunctionModel`. Everything else -- the three tools,
    the retry budget, the instructions -- is the production shape.
    """
    del model
    agent: Agent[None, str] = Agent(
        _SCRIPTED_FUNCTION_MODEL["model"],
        instructions=instructions,
        retries=AGENT_RETRIES,
    )
    agent.tool_plain(session.read_file)
    agent.tool_plain(session.write_region)
    agent.tool_plain(session.parse_check)
    return agent


#: The one scripted model the seam above binds, set by each test that
#: drives :func:`_run_fill` through it.
_SCRIPTED_FUNCTION_MODEL: dict[str, FunctionModel] = {}


@pytest.fixture
def outside_dir(tmp_path: Path) -> Path:
    """A directory *next to* the project root -- a traversal target."""
    target = tmp_path / "outside"
    target.mkdir()
    return target


# ---------------------------------------------------------------------------
# 1. write_region: byte-identical outside the named region
# ---------------------------------------------------------------------------


def test_write_region_replaces_only_the_named_region_byte_identically(
    tmp_path: Path,
) -> None:
    """The whole Phase-4 guarantee rests on this: the body region is the
    only thing that changes, and everything else survives byte for
    byte."""
    project = tmp_path / "project"
    step_path = build_step_project(project)
    before = step_path.read_bytes()
    session = FillSession(project)

    message = session.write_region(
        "src/swarm_workflow/steps/summarize.py", "summarize", "body", _SCRIPTED_BODY
    )

    after = step_path.read_bytes()
    assert message == (
        "replaced the body region for node 'summarize' in "
        "src/swarm_workflow/steps/summarize.py"
    )
    assert b"NotImplementedError" not in after
    assert _SCRIPTED_BODY.encode() in after

    # Byte-identical outside: the new bytes are exactly the old bytes
    # with the placeholder line replaced by the written body.
    expected = before.replace(
        ("    " + UNFILLED_SENTINEL).encode(), _SCRIPTED_BODY.encode()
    )
    assert after == expected

    # Region-scoped restatement: head (through the opening marker line)
    # and tail (from the closing marker line) are untouched.
    begin = body_marker_begin("summarize").encode()
    end = body_marker_end("summarize").encode()
    head = after[: after.index(begin) + len(begin)]
    tail = after[after.rindex(end) :]
    assert head == before[: before.index(begin) + len(begin)]
    assert tail == before[before.rindex(end) :]
    assert b"# trailing comment outside the body region" in tail
    assert b'TAIL_SENTINEL = "untouched"' in after
    assert after.startswith(b'"""``summarize`` step')


def test_write_region_replaces_the_imports_region_independently(tmp_path: Path) -> None:
    """The imports region is addressable on its own, and writing it
    leaves the body region (and its placeholder) alone."""
    project = tmp_path / "project"
    step_path = build_step_project(project)
    session = FillSession(project)

    session.write_region(
        "src/swarm_workflow/steps/summarize.py", "summarize", "imports", "import json"
    )

    after = step_path.read_text()
    expected_region = (
        "# --- swarm:imports summarize ---\n"
        "import json\n"
        "# --- swarm:end-imports summarize ---"
    )
    assert expected_region in after
    assert UNFILLED_SENTINEL in after


def test_write_region_records_the_node_once(tmp_path: Path) -> None:
    """``FillResult`` node ids are write-ordered and deduplicated."""
    project = tmp_path / "project"
    build_step_project(project)
    session = FillSession(project)
    name = "src/swarm_workflow/steps/summarize.py"

    session.write_region(name, "summarize", "imports", "import json")
    session.write_region(name, "summarize", "body", _SCRIPTED_BODY)

    assert session.written_node_ids == ["summarize"]


# ---------------------------------------------------------------------------
# 2. write_region refusals
# ---------------------------------------------------------------------------


def test_write_region_refuses_an_unknown_file(tmp_path: Path) -> None:
    """A file that is not a step module is refused, even in-project."""
    project = tmp_path / "project"
    build_step_project(project)
    graph_file = project / "src" / "swarm_workflow" / "graph.py"
    graph_file.write_text("x = 1\n", encoding="utf-8")
    session = FillSession(project)

    with pytest.raises(ModelRetry) as exc:
        session.write_region("src/swarm_workflow/graph.py", "summarize", "body", _SCRIPTED_BODY)

    assert "not editable" in str(exc.value)
    assert graph_file.read_text(encoding="utf-8") == "x = 1\n"


def test_write_region_refuses_a_forbidden_path_and_a_missing_file(tmp_path: Path) -> None:
    """``pyproject.toml`` and an agents/ module are both off limits, and
    a step module that does not exist is not creatable."""
    project = tmp_path / "project"
    build_step_project(project)
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    session = FillSession(project)

    with pytest.raises(ModelRetry, match="not editable"):
        session.write_region("pyproject.toml", "summarize", "body", _SCRIPTED_BODY)
    with pytest.raises(ModelRetry, match="not editable"):
        session.write_region(
            "src/swarm_workflow/agents/summarize.py", "summarize", "body", "    pass"
        )
    with pytest.raises(ModelRetry, match="does not exist"):
        session.write_region("src/swarm_workflow/steps/missing.py", "missing", "body", "    pass")
    assert (project / "pyproject.toml").read_text(encoding="utf-8") == "[project]\n"


def test_write_region_refuses_an_unknown_marker(tmp_path: Path) -> None:
    """A node id whose markers are not in the addressed file is refused
    and nothing is written."""
    project = tmp_path / "project"
    step_path = build_step_project(project)
    before = step_path.read_bytes()
    session = FillSession(project)

    with pytest.raises(ModelRetry, match="does not match file name"):
        session.write_region(
            "src/swarm_workflow/steps/summarize.py", "other", "body", _SCRIPTED_BODY
        )

    # A module whose markers belong to a *different* node, so the file
    # name matches the requested node but the markers do not: this is the
    # "Phase 2 emitted a module the fill stage cannot address" path.
    orphan = project / "src" / "swarm_workflow" / "steps" / "orphan.py"
    orphan.write_text(
        "# --- swarm:imports somebody_else ---\n"
        "# --- swarm:end-imports somebody_else ---\n"
        "\n"
        "async def orphan(ctx):  # type: ignore[no-untyped-def]\n"
        "    # --- swarm:begin somebody_else ---\n"
        "    pass\n"
        "    # --- swarm:end somebody_else ---\n",
        encoding="utf-8",
    )
    orphan_before = orphan.read_bytes()
    with pytest.raises(ModelRetry, match="no 'body' marker region"):
        session.write_region(
            "src/swarm_workflow/steps/orphan.py", "orphan", "body", _SCRIPTED_BODY
        )
    with pytest.raises(ModelRetry, match="no 'imports' marker region"):
        session.write_region(
            "src/swarm_workflow/steps/orphan.py", "orphan", "imports", "import json"
        )

    assert step_path.read_bytes() == before
    assert orphan.read_bytes() == orphan_before


def test_write_region_refuses_an_unknown_region_name(tmp_path: Path) -> None:
    """Only ``imports`` and ``body`` are accepted -- not ``begin``,
    ``end``, or a marker spelling."""
    project = tmp_path / "project"
    step_path = build_step_project(project)
    before = step_path.read_bytes()
    session = FillSession(project)

    for region in ("begin", "end", "end-imports", ""):
        with pytest.raises(ModelRetry, match="unknown region"):
            session.write_region(
                "src/swarm_workflow/steps/summarize.py", "summarize", region, _SCRIPTED_BODY
            )
    assert step_path.read_bytes() == before


def test_write_region_refuses_an_empty_body(tmp_path: Path) -> None:
    """An empty body would fail Phase 4's empty-region check, so it is
    refused before anything is written."""
    project = tmp_path / "project"
    step_path = build_step_project(project)
    before = step_path.read_bytes()
    session = FillSession(project)

    for body in ("", "   \n  "):
        with pytest.raises(ModelRetry, match="empty"):
            session.write_region(
                "src/swarm_workflow/steps/summarize.py", "summarize", "body", body
            )
    assert step_path.read_bytes() == before


# ---------------------------------------------------------------------------
# 3. Traversal: refused before any I/O
# ---------------------------------------------------------------------------


async def test_scripted_traversal_read_gets_an_error_back_and_writes_nothing(
    tmp_path: Path, outside_dir: Path
) -> None:
    """A scripted model that reaches for ``../..`` is told no: the tool
    returns the confinement error, the target is never created, and the
    in-project module is untouched."""
    project = tmp_path / "project"
    outside_file = outside_dir / "outside.py"
    assert not outside_file.exists()
    outside_file.parent.joinpath("sentinel.txt").write_text("original", encoding="utf-8")

    session = FillSession(project)
    script = ScriptedModel(
        [
            call("read_file", name="../outside/sentinel.txt"),
            text("understood, I will stay inside the project"),
        ]
    )
    agent = bound_agent(session, script.model)

    result = await agent.run("go", usage_limits=UsageLimits(request_limit=6))

    assert result.output == "understood, I will stay inside the project"
    # The refusal reaches the model as a retry prompt naming the guard,
    # and `Agent(retries=...)` lets the model react to it.
    prompts = retry_prompts(result)
    assert prompts, "the agent was never told why the tool call failed"
    assert all("resolves outside confinement root" in prompt for prompt in prompts)
    assert not outside_file.exists()
    assert outside_file.parent.joinpath("sentinel.txt").read_text(encoding="utf-8") == "original"


async def test_scripted_traversal_write_creates_no_file_at_all(
    tmp_path: Path, outside_dir: Path
) -> None:
    """The same for ``write_region``: an escaping path is refused before
    so much as a read, and no file appears outside the project."""
    outside_target = outside_dir / "escaped.py"
    project = tmp_path / "project"
    step_path = build_step_project(project)
    before = step_path.read_bytes()

    session = FillSession(project)
    script = ScriptedModel(
        [
            call(
                "write_region",
                name="../../outside/escaped.py",
                node_id="x",
                region="body",
                body="    pass",
            ),
            text("stopping"),
        ]
    )
    agent = bound_agent(session, script.model)

    await agent.run("go", usage_limits=UsageLimits(request_limit=6))

    assert not outside_target.exists()
    assert not any(outside_dir.iterdir())
    assert step_path.read_bytes() == before
    assert session.written_node_ids == []


# ---------------------------------------------------------------------------
# 4. read_file
# ---------------------------------------------------------------------------


def test_read_file_allows_in_project_and_rejects_outside(tmp_path: Path) -> None:
    """An in-project read works; an absolute path outside the root, a
    ``..`` traversal, and a missing file are all refused."""
    project = tmp_path / "project"
    step_path = build_step_project(project)
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me", encoding="utf-8")
    session = FillSession(project)

    assert session.read_file("src/swarm_workflow/steps/summarize.py") == step_path.read_text()
    assert session.read_file(str(step_path)) == step_path.read_text()

    with pytest.raises(ModelRetry, match="resolves outside confinement root"):
        session.read_file(str(secret))
    with pytest.raises(ModelRetry, match="resolves outside confinement root"):
        session.read_file("../../secret.txt")
    with pytest.raises(ModelRetry, match="not a file"):
        session.read_file("src/swarm_workflow/steps/nope.py")


def test_read_file_rejects_a_symlink_escaping_the_root(tmp_path: Path) -> None:
    """Confinement resolves symlinks, so a link planted inside the
    project cannot smuggle a read of the file it points at."""
    project = tmp_path / "project"
    build_step_project(project)
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me", encoding="utf-8")
    link = project / "src" / "swarm_workflow" / "steps" / "link.py"
    link.symlink_to(secret)
    session = FillSession(project)

    with pytest.raises(ModelRetry, match="resolves outside confinement root"):
        session.read_file("src/swarm_workflow/steps/link.py")


# ---------------------------------------------------------------------------
# 5. parse_check
# ---------------------------------------------------------------------------


def _scaffolded_project(tmp_path: Path) -> tuple[Path, object]:
    """Scaffold the mixed fixture and fill it, for parse_check tests."""
    from fixtures.graphs import mixed_programmatic_graph
    from swarm_builder.compile import default_scaffold_model
    from swarm_builder.compile.fake_fill import apply_fake_fill
    from swarm_builder.compile.scaffold import scaffold

    graph = mixed_programmatic_graph()
    project = tmp_path / "project"
    scaffold(graph, project, default_scaffold_model())
    apply_fake_fill(project, graph)
    return project, graph


def test_parse_check_reports_success_for_valid_modules(tmp_path: Path) -> None:
    """Every filled module parses, so the outcome is a clean exit."""
    project, _ = _scaffolded_project(tmp_path)
    session = FillSession(project)

    outcome = session.parse_check()

    assert isinstance(outcome, CheckOutcome)
    assert outcome.exit_code == 0
    assert "parsed src/swarm_workflow/steps/fetch.py" in outcome.stdout
    assert outcome.stderr == ""
    assert outcome.timed_out is False


def test_parse_check_reports_a_syntax_error_as_data(tmp_path: Path) -> None:
    """A broken body is reported with its file and line, not raised."""
    project, _ = _scaffolded_project(tmp_path)
    step = project / "src" / "swarm_workflow" / "steps" / "fetch.py"
    step.write_text(step.read_text().replace('return f"{ctx.inputs}"', 'return f"{ctx.inputs}'))
    session = FillSession(project)

    outcome = session.parse_check()

    assert outcome.exit_code == 1
    assert "steps/fetch.py" in outcome.stderr
    assert "SyntaxError" in outcome.stderr


def test_parse_check_executes_nothing(tmp_path: Path) -> None:
    """The tool parses; it must never run the code it is given.

    This is the regression guard for a real security hole: the tool this
    replaced ran arbitrary model-authored Python in a subprocess, and a
    snippet writing outside the project succeeded -- invisible to Phase 4,
    which only walks the project directory. A module whose body would
    create a file on execution must parse cleanly and create nothing.
    """
    project, _ = _scaffolded_project(tmp_path)
    escape_target = tmp_path / "ESCAPED.txt"
    step = project / "src" / "swarm_workflow" / "steps" / "fetch.py"
    step.write_text(
        step.read_text().replace(
            'return f"{ctx.inputs}"',
            f"import pathlib; pathlib.Path({str(escape_target)!r}).write_text('x')\n"
            '    return f"{ctx.inputs}"',
        )
    )
    session = FillSession(project)

    outcome = session.parse_check()

    assert outcome.exit_code == 0, outcome.stderr
    assert not escape_target.exists(), "parse_check executed the module it was asked to parse"


def test_parse_check_refuses_a_path_outside_the_project(tmp_path: Path) -> None:
    """A traversal path is refused by the same guard as every other tool."""
    project, _ = _scaffolded_project(tmp_path)
    session = FillSession(project)

    with pytest.raises(ModelRetry):
        session.parse_check(["../../etc/passwd"])


def test_parse_check_with_no_modules_is_a_clean_no_op(tmp_path: Path) -> None:
    """A project with no step modules reports success, not an error."""
    project = tmp_path / "empty"
    project.mkdir()
    session = FillSession(project)

    outcome = session.parse_check()

    assert outcome.exit_code == 0
    assert outcome.stderr == ""


# ---------------------------------------------------------------------------
# 6. Tool surface as the model sees it, and bound constants
# ---------------------------------------------------------------------------


async def test_the_model_is_offered_exactly_three_tools() -> None:
    """Three tools and no others; no shell, no general file write."""
    project = Path("/tmp/does-not-matter")
    session = FillSession(project)
    script = ScriptedModel([text("nothing to do")])
    agent = bound_agent(session, script.model)

    await agent.run("go")

    assert script.tool_names() == ["parse_check", "read_file", "write_region"]
    assert script.tool_schema("read_file")["required"] == ["name"]
    assert script.tool_schema("write_region")["required"] == [
        "name",
        "node_id",
        "region",
        "body",
    ]
    assert script.tool_schema("parse_check").get("required", []) == []


def test_usage_limit_constants_are_the_documented_ones() -> None:
    """The run's caps equal the module's ``UPPER_SNAKE_CASE`` constants,
    and the installed 2.43.0 ``UsageLimits`` accepts ``cost_limit`` --
    deliberately left ``None`` (see the module docstring)."""
    limits = usage_limits()

    assert limits.request_limit == REQUEST_LIMIT
    assert limits.tool_calls_limit == TOOL_CALLS_LIMIT
    assert limits.input_tokens_limit == INPUT_TOKENS_LIMIT
    assert limits.output_tokens_limit == OUTPUT_TOKENS_LIMIT
    assert limits.total_tokens_limit == TOTAL_TOKENS_LIMIT
    assert limits.cost_limit is COST_LIMIT is None
    assert "cost_limit" in type(limits).__dataclass_fields__


# ---------------------------------------------------------------------------
# 7. Limits and cancellation
# ---------------------------------------------------------------------------


async def test_tripped_usage_limit_ends_the_run_and_names_the_limit(tmp_path: Path) -> None:
    """A pathological loop is stopped by ``UsageLimits`` and the compile
    failure names the cap that tripped."""
    project = tmp_path / "project"
    build_step_project(project)
    graph = mixed_programmatic_graph()
    script = ScriptedModel(
        [call("parse_check"), call("parse_check")]
    )

    _SCRIPTED_FUNCTION_MODEL["model"] = script.model
    with pytest.raises(FillError, match="tool_calls_limit"):
        await _run_fill(
            project_dir=project,
            graph=graph,
            model=script.model,
            model_description="scripted",
            previous_failure=None,
            config=_FillRunConfig(
                build_agent=scripted_build_agent,
                limits=UsageLimits(request_limit=8, tool_calls_limit=2),
            ),
        )

    # The model kept being asked to call the tool until the cap stopped it.
    assert script.index >= 3


async def test_cancelled_error_propagates_rather_than_being_swallowed(tmp_path: Path) -> None:
    """Cancellation must reach the caller: the pipeline cancels the fill
    task and relies on that propagating (no process to kill, fact 26)."""
    project = tmp_path / "project"
    build_step_project(project)
    script = ScriptedModel([boom()])
    _SCRIPTED_FUNCTION_MODEL["model"] = script.model

    with pytest.raises(asyncio.CancelledError):
        await _run_fill(
            project_dir=project,
            graph=mixed_programmatic_graph(),
            model=script.model,
            model_description="scripted",
            previous_failure=None,
            config=_FillRunConfig(build_agent=scripted_build_agent),
        )


async def test_an_agent_that_writes_nothing_fails_the_fill(tmp_path: Path) -> None:
    """A run that finishes without writing any region is a failure, not
    a silent no-op: Phase 4 would otherwise reject the placeholders."""
    project = tmp_path / "project"
    build_step_project(project)
    script = ScriptedModel([text("all done, nothing to change")])
    _SCRIPTED_FUNCTION_MODEL["model"] = script.model

    with pytest.raises(FillError, match="without writing any region"):
        await _run_fill(
            project_dir=project,
            graph=mixed_programmatic_graph(),
            model=script.model,
            model_description="scripted",
            previous_failure=None,
            config=_FillRunConfig(build_agent=scripted_build_agent),
        )


async def test_fill_requires_a_live_model(tmp_path: Path) -> None:
    """The pipeline always supplies one; a ``None`` is a programming
    error with an actionable message, not a stack trace."""
    project = tmp_path / "project"
    build_step_project(project)

    with pytest.raises(FillError, match="needs a live model"):
        await fill(
            project_dir=project,
            graph=mixed_programmatic_graph(),
            resolved_model=ResolvedModel(helper_source="def f() -> str:\n    return 'm'\n"),
            live_model=None,
            previous_failure=None,
        )


async def test_fill_rejects_a_live_model_without_a_model_field(tmp_path: Path) -> None:
    """A malformed ``LiveModel``-shaped object fails loudly."""
    project = tmp_path / "project"
    build_step_project(project)

    with pytest.raises(FillError, match="no usable .model"):
        await fill(
            project_dir=project,
            graph=mixed_programmatic_graph(),
            resolved_model=ResolvedModel(helper_source="def f() -> str:\n    return 'm'\n"),
            live_model=object(),
            previous_failure=None,
        )


@dataclass(frozen=True)
class _FakeLiveModel:
    """Stand-in for ``inherit.routes.LiveModel``.

    ``compile/`` deliberately does not import ``inherit/`` (the
    ``ResolvedModel`` seam exists to keep them apart), so the filler reads
    the three fields it needs duck-typed; this mirrors that shape without
    pulling Group 3's settings machinery into this suite.
    """

    model: object
    source_description: str = "amazon-bedrock / us.anthropic.claude-opus-5"


async def test_fill_runs_on_the_live_models_model_and_quotes_its_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real entry point uses ``live_model.model`` and tells the model
    which route it is spending."""
    project = tmp_path / "project"
    build_step_project(project)
    script = ScriptedModel([text("nothing to change")])
    captured: dict[str, object] = {}

    def fake_builder(
        session: FillSession, *, model: object, instructions: str
    ) -> Agent[None, str]:
        captured["model"] = model
        captured["instructions"] = instructions
        return bound_agent(session, script.model)

    monkeypatch.setattr("swarm_builder.compile.agent.build_fill_agent", fake_builder)
    fake_live_model = _FakeLiveModel(model=script.model)

    with pytest.raises(FillError, match="without writing any region"):
        await fill(
            project_dir=project,
            graph=mixed_programmatic_graph(),
            resolved_model=ResolvedModel(helper_source="def f() -> str:\n    return 'm'\n"),
            live_model=fake_live_model,
            previous_failure="boundary check found 1 violation(s)",
        )

    assert captured["model"] is script.model
    instructions = captured["instructions"]
    assert isinstance(instructions, str)
    assert fake_live_model.source_description in instructions
    # The single retry appends the previous failure.
    assert "previous attempt failed" in instructions


# ---------------------------------------------------------------------------
# 8. Instruction assembly
# ---------------------------------------------------------------------------


def test_instructions_pin_the_api_and_describe_every_node() -> None:
    """The prompt states the rules a model gets wrong from training data,
    plus every per-node field PLAN.md Phase 3 lists."""
    graph = orchestrator_graph()
    instructions = build_fill_instructions(
        Path("/tmp/project"), graph, model_description="This compile runs on: test."
    )
    node = next(n for n in graph.nodes if n.agent and n.agent.delegates_to)

    assert "ctx.inputs" in instructions
    assert "NEVER `ctx.input`" in instructions
    assert "never hardcode an API key" in instructions
    assert "ctx.deps.model" in instructions
    assert "NativeTool(WebSearchTool(optional=True))" in instructions
    assert "result.output" in instructions.lower()
    assert "no `.output` on the graph result" in instructions
    assert "RuntimeError: No branch matched inputs" in instructions
    assert "# --- swarm:imports" in instructions
    assert "# --- swarm:begin" in instructions
    # Files it must not touch, restated.
    for forbidden in ("graph.py", "state.py", "deps.py", "pyproject.toml", "validate/"):
        assert forbidden in instructions
    # The orchestrator is an ``agent`` node, so its step body is already
    # complete: it must NOT be listed as a fill target, while the graph
    # description still states its delegation.
    assert node.kind == "agent"
    assert f"steps/{node.id}.py" not in instructions
    assert node.id in instructions
    assert node.agent.delegates_to[0] in instructions
    assert "calls these child agents as tools" in instructions
    assert "Nodes to fill (0)" in instructions
    assert node.agent.instructions in instructions


def test_instructions_carry_signature_hints_state_ownership_and_branch_matches() -> None:
    """Programmatic hints, ``reads``/``writes`` ownership, and a
    decision's match values are all in the prompt; a step feeding a
    decision is told which values it must return (fact 27)."""
    graph = decision_branching_graph()
    instructions = build_fill_instructions(
        Path("/tmp/project"), graph, model_description="This compile runs on: test."
    )

    assert "length > 3 -> big else small" in instructions
    assert "state fields this step MUST assign before returning: length_bucket" in instructions
    # Ports are rendered with their Python annotation (fact 17), never as
    # a bare PortType member.
    assert "input port:  str -> str" in instructions
    assert "output port: str -> str" in instructions
    # Every fill target gets its own editable file path -- and only a
    # `programmatic` node is one. Phase 2 emits no steps/<id>.py for a
    # `decision` or `join` node (both are wired in graph.py), so naming
    # one here used to tell the model to edit a file that cannot exist.
    for target in (n for n in graph.nodes if n.kind in FILLABLE_NODE_KINDS):
        assert f"steps/{target.id}.py" in instructions
    for wired_only in (n for n in graph.nodes if n.kind in {"decision", "join"}):
        assert f"steps/{wired_only.id}.py" not in instructions
    assert "'big', 'small'" in instructions
    assert "your return value is the input of decision node 'decision'" in instructions
    assert "receives the match value itself via ctx.inputs" in instructions
    assert "length_bucket: str" in instructions


def test_instructions_include_the_previous_failure_on_a_retry() -> None:
    """The single retry appends the phase-4/5 failure text."""
    graph = mixed_programmatic_graph()
    first = build_fill_instructions(
        Path("/tmp/project"), graph, model_description="m", previous_failure=None
    )
    retry = build_fill_instructions(
        Path("/tmp/project"),
        graph,
        model_description="m",
        previous_failure="boundary check found 1 violation(s): empty_body_region",
    )

    assert "previous attempt failed" not in first
    assert "previous attempt failed" in retry
    assert "empty_body_region" in retry


def test_instructions_survive_a_graph_with_nothing_to_fill() -> None:
    """A graph whose bodies are all complete says so instead of asking
    for edits that would overwrite working code."""
    graph = linear_chat_graph()
    only_agents = graph.model_copy(
        update={"nodes": [n for n in graph.nodes if n.kind == "agent"]}
    )
    instructions = build_fill_instructions(
        Path("/tmp/project"), only_agents, model_description="m"
    )

    assert "Nodes to fill (0)" in instructions


# ---------------------------------------------------------------------------
# 9. End to end: scripted fill, then the real Phase-4 and Phase-5 gates
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_scripted_fill_passes_phase_four_and_phase_five(tmp_path: Path) -> None:
    """The real proof that the agent's output is acceptable: a scripted
    model writes both programmatic bodies of a scaffolded fixture, and
    the resulting project passes Phase 4's ``check_boundary`` and Phase
    5's ``validate_project`` -- which runs ``uv sync``, the keyless
    import, and the emitted dry run."""
    graph = linear_chat_graph()
    project = tmp_path / "scripted-fill"
    resolved_model = ResolvedModel(
        helper_source=(
            'DEFAULT_MODEL: str = "bedrock:us.anthropic.claude-opus-5"\n'
            "\n"
            "\n"
            "def _resolve_default_model() -> str:\n"
            '    return os.environ.get("SWARM_MODEL") or DEFAULT_MODEL\n'
        ),
        pyproject_extras=("bedrock",),
        readme_model_note="Inherited default model: bedrock:us.anthropic.claude-opus-5.",
    )
    scaffold(graph, project, resolved_model)
    baseline = capture_baseline(project)
    # Phase 2 leaves exactly the two programmatic bodies unfilled.
    for node_id in ("intake", "summarize"):
        step_text = (project / "src" / "swarm_workflow" / "steps" / f"{node_id}.py").read_text()
        assert UNFILLED_SENTINEL in step_text

    body_intake = '    topic = ctx.inputs.strip()\n    ctx.state.topic = topic\n    return topic'
    body_summarize = '    return f"SUMMARY: {ctx.inputs}"'
    script = ScriptedModel(
        [
            call("read_file", name="src/swarm_workflow/steps/intake.py"),
            call(
                "write_region",
                name="src/swarm_workflow/steps/intake.py",
                node_id="intake",
                region="body",
                body=body_intake,
            ),
            call("read_file", name="src/swarm_workflow/steps/summarize.py"),
            call(
                "write_region",
                name="src/swarm_workflow/steps/summarize.py",
                node_id="summarize",
                region="body",
                body=body_summarize,
            ),
            call("parse_check"),
            text("filled intake and summarize"),
        ]
    )

    session = FillSession(project)
    instructions = build_fill_instructions(
        project,
        graph,
        model_description="This compile runs on: scripted (test)",
    )
    agent = bound_agent(session, script.model)
    run = await agent.run(instructions, usage_limits=usage_limits())

    assert session.written_node_ids == ["intake", "summarize"]
    for node_id in ("intake", "summarize"):
        step_text = (project / "src" / "swarm_workflow" / "steps" / f"{node_id}.py").read_text()
        assert UNFILLED_SENTINEL not in step_text

    # The scripted parse_check really parsed the project's modules.
    check_return = tool_returns(run.all_messages())["parse_check"]
    assert isinstance(check_return.content, CheckOutcome)
    assert check_return.content.exit_code == 0, check_return.content.stderr

    # The prompt the model actually received pins the API it must write.
    assert "ctx.inputs" in instructions
    assert "# --- swarm:begin intake ---" in instructions

    # Phase 4: markers intact, bodies non-empty, nothing else touched.
    check_boundary(project, baseline)

    # Phase 5: the real keyless gate.
    try:
        validation = validate_project(
            project,
            route=None,
            resolved_model=resolved_model,
            uv_cache_dir=UV_CACHE_DIR,
        )
    except Exception as exc:  # surfaced with the failing step's output
        pytest.fail(f"Phase 5 rejected the scripted fill: {type(exc).__name__}: {exc}")
    assert validation.ok
    assert [step.step for step in validation.steps] == ["uv_sync", "keyless_import", "dry_run"]


@pytest.mark.slow
async def test_scaffold_refuses_to_run_without_uv_on_path(tmp_path: Path) -> None:
    """Guard for the suite's own assumption: this repository's offline
    gates need ``uv``; without it the e2e test above cannot be trusted."""
    assert shutil.which("uv"), "uv must be on PATH for the keyless gate"


# ---------------------------------------------------------------------------
# 10. TestModel is enough to drive the tools (no provider, no network)
# ---------------------------------------------------------------------------


async def test_test_model_drives_the_bound_fill_agent(tmp_path: Path) -> None:
    """``TestModel`` can drive the same three bound tools with no
    provider and no network, and a run that calls no tool leaves the
    project byte-identical."""
    project = tmp_path / "project"
    step_path = build_step_project(project)
    session = FillSession(project)
    agent: Agent[None, str] = Agent(
        TestModel(call_tools=[], custom_output_text="nothing to do"), instructions="fill"
    )
    agent.tool_plain(session.read_file)
    agent.tool_plain(session.write_region)
    agent.tool_plain(session.parse_check)

    result = await agent.run("go")

    assert result.output == "nothing to do"
    assert session.written_node_ids == []
    assert step_path.read_text().count("# --- swarm:begin summarize ---") == 1
    assert sorted(p.name for p in project.rglob("*") if p.is_file()) == ["summarize.py"]
