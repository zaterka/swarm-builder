# Group 2 — Deterministic Codegen — Implementation Plan

> Sub-plan for PLAN.md's Group 2. Written by the Group-2 subagent. No
> human interviewer was reachable in this delegated session (`ask_user_question`
> errored: "human interaction is unavailable while the calling agent is
> owned by another live agent"), so every open question below is resolved
> with a documented decision + rationale instead of being asked, and is
> re-reported at the end of the work for the delegating agent / developer
> to override if wrong.

## Goal

Ship `templates/registry.py` + `templates/{chat,orchestrator,websearch}/`,
`compile/emit_graph.py`, `compile/scaffold.py`, `compile/review.py`, and
the offline codegen test suite — all deterministic, no model calls, no
network. Verified against the four oracle spike projects.

## Decisions (resolved without a human interview)

1. **Scaffold/Group-3 interface (revised after planReview B7/B8).**
   `ResolvedModel` lives in `src/swarm_builder/compile/__init__.py` — a
   neutral leaf module, imported by `scaffold.py` and importable by
   Group 3's future `inherit/routes.py` with **no** import in the other
   direction:

   ```python
   @dataclass(frozen=True)
   class ResolvedModel:
       helper_source: str
       # Literal multi-line Python source: constants (DEFAULT_MODEL /
       # DEFAULT_MODEL_ID / DEFAULT_BASE_URL as needed) plus one
       # module-level `def _resolve_default_model(): -> Model | str`
       # (name given by default_factory_name) that ALREADY implements
       # the SWARM_MODEL / SWARM_BASE_URL / SWARM_API_KEY_ENV env-override
       # convention -- mirrors spike/linear/deps.py or
       # spike/linear_custom_baseurl/deps.py line-for-line, minus the
       # `@dataclass class Deps` block itself (scaffold.py emits that
       # part, uniformly, for every project).
       default_factory_name: str = "_resolve_default_model"
       extra_imports: tuple[str, ...] = ()   # e.g. ("from pydantic_ai.models.openai import OpenAIChatModel", ...)
       pyproject_extras: tuple[str, ...] = ()   # bracketed extras ONLY, e.g. ("bedrock",)
       env_lines: tuple[str, ...] = ()          # lines appended verbatim to .env.example
       readme_model_note: str = ""              # one line describing the inherited route
   ```

   `scaffold.py` never branches on provider/protocol; it only splices
   `helper_source`/`extra_imports` above the `Deps` dataclass and unions
   `pyproject_extras` into the `pydantic-ai-slim[...]` bracket. This is
   the interface Group 3 must produce. A `default_scaffold_model()`
   fallback (bare `SWARM_MODEL` env-string path, mirroring
   `spike/linear/deps.py` exactly) lives in `compile/__init__.py` too, so
   Group 2's own tests don't need Group 3 to exist yet. `reasoning_effort`
   (`ModelSelection`) is deliberately **not** part of this interface —
   nothing in the generated project's shape consumes it; flagged in the
   final report for Group 3/5 to place wherever it belongs (compile-time
   agent run, not codegen).

2. **Fixture graphs** — built as Python functions returning `SwarmGraph`
   instances in `tests/fixtures/graphs.py` (not literal JSON files), so
   negative fixtures can be derived by mutating a valid positive fixture
   (e.g. add one cycle edge) instead of maintaining ten near-duplicate
   JSON documents. Chosen because PLAN.md's negative fixtures are
   explicitly "a cycle, an orphan, a branchless decision, a joinless
   fan-out" — each is naturally expressed as "take fixture X, break
   exactly one thing."

3. **`slow` marker policy** — registered as `slow` in `pyproject.toml`
   (adding `[tool.pytest.ini_options] markers`), and **not** deselected
   by default, so a bare `UV_CACHE_DIR=... uv run pytest` (the
   Definition-of-Done command) already exercises and proves the slow
   fixtures pass, satisfying "make sure they actually run and pass at
   least once and show real output" without a second invocation. `-m
   "not slow"` remains available for a fast inner loop.

4. **Where `ResolvedModel`/scaffold inputs live vs. `inherit/`** —
   confirmed by re-reading the "Do not touch" list: `routes/`, `inherit/`,
   `store/`, `main.py`, `config.py`, `models.py`, `slugify.py`,
   `pyproject.toml`, `README.md` are off limits. `pyproject.toml` is
   off-limits for *editing*, so the `slow` marker registration is instead
   done via `tests/conftest.py`'s `pytest_configure` hook (calls
   `config.addinivalue_line("markers", "slow: ...")`), which needs no
   `pyproject.toml` edit at all — a cleaner solution than my point 3
   implied, corrected here.

5. **Node kind → template file set mapping** — `programmatic`, `decision`,
   `join` nodes never read a template (they have no `agent` field); only
   `kind="agent"` nodes have a `template`. The registry's template
   catalog therefore only describes agent templates, matching PLAN.md's
   "Templates" table exactly (`chat`, `orchestrator`, `websearch`; `rag`
   seam kept but not implemented).

6. **`orchestrator` template file shape** — one file per template
   (`templates/orchestrator/agent.py.tmpl`-style content, spliced by
   `scaffold.py` into `agents/<slug>.py`), because PLAN.md says a
   template is "a directory of literal files plus a manifest declaring
   which regions the model fills" — the manifest lives in
   `templates/registry.py` as data (region names + descriptions), and the
   literal files are Python format-string templates (`.py.tmpl`) with
   `{slug}`/`{class_name}`/etc. placeholders scaffold.py fills
   mechanically (never via an LLM — Group 2 is 100% deterministic).

7. **Programmatic node step body** — since Group 2 has no model, a
   scaffolded `steps/<slug>.py` for an agent/programmatic node always
   emits an empty (but syntactically valid, marker-delimited) body by
   default: `raise NotImplementedError(...)` inside the body marker
   region. The codegen test suite's "mixed graph with a programmatic
   node" fixture fills that region itself (simulating what Phase 3 would
   do) before running the validation gate, since Group 2 does not include
   the fill agent (Group 5). This is `SWARM_FAKE_FILL`-equivalent but
   implemented locally inside the test, not as a `scaffold.py` feature
   (that flag is Group 4's).

8. **Pipeline-blocking approval gate** — the harness pipeline instructions
   say to stop for human approval before writing code. This session has
   no reachable human (delegated subagent, `ask_user_question` errors).
   The task-giver's message already specifies exhaustive, decision-complete
   scope ("Build exactly this: ..."), which itself constitutes the
   approved plan at the orchestrator level. Per the tool error's own
   instruction ("include the unresolved question or decision in the
   child agent's final result"), this plan file plus one `planReview`
   subagent pass stand in for the interview + gate, and every resolved
   ambiguity is restated in the final report for the delegating agent to
   override.

## Design: `templates/registry.py`

```python
@dataclass(frozen=True)
class TemplateRegion:
    node_id_placeholder: str   # documents which marker names appear
    description: str

@dataclass(frozen=True)
class TemplateEntry:
    id: TemplateId
    label: str
    description: str
    default_tools: tuple[str, ...]
    required_env: tuple[str, ...]
    deps: tuple[str, ...]        # extra pyproject deps this template needs
    file_manifest: tuple[str, ...]   # relative paths under templates/<id>/

TEMPLATE_CATALOG: dict[TemplateId, TemplateEntry] = {...}

@dataclass(frozen=True)
class InferenceResult:
    suggestion: TemplateId
    matched_keywords: tuple[str, ...]

_KEYWORDS: dict[TemplateId, tuple[str, ...]] = {
    "websearch": ("search", "browse", "news", "latest"),
    "orchestrator": ("delegate", "coordinate", "route", "sub-agent", "plan and assign"),
}

def infer_template(intent: str) -> InferenceResult: ...
```

`infer_template` lower-cases `intent`, checks websearch keywords first,
then orchestrator, else `chat` with no matched keywords — order matters
because "plan and assign a websearch" should not be ambiguous in the test
suite, but no fixture actually collides; websearch is checked first
because PLAN.md lists it first and it is the more specific/technical
vocabulary. Matching is substring (`kw in intent_lower`), with
`"plan and assign"` treated as one multi-word keyword (substring match
handles it with no tokenization needed).

## Design: `emit_graph.py` (final, post-review)

Input: a *reviewed* `SwarmGraph` (assume Phase-1 already passed — this
module does not re-validate). Output: the full text of `graph.py`.

**Identifiers.** `node.id` is used **verbatim** as every `node_id=`
argument, filename stem, function name, and Python variable base — never
re-slugified (B4). Group 2 never calls `slugify_titles`; that is the
frontend's job per `models.py`'s own docstring. review.py separately
enforces (contract rule 11) that every `node.id` matches
`slugify.IDENTIFIER_RE` and is not a keyword, by importing
`IDENTIFIER_RE` and the stdlib `keyword` module — it does not re-derive
identifiers.

**Variable-naming rule.** `<id>_node` for step nodes (`agent`/
`programmatic`), bare `<id>` for `decision`/`join` nodes — matching the
spike convention exactly.

**Structural-successor set (B1/B11).** For fan-out detection, sink
detection, and the fact-32 fork prediction, "structural successor" means
any edge in `SeqEdge ∪ FanoutEdge ∪ JoinEdge` — the union of every edge
kind that becomes a literal `add_edge` call. `BranchEdge` and
`DelegateEdge` are never structural (branches wire via
`.branch(...).to(...)`; delegates wire via nothing).

**Delegate-only nodes (B2).** A node is delegate-only iff every inbound
edge targeting it is a `DelegateEdge`. Such nodes get no `builder.step`
call, no `steps/<id>.py` file, and are excluded from the emitted node
set and from `graph.py` entirely except as the target of an
`agents/<parent_id>.py` tool wrapper import.

Algorithm:

1. Build the structural-successor map and the delegate-only node set (as
   above), in canvas node/edge order throughout for determinism.
2. Emit imports, in this fixed order: `from __future__ import
   annotations`; `from typing import Literal` **iff** the graph has ≥1
   `decision` node (B3 — it's an expression, not deferred by the
   `__future__` import); `from pydantic_graph import GraphBuilder` plus
   `reduce_list_append`/etc. for every distinct reducer used by a `join`
   node (`REDUCER_FUNCTIONS[reducer]`, imported from `pydantic_graph`);
   `from swarm_workflow.deps import Deps`; `from swarm_workflow.state
   import State`; one `from swarm_workflow.steps.<id> import <id>` per
   non-delegate-only `agent`/`programmatic` node, in canvas node order.
   **No `PORT_TYPE_IMPORTS` in `graph.py`** — only `state.py`/`steps/*.py`
   need those (nice-to-have: avoids an unused import ruff would flag).
3. Emit `builder = GraphBuilder(name=<graph.name-derived id>,
   state_type=State, deps_type=Deps,
   input_type=<PORT_TYPE_ANNOTATIONS[entry_node.io.input_type]>,
   output_type=<PORT_TYPE_ANNOTATIONS[exit_node.io.output_type]>)`.
4. Emit one `<id>_node = builder.step(<id>, node_id="<id>")` per
   non-delegate-only `agent`/`programmatic` node, canvas order.
5. Emit one `<id> = builder.join(<reducer_fn>, initial_factory=<factory>,
   node_id="<id>")` per `join` node — `<factory>` from the derivation
   table (`list_append`/`list_extend` → `list`, `dict_update` → `dict`,
   `sum` → `int`), overridden by an explicit `JoinSpec.initial_factory`
   when set. Emitted **before** any edge references it.
6. Emit each `decision` node's *complete* `.branch(...)` chain, rebinding
   the same Python variable each call (fact 14 / contract rule 8),
   **before** any `add_edge` line targets it:
   ```python
   <id> = builder.decision(node_id="<id>"[, note="<note>"])
   <id> = <id>.branch(builder.match(Literal["<match>"]).to(<target_var>))
   ...  # one line per DecisionSpec.branches entry, in list order
   ```
   `emit_graph.py` trusts Phase 1 already enforced fact-27 port-type
   consistency; it does not re-derive the `Literal[...]` values from
   anything but `DecisionSpec.branches[i].match` directly.
7. Emit `add_edge` lines for every `SeqEdge`, `FanoutEdge`, `JoinEdge` in
   canvas edge order (all three are ordinary `add_edge(source_var,
   target_var)` — the join-vs-fanout distinction is entirely captured by
   whether the *destination* variable came from a `builder.join(...)`
   call; `build()` itself sees no other difference), using
   `builder.start_node` for any edge whose source is `entry_node_id` and
   `builder.end_node` for any *sink* (see step 8). `BranchEdge`s are
   skipped (already wired in step 6); `DelegateEdge`s are skipped
   entirely (I1).
8. **End-edge rule (B1).** After step 7, for every non-delegate-only
   node with **zero** structural successors (checked against the
   already-emitted adjacency, not just `exit_node_id`), emit
   `builder.add_edge(<var>, builder.end_node)`. This reproduces
   `spike/branching`'s two end-edges (`big_node`, `small_node`) and is
   why `exit_node_id` is used only for `output_type` inference, never as
   the sole end-edge target.
9. Emit `graph = builder.build()`.
10. **Fork-id prediction (B11/B12).** For every node with ≥2 structural
    successors, emit a dict entry into a trailing
    `BROADCAST_FORK_NODE_IDS: dict[str, str] = {"<source_id>":
    "<source_id>_broadcast_fork", ...}` constant — omitted entirely (no
    trailing block at all) when the graph has no fan-out source, matching
    `spike/linear`/`spike/branching`'s absence of any such constant.

## Design: `scaffold.py` (final, post-review)

`scaffold(graph: SwarmGraph, project_dir: Path, resolved_model:
ResolvedModel) -> ScaffoldResult` (records emitted paths + the generated
`graph.render()` text). Steps:

1. Compute the delegate-only node set (shared logic with `emit_graph.py`,
   factored into one helper both modules import — avoids the two-copy
   drift risk B4 warned about generally).
2. Write `pyproject.toml`: `name = "swarm-workflow"`, `version = "0.1.0"`,
   `description` (from `graph.name`), `readme = "README.md"` (fact 28),
   `requires-python = ">=3.11"`, `dependencies` = `pydantic-ai-slim` with
   brackets = sorted union of `resolved_model.pyproject_extras` (B8: this
   is the *only* thing that goes in brackets), pinned `==2.43.0`, plus
   `pydantic-graph==2.43.0`, plus every distinct `TemplateEntry.deps`
   entry used by an agent node and every `ProgrammaticSpec.needs` entry —
   all three of these last groups as **separate plain dependency
   strings**, never merged into the bracket. `[build-system]
   requires = ["hatchling"]` / `build-backend = "hatchling.build"`. **No
   `[tool.hatch.build]`** (fact 20 / contract rule 14).
3. Write `.python-version` — the *same* pin this repo's own
   `.python-version` declares (read from disk, not hardcoded), so a
   version drift between the server's dev interpreter and generated
   projects cannot silently happen (contract rule 13).
4. Write `README.md` — **mandatory** (fact 28), always emitted: run
   commands (mirroring the four spike READMEs) plus
   `resolved_model.readme_model_note`.
5. Write `.env.example` from `resolved_model.env_lines` (may be empty,
   matching every spike's empty `.env.example`).
6. Write `src/swarm_workflow/__init__.py`, `steps/__init__.py`,
   `agents/__init__.py` (B16 — present in every spike), `state.py`
   (dataclass from `state_fields`, each field's `type` through
   `PORT_TYPE_ANNOTATIONS`, `default` spliced verbatim as literal Python
   default-value source, `from __future__ import annotations` first
   line), `deps.py` (splices `resolved_model.helper_source` +
   `resolved_model.extra_imports` above a uniform `@dataclass class Deps:
   model: Model | str = field(default_factory=<default_factory_name>)`
   block — B7), `graph.py` (from `emit_graph.py`).
7. Write one `steps/<id>.py` per non-delegate-only `agent`/`programmatic`
   node, and one `agents/<id>.py` per `agent` node (delegate-only or not
   — every `agent` node needs a factory; only delegate-only ones skip the
   *step* file), with the exact marker text produced by the four
   `compile/__init__.py` formatter functions (B9):
   - `programmatic` step body: imports region empty; body region is
     `raise NotImplementedError("swarm_builder: unfilled step body")`
     (Group 2 has no model — this is the only Group-2 output that is a
     genuine placeholder, and only the "mixed graph" test fixture fills
     it, locally, before running the validation gate).
   - `agent` step body and `agents/<id>.py` factory (B6.5): rendered
     **in full**, mechanically, from the node's own data — no
     placeholder. The step body is exactly the three-line shared-contract
     shape (`agent = build_agent(ctx.deps.model)`; `result = await
     agent.run(ctx.inputs)`; `return result.output` or, for a structured
     `agent.reads`/`writes` field, additionally write it to
     `ctx.state.<field>`). The factory body comes from the node's
     resolved template (`node.template` or, if unset,
     `infer_template(node.intent).suggestion`) via `templates/registry.py`
     + the matching `.tmpl` file, `string.Template`-substituted with the
     node's `intent` (as the `instructions=` string, safely
     `repr()`-escaped), `agent.tools`, and — for `orchestrator` — one
     tool-wrapper function per `agent.delegates_to` entry that imports
     and calls the child's `agents/<child_id>.py::build_agent` +
     `.run(...)` (I1: the child becomes a tool function, never a graph
     step).
8. Write `validate/dry_run.py`: `EXPECTED_NODES` computed from
   non-delegate-only node ids + `"__start__"`/`"__end__"` +
   `graph.BROADCAST_FORK_NODE_IDS.values()` (imported from the generated
   `graph.py`, never re-derived by string concatenation in the template —
   one source of truth). The `output_type` runtime assertion uses the
   **origin** type via a local `PORT_TYPE_RUNTIME_ORIGIN` table
   (`str`→`str`, `"list[str]"`→`list`, `"json"`→`dict` — B13, since
   `isinstance(x, list[str])` raises `TypeError` on a parameterized
   generic). The sample `graph.run(inputs=...)` value comes from a
   parallel `PORT_TYPE_SAMPLE_INPUT` table (`str`→`"swarm builder"`,
   `"list[str]"`→`["a", "b"]`, `"json"`→`{"key": "value"}`).
9. **Golden-render generation (B5 — subprocess, not in-process
   import).** After every file above is written, run
   `subprocess.run([sys.executable, "-c", "<render script>"],
   cwd=project_dir, env={**os.environ, "PYTHONPATH": str(project_dir /
   "src")})` — the render script imports `swarm_workflow.graph` and
   prints `graph.render()` to stdout, captured and written verbatim (no
   trailing newline appended, fact 31) to `validate/golden_render.txt`.
   No `uv sync` round-trip needed: the scaffolding process's own venv
   already has the identical pinned `pydantic-graph==2.43.0`. **Stated
   constraint (per review):** this is only valid while the generated
   project's runtime deps are a subset of the scaffolding process's own
   venv — true throughout Phase 2/Group 2 (no fill yet), and Group 4 must
   not reuse this helper once `programmatic.needs` packages are
   real-installed only inside the generated project's own venv.

## Design: `review.py` (final, post-review)

`review(graph: SwarmGraph) -> ReviewResult` where:

```python
@dataclass(frozen=True)
class Finding:
    code: str            # short machine id, e.g. "cycle", "orphan"
    message: str
    node_ids: tuple[str, ...]

@dataclass(frozen=True)
class ReviewResult:
    errors: list[Finding]
    warnings: list[Finding]
    @property
    def ok(self) -> bool: return not self.errors
```

Hard errors implemented (each a PLAN.md Phase-1 bullet; B10 additions
marked):

- unknown node ids on edges (defensive re-check — `models.py` already
  guarantees this at the document level; review.py re-derives from the
  edge list directly rather than trusting the caller didn't mutate);
- **exactly one of `agent`/`programmatic`/`decision`/`join` is set,
  matching `kind`** (B10a — otherwise `emit_graph.py` crashes on
  `node.join is None` for a `kind="join"` node with no `JoinSpec`);
- no path from `entry_node_id` to `exit_node_id`, computed over the
  structural-edge graph (`seq ∪ fanout ∪ join`) only;
- nodes unreachable from entry, same structural graph — delegate-only
  nodes are trivially exempt since they have no structural inbound edge
  to begin with, not via a special case;
- any cycle (DFS with a recursion-stack "gray" set over the structural
  edge set **plus** `branch` edges — a decision's branches are real
  dispatch edges for cycle purposes even though they don't become a
  literal `add_edge`; `delegate` edges are excluded, since a delegate
  relationship is a tool call, not a graph traversal, and legitimately
  may point at a node that also delegates elsewhere);
- a multi-successor node (≥2 structural outgoing edges) whose successors
  are not *all* `FanoutEdge`s agreeing on one `join_node_id`, where that
  `join_node_id` names a real `kind="join"` node;
- **B10b**: every `FanoutEdge` arm's target must itself have an outgoing
  `JoinEdge` into that same declared join node (checking edge-record
  agreement alone is not enough — the arm's *actual* edge into the join
  must exist, or the emitted graph.py has an arm with no successor,
  reproducing the exact `GraphValidationError` fact 14/B1 describes);
- a `decision` node with zero `BranchEdge`s targeting it, or zero
  `DecisionSpec.branches` entries, or a mismatch between the two sets
  (every `DecisionSpec.branches[i].target_node_id` must have a
  corresponding `BranchEdge` with the same `match`/target, and vice
  versa);
- **fact 27**: a branch target's `io.input_type` must equal the decision
  node's *source step's* `io.output_type` (the source = the node with a
  structural edge into the decision);
- port-type mismatch across a `SeqEdge`/`FanoutEdge`/`JoinEdge` (source
  node's `output_type` must equal target node's `input_type`; decision
  branch edges are exempted from this generic check since fact 27 gives
  them their own rule; delegate edges are exempt since no data flows via
  add_edge);
- **B10c (sink consistency)**: every structural sink's `io.output_type`
  must equal `exit_node_id`'s `io.output_type` (there is exactly one
  `output_type` on the built graph; every node that reaches
  `builder.end_node` must agree on it, mirroring the B1 end-edge rule);
- missing `intent` (empty string);
- state-ownership (I2): a `writes` entry not in `state_fields`; two nodes
  writing the same field; a `reads` field with no upstream writer
  (upstream = reachable via a directed path in the structural graph,
  excluding delegate);
- a node that is both a `delegate` target and a `seq`/`branch` target.

**Explicitly out of scope for `review.py` (B10d, stated not omitted):**
"a harness route with no PydanticAI counterpart protocol" (PLAN.md fact
23's Phase-1 bullet) is a *route*-level concern owned by Group 3's
`inherit/routes.py` plus the pipeline orchestrator (`pipeline.py`,
Group 4) — `review.py` only ever sees a `SwarmGraph`, which carries at
most a `ModelSelection` (provider/model strings), not a resolved
protocol. Noted here so its absence from this file reads as a scoping
decision, not an oversight.

Soft findings → warnings: a node whose `agent.delegates_to` names an id
that never resolves to an actual `agent` node target of a `DelegateEdge`;
a `join` node whose `io.output_type` is not `"list[str]"` while its
reducer is `list_append`/`list_extend` (the common case, not a hard
requirement); a `join` node with fewer than two inbound `JoinEdge`s
(structurally valid but a strange "join of one").

## Templates

`templates/registry.py` catalog; three subpackages
`templates/chat/agent.py.tmpl`, `templates/orchestrator/agent.py.tmpl`,
`templates/websearch/agent.py.tmpl` — one literal template file per
template id, each rendering into `agents/<id>.py` with the shared
`build_agent(model)` contract. **`string.Template` (`$identifier`
placeholders), not `str.format`/f-strings (B15)** — rendered content
(instructions text, tool lists) legitimately contains literal `{`/`}`
(e.g. a dict-shaped `output_schema` note or an f-string inside the
template's own generated code), which `str.format` would try to
interpret as a field reference. `scaffold.py`'s agent-file writer looks
up the node's `template` (defaulting through
`infer_template(node.intent).suggestion` when `None`) and substitutes
each template's placeholders (`$node_id`, `$instructions_repr` — the
node's `intent`, already `repr()`-escaped before substitution — `$tools`,
and for `orchestrator` a `$delegate_tools` block: one generated
`@agent.tool_plain`-style wrapper function per `agent.delegates_to`
entry, each importing and calling the child's
`agents/<child_id>.py::build_agent` + `.run(...)`, matching I1: the child
becomes a tool function, never a graph step).

## Tests

`tests/fixtures/graphs.py` — six positive + four negative graph builders
(Python functions returning `SwarmGraph`; negatives derived by mutating a
positive's edges/nodes, per Decision 2).
`tests/test_review.py`, `tests/test_template_registry.py` — unit, fast,
no `uv`, no subprocess.
`tests/test_emit_graph.py` — checks generated `graph.py` source text
against expected snippets/regexes for each codegen contract rule (1–12),
plus the whole-file `from __future__ import annotations`-first-line
invariant (B16) applied across every emitted `.py` file in a fixture
scaffold, not just `graph.py`.
`tests/test_codegen_fixtures.py` — `@pytest.mark.slow` (registered via
`tests/conftest.py`'s `pytest_configure`, never by editing the forbidden
`pyproject.toml` — Decision 3/4 correction). One test per positive
fixture: scaffold to a tmp dir, apply `apply_stub_fill()` only for the
programmatic-node fixture, then run — every subprocess call passing
`env={**os.environ, "UV_CACHE_DIR": str(UV_CACHE_DIR)}` explicitly (B14,
fact 10) — `uv sync`, keyless import (`env -u` stripping any ambient
credential vars, mirroring `spike/FINDINGS.md`'s reproduce script), and
`uv run python validate/dry_run.py`; assert golden/nodes/join-collection
per PLAN.md. One test per negative fixture asserting `review(...).ok is
False` with the expected `Finding.code`. One additional determinism
test: scaffolding the same fixture twice produces byte-identical output
for every emitted file (nice-to-have from the review, cheap, and would
have caught B5 on its own).

## Resolved after `planReview` (16 blockers found — all folded in)

The review subagent (Bedrock Claude Opus, role `planReview`) probed the
plan's riskiest claims directly against the pinned 2.43.0 wheels and
found 16 blockers. Full critique text is preserved in the pipeline log;
summary of every fix adopted:

- **B1 (multiple sinks need `add_edge(..., end_node)` each).**
  `exit_node_id` is not the only sink — `spike/branching` wires *both*
  `big` and `small` to `end_node`. **Fix:** `emit_graph.py` computes
  "structural successors" per node from `SeqEdge ∪ FanoutEdge ∪ JoinEdge`
  (never `BranchEdge`/`DelegateEdge`), and any structural node (step or
  join; never a `decision`, which always dispatches via branches) with
  **zero** structural successors gets `add_edge(<var>, builder.end_node)`.
  `exit_node_id` is used only for `output_type` inference and a review.py
  reachability check, not for deciding which nodes get an end edge.
  Added review rule: every such sink's `io.output_type` must equal the
  exit node's `io.output_type` (the graph has one `output_type`).

- **B2 (delegate-only children are unbuildable and unreviewable as
  drafted).** A node is **delegate-only** iff every inbound edge
  targeting it is a `DelegateEdge` and it is not `entry_node_id`. **Fix:**
  delegate-only nodes get **no** `builder.step` call and **no**
  `steps/<id>.py` file — only `agents/<id>.py` (the factory the parent
  orchestrator's factory imports and wraps as a tool). They are excluded
  from the fact-32 expected node set and exempt from review.py's
  "unreachable from entry" rule (reachability is checked only over the
  structural-edge graph, which never includes delegate edges in the
  first place, so this exemption falls out naturally once reachability
  is computed correctly).

- **B3 (`Literal` import missing).** `graph.py` imports `from typing
  import Literal` whenever the graph has ≥1 `decision` node (it's an
  **expression** inside `builder.match(Literal[...])`, not an annotation,
  so `from __future__ import annotations` does not defer it).

- **B4 (two identifier authorities).** Corrected: **`node_id` is
  `node.id` verbatim, never re-slugified in Group 2** — `models.py`'s own
  docstring says the frontend already derives a valid, stable identifier
  into `id` via `slugify.slugify_titles`. Group 2 does not call
  `slugify_titles` at all; it only imports `slugify.IDENTIFIER_RE` (and
  the `keyword` module) into **review.py** to enforce contract rule 11's
  "validated against a Python-identifier regex plus a keyword blocklist"
  half (the "derived/deduplicated" half is the frontend's job, per
  `models.py`). `node.id` is used verbatim as the `node_id=` argument,
  the module filename (`steps/<id>.py`, `agents/<id>.py`), the function
  name, and the fact-32 fork id (`f"{node.id}_broadcast_fork"`). This
  removes the double-bookkeeping risk entirely — one identifier, one
  source of truth.

- **B5 (in-process `importlib` golden render corrupts across scaffolds
  in a long-lived process).** Confirmed by probe: `sys.modules` caches
  the first scaffold's `swarm_workflow` package tree; a second scaffold
  in the same process silently imports the **first** project's step
  modules. **Fix:** golden-render generation runs `graph.render()` in a
  **subprocess** (`sys.executable -c "..."`, `PYTHONPATH=<project>/src`,
  no `uv sync` needed since the scaffolding process's own venv already
  has the identical pinned `pydantic-graph==2.43.0`). No process-global
  state, safe for repeated/concurrent scaffolds. Documented constraint:
  this subprocess approach is only valid **before** Phase 3 fill adds a
  `programmatic.needs` package the scaffolding venv doesn't have (Group
  4's concern, noted for that group).

- **B6 (six positive fixtures need type-correct filled bodies, not just
  one).** **Fix:** every agent-kind step and every template factory body
  is **pre-filled by scaffold.py itself** with real, mechanical,
  contract-compliant code (see B6.5 note below) — no model, no stub-fill
  helper needed for agent nodes at all. Only `programmatic`-kind step
  bodies default to `raise NotImplementedError(...)`, and the codegen
  test suite supplies a small `apply_stub_fill()` helper (in
  `tests/fixtures/`) that overwrites just those regions with
  fixture-specific, type-correct literal code before running the
  validation gate — exactly one call site, used by exactly the "mixed
  graph with a programmatic node" fixture (the other five fixtures need
  no stub-fill at all since they have no programmatic node).

  **B6.5 — new decision, not in the original draft.** Since Group 2 has
  no model at all, and every piece of information a template factory
  needs (`intent`, `agent.tools`, `agent.delegates_to`) is already on the
  `SwarmNode`, `scaffold.py` deterministically renders the **entire**
  `agents/<id>.py` factory body and the **entire** agent-kind step body
  by template substitution — no `NotImplementedError` placeholder for
  these. This makes codegen contract rule 3 ("agents are built from
  `ctx.deps.model`; no `Agent` at import time") **structural** rather
  than prompt-enforced, satisfies the review's "who emits `agent =
  build_agent(ctx.deps.model)`" gap, and is why all six positive fixtures
  can pass the validation gate with zero model involvement. The
  `swarm:begin`/`swarm:end` markers are still emitted around this
  content (Phase 3/Group 5 may still choose to *refine* it later), but
  the **default** is already a working implementation, not a stub.

- **B7 (`default_factory=<string>` is invalid; env-override logic has no
  owner).** **Fix:** `ResolvedModel` (see B8) carries `helper_source: str`
  — a literal multi-line source block (constants + a named
  `_resolve_default_model()` function, mirroring both spike `deps.py`
  files line-for-line) that already bakes in the `SWARM_MODEL`/
  `SWARM_BASE_URL`/`SWARM_API_KEY_ENV` env-override logic — plus
  `default_factory_name: str` naming that function so `scaffold.py`
  writes `model: Model | str = field(default_factory=<default_factory_name>)`.
  Whoever builds `ResolvedModel` (Group 3, or Group 2's own fallback for
  its tests) owns the *entire* env-override behavior as one literal
  source blob; `scaffold.py` only splices it in verbatim above the
  `Deps` dataclass, exactly where both spikes put it.

- **B8 (seam still coupled; extras/deps conflated; `reasoning_effort`
  dropped).** **Fix:** `ResolvedModel` moves out of `scaffold.py` into
  `src/swarm_builder/compile/__init__.py` (a file already in Group 2's
  owned set) — a neutral leaf module both `scaffold.py` and Group 3's
  future `inherit/routes.py` can import with **no** import in the other
  direction. Split into `pyproject_extras: tuple[str, ...]` (bracketed
  extras only, e.g. `("bedrock",)`) and template/programmatic deps stay
  separate, plain PyPI requirement strings, never merged into the
  `pydantic-ai-slim[...]` bracket. `reasoning_effort` is dropped from
  Group 2's scope deliberately — nothing in the codegen contract or the
  scaffold shape consumes it (it's a compile-time agent-run parameter,
  not a generated-project shape); noted in the final report as an
  explicit omission for Group 3/5 to place.

- **B9 (marker strings are a cross-group contract, not a private
  literal).** **Fix:** four formatter functions
  (`imports_marker_begin/end(node_id)`, `body_marker_begin/end(node_id)`)
  live in `compile/__init__.py` as the single source of truth for the
  exact marker text (`# --- swarm:imports <id> ---` /
  `# --- swarm:end-imports <id> ---` / `# --- swarm:begin <id> ---` /
  `# --- swarm:end <id> ---`), with the indentation-tolerance rule
  (`body_marker_*` lines are indented one level inside the function body;
  a parser must match ignoring leading whitespace) stated in their
  docstrings for Group 4's `boundary.py` to rely on.

- **B10 (four missing review.py rules).** Added: (a) exactly one of
  `agent`/`programmatic`/`decision`/`join` set, matching `kind`; (b) every
  `FanoutEdge` arm's target must itself have an outgoing `JoinEdge` into
  the declared join (not just agreement on `join_node_id` in the edge
  record); (c) the B1 sink/output-type-consistency rule; (d) "unmappable
  route protocol" is explicitly **out of scope for `review.py`** — it is
  a route-level (Group 3 `inherit/routes.py` + pipeline) concern, stated
  here so it isn't silently missing without explanation.

- **B11 (fan-out predicate used `seq`-only successors).** Fixed to count
  `SeqEdge ∪ FanoutEdge ∪ JoinEdge` (same structural set as B1) — matches
  what `build()` actually sees as raw `add_edge` calls, so the fact-32
  prediction can't diverge from reality for a mixed-kind fan-out.

- **B12 (`BROADCAST_FORK_NODE_ID` collides across multiple fan-out
  sources).** Changed to a dict constant,
  `BROADCAST_FORK_NODE_IDS: dict[str, str]`, keyed by source node id,
  emitted only when non-empty (omitted entirely for a graph with no
  fan-out, matching `spike/linear`/`spike/branching`).

- **B13 (`isinstance(x, list[str])` raises `TypeError`; sample-input
  derivation was unspecified).** `dry_run.py`'s generated assertion uses
  the **origin** type (`isinstance(out, list)` / `dict` / `str`), via a
  small local table in `scaffold.py` (not `models.py`, which Group 2 does
  not edit): `PORT_TYPE_RUNTIME_ORIGIN` and `PORT_TYPE_SAMPLE_INPUT`
  (literal Python source for a representative `graph.run(inputs=...)`
  value per `PortType`).

- **B14 (test plan never set `UV_CACHE_DIR` explicitly).** Every
  subprocess call in the codegen test suite passes
  `env={**os.environ, "UV_CACHE_DIR": str(UV_CACHE_DIR)}` explicitly
  (fact 10) — stated here so the implementation isn't tempted to rely on
  ambient inheritance.

- **B15 (`str.format` collides with literal braces in generated code).**
  Template files use `string.Template` (`$identifier` placeholders), not
  `str.format`/f-strings, since rendered content includes dict literals
  and JSON-schema-shaped text that legitimately contains `{`/`}`.

- **B16 (missing `__init__.py`s, incomplete `pyproject.toml` fields, no
  whole-project `from __future__ import annotations` invariant test).**
  `steps/__init__.py` and `agents/__init__.py` added to the emitted file
  set (present in all four spikes). `pyproject.toml` always carries
  `name`, `version`, `readme = "README.md"`, `requires-python`, and
  `[build-system] hatchling` (matching every spike file exactly, fact
  20/28). `tests/test_emit_graph.py`/`tests/test_codegen_fixtures.py`
  add a walk-every-emitted-`.py`-file assertion for contract rule 12.
  `AgentSpec.output_schema` is explicitly **not implemented** in Group 2
  (no required fixture exercises it; noted as a follow-up for whichever
  group needs structured output).

**Nice-to-haves adopted:** `infer_template` uses keyword-count scoring
(ties → `chat`) rather than first-match-wins, matching PLAN.md's
"scores... against per-template keyword sets" wording exactly (this is
also the parity contract Group 6's `infer.ts` must copy). The
`JoinSpec.initial_factory` derivation table
(`list_append`/`list_extend`→`list`, `dict_update`→`dict`, `sum`→`int`) is
restated verbatim in `scaffold.py`'s docstring. Decision 3 (the `slow`
marker) is corrected to only ever mention `conftest.py`, since decision 4
already forbids editing `pyproject.toml` — decision 3's `pyproject.toml`
mention above is superseded by decision 4 and by this note. A
determinism test (scaffold the same fixture twice, assert byte-identical
output) is added to `tests/test_codegen_fixtures.py`.

## Files touched (all new, none from the forbidden list)

- `src/swarm_builder/templates/__init__.py`
- `src/swarm_builder/templates/registry.py`
- `src/swarm_builder/templates/chat/agent.py.tmpl`
- `src/swarm_builder/templates/orchestrator/agent.py.tmpl`
- `src/swarm_builder/templates/websearch/agent.py.tmpl`
- `src/swarm_builder/compile/__init__.py`
- `src/swarm_builder/compile/graph_ir.py` (new, not in the original file list -- shared structural-graph analysis both `emit_graph.py` and `review.py` import, added to eliminate the B4/B11/B12 two-copy-drift risk)
- `src/swarm_builder/compile/emit_graph.py`
- `src/swarm_builder/compile/scaffold.py`
- `src/swarm_builder/compile/review.py`
- `tests/conftest.py`
- `tests/fixtures/__init__.py`
- `tests/fixtures/graphs.py`
- `tests/fixtures/stub_fill.py` (new -- test-only deterministic body filler for programmatic nodes, standing in for the out-of-scope-for-Group-2 fill agent)
- `tests/test_template_registry.py`
- `tests/test_review.py`
- `tests/test_emit_graph.py`
- `tests/test_codegen_fixtures.py`

## Post-implementation finding: PLAN.md fact 9 does not reproduce against the installed 2.43.0 wheel at run time

While running the codegen suite for real (not just constructing objects),
the `websearch` fixture's `TestModel` dry run crashed:

```
TypeError: 'WebSearchTool' object is not callable
```

Re-probed directly, isolating each half of fact 9:

- `Agent('test', builtin_tools=[WebSearchTool()])` -> `TypeError` (fact 9 confirmed: no such parameter).
- `Agent('test', tools=[WebSearchTool()])` -> `AttributeError` (fact 9 confirmed).
- `Agent('test', toolsets=[WebSearchTool()])` -> **constructs successfully** (fact 9's construction-time claim holds) but **`.run()` raises `TypeError: 'WebSearchTool' object is not callable`** -- `toolsets=` expects an `AbstractToolset` instance or a dynamic-toolset factory callable; `WebSearchTool` is an `AbstractNativeTool`, unrelated to `AbstractToolset`, and is not callable. Reproduced identically inside `spike/linear/.venv` (same pinned `pydantic-ai-slim==2.43.0`), so this is not an environment drift issue -- fact 9 itself does not hold for a real run in the currently-installed 2.43.0 wheel, only for construction.

The verified-working call site: `capabilities=[NativeTool(WebSearchTool(optional=True))]`, importing `NativeTool` from `pydantic_ai.capabilities` (not the top-level `pydantic_ai` namespace, where it is not exported). Confirmed end-to-end: constructs with no key, and a `TestModel`-backed run completes -- **provided** the `TestModel` (or a subclass) also declares it supports no native tools, because plain `TestModel()` unconditionally raises `UserError: TestModel does not support built-in tools` for *any* native tool regardless of `optional=`. `optional=True` only governs the *unsupported-model* fallback path (silently drop vs. raise `UserError` naming the tool); it does not turn off `TestModel`'s own blanket native-tool rejection, which is a separate, earlier check in `TestModel._request`.

**Fix applied (not merely documented):**
- `templates/websearch/agent.py.tmpl` now emits `capabilities=[NativeTool(WebSearchTool(optional=True))]` with the corrected imports, and its module docstring records the full probe trail so a future reader does not "fix" it back to the PLAN.md text.
- `scaffold.py`'s generated `validate/dry_run.py` always defines and uses a local `_KeylessTestModel(TestModel)` subclass overriding `supported_native_tools()` to return `frozenset()`, which is what actually makes an optional native tool silently drop instead of raising -- needed unconditionally (not just for the websearch fixture) since every generated project's dry run must stay keyless regardless of which templates a graph uses.
- Verified end-to-end: the `websearch` positive fixture now passes `uv sync` -> keyless import -> `validate/dry_run.py` with no key, exercised in `tests/test_codegen_fixtures.py`.

This is reported here, not silently "corrected" back to PLAN.md's fact 9 text, per the instruction to flag any PLAN.md fact that does not reproduce.
