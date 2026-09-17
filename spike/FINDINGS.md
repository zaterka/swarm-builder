# Group 0 spike — findings

Evidence gathered by actually running code, not by reasoning about
documentation. Every claim below has a command shown further up this
session's transcript (and reproducible with the commands in
"Reproduce" at the end of each project's section). Environment:

- `uv 0.11.18` (Homebrew), Python interpreters available via `uv python
  list`; spike projects pin `.python-version` to `3.14.5`.
- `UV_CACHE_DIR` explicitly set for every `uv` invocation to
  `swarm-builder/.uv-cache` (the default `~/.cache/uv` is not writable
  in this environment — `Operation not permitted` on
  `~/.cache/uv/sdists-v9/.git`).
- No `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `SWARM_API_KEY` present for
  any import/dry-run check (explicitly unset with `env -u ...` on top of
  an already-keyless shell).
- `pydantic-ai-slim==2.43.0`, `pydantic-graph==2.43.0` pinned exactly in
  every project's `pyproject.toml`.

## 1. Exact working API surface (confirmed signatures)

All obtained via `inspect.signature` against the installed 2.43.0
wheels, reproduced in this session:

```python
GraphBuilder.__init__(self, *, name=None, state_type=NoneType,
    deps_type=NoneType, input_type=NoneType, output_type=NoneType,
    auto_instrument=True)

GraphBuilder.build(self, validate_graph_structure=True) -> Graph[...]

GraphBuilder.step(self, call=None, *, node_id=None, label=None)
    -> Step | Callable[[StepFunction], Step]
    # usable BOTH as @builder.step(node_id=...) decorator AND as
    # builder.step(existing_fn, node_id=...) -- both forms verified
    # working (see spike/linear vs the earlier inline probes).

GraphBuilder.add_edge(self, source, destination, *, label=None) -> None

GraphBuilder.join(self, reducer, *, initial=<Unset>,
    initial_factory=<Unset>, node_id=None, parent_fork_id=None,
    preferred_parent_fork='farthest') -> Join[...]

GraphBuilder.decision(self, *, note=None, node_id=None) -> Decision[...]
GraphBuilder.match(self, source, *, matches=None) -> DecisionBranchBuilder[...]
DecisionBranchBuilder.to(self, destination, /, *extra_destinations,
    fork_id=None) -> DecisionBranch
Decision.branch(self, branch: DecisionBranch) -> Decision   # NEW object

builder.start_node / builder.end_node   # properties, not callables

Graph.run(self, *, state=None, deps=None, inputs=None, span=None,
    infer_name=True) -> OutputT      # keyword-only, bare value return
Graph.render(self, *, title=None, direction=None) -> str
Graph.nodes            # dict-like mapping of node_id -> node object

StepContext.__init__(self, *, state, deps, inputs)   # .inputs, not .input

Agent.__init__(self, model=None, *, output_type=str, instructions=None,
    ..., deps_type=object, retries=None, tools=(), toolsets=None,
    defer_model_check=False, ...)
    # NOTE: no `builtin_tools` parameter in 2.43.

TestModel.__init__(self, *, call_tools='all', custom_output_text=None,
    custom_output_args=None, seed=0, model_name='test', profile=None,
    settings=None)
```

`pydantic_graph` public surface: `BaseNode, Decision, Edge, End,
EndMarker, EndNode, ErrorMarker, Fork, Graph, GraphBuilder, GraphRun,
GraphRunContext, GraphRuntimeError, GraphSetupError, GraphTask,
GraphTaskRequest, Join, JoinItem, JoinNode, ReduceFirstValue,
ReducerContext, ReducerFunction, StartNode, Step, StepContext,
TypeExpression, ... reduce_dict_update, reduce_list_append,
reduce_list_extend, reduce_null, reduce_sum, ...`.

**Reducers available** (from `dir(pydantic_graph)`): `reduce_list_append`,
`reduce_list_extend`, `reduce_dict_update`, `reduce_sum`, `reduce_null`,
plus `ReduceFirstValue` (a class, not a function — not counted among the
five PLAN.md fact 13 names, but present). The plan's fact 13 list of five
names is accurate. **Used in the spike:** `reduce_list_append` for
`spike/fanout` (accumulates every fan-in value into a list — the
canonical "collect all branch outputs" reducer, and the most obviously
correct choice for a generic join since it makes no assumption about
element type beyond "appendable").

## 2. Golden `render()` text per project

### `spike/linear/validate/golden_render.txt` (also `linear_custom_baseurl`, identical wiring)

```
stateDiagram-v2
  intake
  research
  summarize

  [*] --> intake
  intake --> research
  research --> summarize
  summarize --> [*]
```
(no trailing newline — `graph.render()` does not emit one; the golden
file byte-matches exactly, confirmed with `repr(data[-20:])`.)

### `spike/branching/validate/golden_render.txt`

```
stateDiagram-v2
  classify
  state decision <<choice>>
  note right of decision
    classify by length
  end note
  big
  small

  [*] --> classify
  classify --> decision
  decision --> big
  decision --> small
  big --> [*]
  small --> [*]
```

Note: the `note right of decision ... end note` block only appears
because `builder.decision(node_id="decision", note="classify by
length")` was given a `note=`. Passing no note omits the block entirely
(confirmed in the earlier bare probe `branch2.py`, which had no note and
rendered without it).

### `spike/fanout/validate/golden_render.txt`

```
stateDiagram-v2
  split
  state split_broadcast_fork <<fork>>
  left
  right
  state join <<join>>

  [*] --> split
  split --> split_broadcast_fork
  split_broadcast_fork --> left
  split_broadcast_fork --> right
  left --> join
  right --> join
  join --> [*]
```

## 3. Exact `graph.nodes` key sets, including the synthetic fork node's real id

| Project | `graph.nodes` keys |
|---|---|
| `linear` / `linear_custom_baseurl` | `__start__, intake, research, summarize, __end__` |
| `branching` | `__start__, classify, decision, big, small, __end__` |
| `fanout` | `__start__, split, left, right, join, __end__, split_broadcast_fork` |

**Synthetic fork node id rule, confirmed exactly:** `build()` names the
injected fork node `<source_node_id>_broadcast_fork`, where
`source_node_id` is the id of the step that has multiple plain
successors (here `split`). This matched PLAN.md fact 13's example
(`a_broadcast_fork` for a node named `a`) and reproduced identically for
`split` → `split_broadcast_fork`. **Implication for the emitter:** the
synthetic id is deterministic and computable as
`f"{fanout_source_node_id}_broadcast_fork"` — the validator does not
need to introspect `graph.nodes` blindly; it can *predict* the expected
node set from the canvas graph plus this naming rule, then assert
equality (which is exactly what `validate/dry_run.py` does in each
spike).

## 4. Facts from PLAN.md that turned out inaccurate or incomplete when actually run

Flagged loudly, as instructed, rather than silently worked around:

1. **Fact 14's decision-output-propagation detail is incomplete (not
   wrong, but a real gap that would have produced a broken emitter).**
   PLAN.md fact 14 is correct about the immutability rule and the
   emission-order requirement, and both reproduced exactly as described
   (`GraphValidationError: The following nodes have no outgoing edges:
   ['decision']` when the edge is added before rebinding the branch
   chain). **What PLAN.md does not mention:** the value a branch step
   receives via `ctx.inputs` is the **matched Literal value returned by
   the decision's source step**, not the original graph input that was
   threaded into that step. Concretely: `classify` returns
   `Literal["big","small"]`, and the `big`/`small` steps downstream of
   the decision receive `ctx.inputs == "big"` / `"small"` — the
   classification label — not the original string that was classified.
   This surprised the first version of `spike/branching/validate/dry_run.py`,
   which asserted `out == "BIG:a long input string"` and got
   `'BIG:big'` instead. This is **exactly the kind of "no runtime type
   checking at edges" behavior PLAN.md itself warns about in a different
   context** ("Port-type mismatch... pydantic-graph performs no runtime
   type checking at edges"), but PLAN.md's own fact 14 narrative doesn't
   flag that decision branches specifically forward the *classification
   value*, not the pre-classification payload. **Implication for
   `emit_graph.py` / `models.py`:** a `decision` node's branch target
   steps must be typed to accept whatever the decision's match values
   are (here `Literal["big","small"]`, i.e. effectively `str`), not the
   upstream payload type; if the real payload must reach the branch
   step, the workflow author must have the classifying step carry it
   forward explicitly (e.g. via `ctx.state`, as this spike does with
   `ctx.state.length_bucket`) rather than expect `ctx.inputs` to carry
   it through the decision. This is a schema implication: `DecisionSpec`
   should document (and Phase 1 should probably warn about) the fact
   that data does not "pass through" a decision node the way it does
   through a plain sequential edge.

2. **Extras genuinely gate import, confirming fact 23 exactly, with one
   added nuance.** Constructing `OpenAIChatModel(...)` without the
   `[openai]` extra raises `ModuleNotFoundError` inside
   `pydantic_ai/models/openai.py`, re-raised by the library itself as
   `ImportError: Please install \`openai\`...`. This matches fact 23's
   claim precisely. **Added nuance not spelled out in PLAN.md:** the
   *known-name* bedrock path (`Agent("bedrock:...", defer_model_check=True)`)
   constructs successfully with **zero extras installed at all** — no
   `boto3`, no `[bedrock]` extra needed for construction, only for an
   actual `.run()` call (confirmed: `Agent("bedrock:...")` without
   `defer_model_check` fails immediately with `UserError: You must
   provide a region_name or a boto3 client for Bedrock Runtime` — a
   *runtime* construction failure, not an import failure). This matters
   for `pyproject.toml` extras derivation (fact 23): a generated
   project's **import-time** keyless gate does not actually exercise
   whether the declared extra is *sufficient* for a real run — it only
   proves the *module* imports. The extra is still needed for `uv sync`
   to install the right packages for a live run later, but the
   Phase-5 "keyless import" step by itself would still pass even if the
   wrong bedrock extra were entirely omitted, provided
   `defer_model_check=True` is used and the step body never actually
   calls `agent.run()` against Bedrock. **Recommendation:** the emitter
   should still declare the extra per fact 23 (needed for a real run to
   work at all), but the validation gate's true test of "does this
   project need `[bedrock]`" is exercised by nothing in the current
   plan — this is a coverage gap worth noting for Group 4's
   `validate.py`, not a blocker for Group 0.

3. **`builder.step` accepts both decorator and plain-call forms; PLAN.md
   fact 6 only shows the decorator form.** Not inaccurate, just
   incomplete: `builder.step(fn, node_id="x")` (calling it directly on an
   already-defined async function, used throughout `graph.py` in every
   spike here to keep step bodies in separate step modules per the
   Phase-2 scaffold shape) works identically to `@builder.step(node_id="x")`
   used inline. This is a good thing for the emitter — it means
   `graph.py` can `from .steps.<slug> import <fn>` and then
   `builder.step(<fn>, node_id=<slug>)`, matching the target
   scaffold's `steps/<node_slug>.py` layout described in PLAN.md's
   Phase 2 section, without needing the decorator to live inside
   `graph.py` itself. Confirmed working end-to-end in all four spikes.

4. **Everything else in PLAN.md's verified facts (6, 7, 9, 10, 12–20,
   22, 23) reproduced exactly as stated, with no discrepancies found.**
   In particular: `Graph.__init__` was never called directly and never
   needed to be; `ctx.inputs` (never `.input`) worked throughout;
   `'json'` was never used as a bare annotation (not applicable to this
   spike, since no node used a `json`-typed port, but the PortType→
   annotation table's necessity is accepted as correctly reasoned from
   the `NameError` PLAN.md already demonstrated); `uv sync` needed
   `UV_CACHE_DIR` set exactly as fact 10 says; a bare `src/<pkg>/`
   layout needed no `[tool.hatch.build]` section (fact 20) — **though
   note new finding below (§5) about `readme` being a hard `uv sync`
   requirement, which is adjacent to fact 20 but not covered by it.**

## 5. One additional gap PLAN.md does not mention at all

**`hatchling` requires a `readme` file to exist on disk before `uv
sync` will build the project**, even though `pyproject.toml` declares
`readme = "README.md"` and the field is otherwise optional-looking.
Without a `README.md` file present, `uv sync` fails during the
`hatchling` build step with `OSError: Readme file does not exist:
README.md`, not during dependency resolution — the error is easy to
misread as a dependency problem. This is unrelated to the `UV_CACHE_DIR`
trap (fact 10) but is exactly the same *shape* of failure: a
build-tool prerequisite that looks like something else. **Implication
for `scaffold.py`:** the Phase-2 scaffolder must always emit a
`README.md` (PLAN.md's own repository layout already includes one, so
this is not a scope change — just recording that omitting it is a hard
`uv sync` failure, not a soft one).

## 6. Concrete implications for `models.py` (the schema)

- **`DecisionSpec` needs a documented data-flow caveat.** Per finding
  §4.1: branch target steps receive the decision's match value (the
  classifying step's return value), not the original upstream payload.
  If `models.py`'s `DecisionSpec.branches` maps `match -> targetNodeId`
  (as PLAN.md already specifies), the schema should also make clear —
  in a docstring or Phase-1 warning — that a branch target step's
  declared `io.inputType` must match the **decision source step's
  output type**, not any node further upstream. This is a natural
  consequence of the graph's data flow and needs no schema field
  change, but it is worth a Phase-1 review rule: *"a branch target's
  declared inputType must equal the decision's source step's declared
  outputType."*
- **`JoinSpec.reducer` mapping to `ReducerId` is validated correct
  end-to-end.** `ReducerId = Literal['list_append', 'list_extend',
  'dict_update', 'sum']` (PLAN.md) maps cleanly to
  `reduce_list_append`, `reduce_list_extend`, `reduce_dict_update`,
  `reduce_sum` — all four confirmed present and importable. `reduce_null`
  exists too but has no `ReducerId` counterpart in the plan's schema;
  this spike did not need it, so no action needed, but the emitter's
  reducer-name lookup table should have exactly four entries matching
  `ReducerId`'s four literals, not five.
- **The synthetic broadcast-fork node id is deterministic and
  predictable** (§3): `f"{fanout_source_node_id}_broadcast_fork"`. A
  `models.py`/`review.py` implication: Phase-1's node-set validation
  (and Phase-5's node-set assertion) can compute the *expected*
  `graph.nodes` key set purely from the canvas document — canvas node
  ids + `__start__`/`__end__` + one predicted `<fanout_source>_broadcast_fork`
  per `FanoutEdge` source that has ≥2 plain successors — without ever
  needing to special-case "whatever `build()` happens to produce."
- **`PortType` → annotation table is validated as necessary** (fact 17
  reproduced, not directly re-probed in this spike since no `json` port
  was used, but nothing contradicts it and the underlying mechanism —
  Python evaluating a bare identifier at class-body / function-signature
  time — is exactly why `'json'` cannot work as a raw annotation).

## 7. Concrete implications for `emit_graph.py` (the emitter)

- **Two-phase decision emission is mandatory, not just a nice-to-have.**
  `emit_graph.py` must compute the *entire* branch list for a `decision`
  node before emitting *any* `add_edge` line that targets it. The
  natural code-generation shape is: (a) emit `builder.decision(...)`
  bound to a Python variable, (b) emit N `= <var>.branch(...)`
  reassignment lines (rebinding the same variable each time, matching
  the pattern this spike uses), (c) only then emit the `add_edge` lines
  for edges whose target is this decision. This ordering constraint is
  purely about **emission order of generated `graph.py` source lines**,
  not about anything dynamic at runtime — so it is a straightforward,
  fully deterministic code-generation rule with no edge cases beyond
  "topologically sort decision-branch emission before decision-target
  edges."
- **`builder.step(fn, node_id=...)` (plain-call form) is the right
  emission target, not the decorator form**, because it lets
  `graph.py` stay a thin wiring file that imports step functions from
  `steps/<slug>.py` — exactly the Phase-2 scaffold shape PLAN.md
  specifies, and exactly what all four spikes here do. The decorator
  form would force step bodies to live inside `graph.py` itself, which
  PLAN.md's own file layout rules out (`graph.py` is "generated, never
  model-written"; step bodies belong in `steps/*.py`).
- **The fan-out/join emission rule (contract rule 7) is simple to check
  mechanically at emit time:** for any step with ≥2 outgoing plain
  `add_edge` targets, the emitter must instead emit those edges as a
  fan-out into a `builder.join(...)` (never two bare `add_edge` calls
  to distinct destinations). This spike's `graph.py` files demonstrate
  the exact two shapes side-by-side is unnecessary in generated code —
  the emitter should simply never produce the lossy shape at all, since
  Phase 1's review (per PLAN.md) already rejects any canvas graph with
  unjoined multi-successor fan-out before `emit_graph.py` ever runs.
- **Golden-render generation should be emitted mechanically, not
  hand-authored, in the real pipeline**: Phase 2's scaffolder can emit
  the golden diagram by literally invoking `graph.render()` once at
  scaffold time (the same interpreter validating the project) and
  writing the result to `validate/golden_render.txt`, exactly as this
  spike created its three golden files by running the built graph and
  capturing `graph.render()` output byte-for-byte (confirmed via
  `repr(data[-N:])` byte comparisons in this session). No hand-tuning
  of the golden text is needed or was done here — every golden file
  was generated by actually running the target project once.
- **`Deps`/`deps.py` emission needs the two-path branch from fact 22**,
  confirmed both work under the keyless gate: a known-name route emits
  a bare prefixed string default (`spike/linear`), and a custom-baseURL
  route emits the structural `OpenAIChatModel(id,
  provider=OpenAIProvider(base_url=..., api_key=...))` form
  (`spike/linear_custom_baseurl`) — both constructed with zero keys
  and zero network access, both passed `uv sync` → keyless import →
  `TestModel` dry run.

## 8. What passes — summary table

| Project | `uv sync` | keyless import | `build()` | `render()` golden | `graph.nodes` set | `TestModel` run |
|---|---|---|---|---|---|---|
| `spike/linear` | ✅ | ✅ | ✅ | ✅ | ✅ (5 keys, no fork) | ✅ `'SUMMARY: success (no tool calls)'` |
| `spike/linear_custom_baseurl` | ✅ | ✅ | ✅ | ✅ | ✅ (5 keys, no fork) | ✅ `'SUMMARY: success (no tool calls)'` |
| `spike/branching` | ✅ | ✅ | ✅ | ✅ (incl. `<<choice>>` + note) | ✅ (6 keys, no fork) | ✅ both branches dispatch correctly |
| `spike/fanout` | ✅ | ✅ | ✅ | ✅ (incl. `<<fork>>` + `<<join>>`) | ✅ (7 keys, incl. `split_broadcast_fork`) | ✅ joined `['R:x','L:x']`, both branches present |

## 9. Reproduce

From `/Users/pedro.zaterka/factored-projects/swarm-builder`:

```bash
export UV_CACHE_DIR=/Users/pedro.zaterka/factored-projects/swarm-builder/.uv-cache
for proj in spike/linear spike/linear_custom_baseurl spike/branching spike/fanout; do
  echo "=== $proj ==="
  (cd "$proj" && uv sync)
  (cd "$proj" && env -u AWS_PROFILE -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY \
      -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u SWARM_API_KEY \
      uv run python -c "import swarm_workflow.graph; print('IMPORT OK')")
  (cd "$proj" && env -u AWS_PROFILE -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY \
      -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u SWARM_API_KEY \
      uv run python validate/dry_run.py)
done
```

All four projects were run with this exact loop, with no API key or AWS
credential present in the environment at any point, and all checks
passed.
