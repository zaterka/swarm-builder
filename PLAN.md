# Swarm Builder — Implementation Plan (v1)

> **Where this file belongs.** This plan was authored inside the Factored Harness workspace for convenience. Copy it to `/Users/pedro.zaterka/factored-projects/swarm-builder/PLAN.md` and start a fresh session with that folder as the workspace root. Every path below is written from that destination's point of view; the harness checkout is referred to as `../Factored-Harness`.

> **Architecture revision (after probing).** An earlier draft made the DeepSeek Harness the compile backbone via its TypeScript SDK. Probes then showed PydanticAI reaches the same Bedrock models **directly** through the user's AWS SSO profile, and that a plain PydanticAI agent with three hand-written tools already performs the code-fill job. The harness tie-in is therefore **dropped**: Swarm Builder is a self-contained Python + React application that *reads* harness settings to inherit model routes, but does not spawn or depend on `dsh`. See [Why not the harness](#why-not-the-harness) for the evidence and what this costs.

A separately launched application: a draw.io/excalidraw-style canvas where a user drags agent and programmatic nodes, connects them, describes intent in plain English, and clicks **Compile**. Compilation fills template-shaped holes with a PydanticAI coding agent, then proves the generated PydanticAI project actually imports and runs.

## Table of contents

- [Goal and success criteria](#goal-and-success-criteria)
- [Decisions already made](#decisions-already-made)
- [Why not the harness](#why-not-the-harness)
- [Verified facts from probes](#verified-facts-from-probes)
- [Spike amendments (established in Group 0)](#spike-amendments)
- [Coding conventions (`.claude/rules/`)](#coding-conventions)
- [Repository layout](#repository-layout)
- [Architecture and data flow](#architecture-and-data-flow)
- [The graph document (schema)](#the-graph-document-schema)
- [HTTP API](#http-api)
- [Templates](#templates)
- [The compile pipeline](#the-compile-pipeline)
- [Codegen contract](#codegen-contract)
- [Frontend](#frontend)
- [Grouped changes by subsystem](#grouped-changes-by-subsystem)
- [Dependency versions](#dependency-versions)
- [Edge cases](#edge-cases)
- [Failure modes](#failure-modes)
- [Tests](#tests)
- [Acceptance criteria](#acceptance-criteria)
- [Assumptions](#assumptions)
- [Explicitly out of scope for v1](#explicitly-out-of-scope-for-v1)

-----

<a id="goal-and-success-criteria"></a>
## Goal and success criteria

**Goal.** Ship a local-first web app, launched independently of the harness, in which a user builds an agent workflow visually, compiles it via the harness, and exports a runnable PydanticAI project.

**Success criteria.**

1. Launching the app in the new folder serves a canvas at a printed `127.0.0.1` URL, with no harness repo changes and no harness process required.
2. A user can create agent nodes, programmatic nodes, edges, and per-node plain-English intent; the graph persists to a JSON file and reloads verbatim.
3. Clicking **Compile** streams progress and produces a project directory containing a `pydantic-graph` `GraphBuilder` workflow whose nodes correspond 1:1 to canvas nodes.
4. The compiled project passes an automated validation gate — dependency sync, import, graph build/render, and a `TestModel` dry run — **with no model API key present**.
5. Three templates (`chat`, `orchestrator`, `websearch`) are selectable, and an unset node's template is inferred from its prompt text.
6. **Export** yields a self-contained folder the user can `uv run` outside Swarm Builder.
7. Compilation and the generated project both inherit the harness's configured models: no model is pinned by Swarm Builder, and the picker shows which inherited route a compile will spend.

-----

<a id="decisions-already-made"></a>
## Decisions already made

Confirmed by the developer in the planning interview:

| Decision | Choice |
|---|---|
| Location | Sibling folder `/Users/pedro.zaterka/factored-projects/swarm-builder`, outside the harness repo |
| Compile driver | **Direct PydanticAI coding agent** in-process; no `dsh` subprocess, no SDK client (revised after probing) |
| Harness relationship | Read-only: inherit provider/model routes from `$DSH_HOME/settings.yaml`; the app runs without the harness present |
| Compile output | Runnable PydanticAI project on disk from templates + LLM fill-in, **validated** |
| Storage | Plain JSON graph files + generated project directories; no database |
| Canvas | React Flow (`@xyflow/react`) with a hand-drawn theme |
| Compile model | Inherited from harness settings when present, else explicit env config; per-workflow override via a picker |
| Generated-project model | Inherits the resolved route as its **default**, written into `.env.example` and the README, but stays env-overridable so the export is portable |
| Model picker | Dropdown of routes/models read from harness settings, plus a free-text field for catalog-only routes |
| Python deps | `uv` + `pyproject.toml`; drives the validation gate |
| RAG store | Local persistent ChromaDB seeded from a `docs/` folder — **deferred out of v1** during plan review; the template seam remains |
| Excluded | Auth, multi-user collaboration, non-PydanticAI frameworks, deployment of generated workflows |

**Plan-review outcome.** A dedicated review subagent critiqued the earlier draft and found six blockers, all verified by re-probing and folded in. Two corrected the probe record: `WebSearchTool` must be passed as `toolsets=`, not `builtin_tools=` (fact 9), and the original `Agent.override` validation idea could not work, replaced by the `deps_type` seam (facts 7, 16). Four were design gaps: the edge model could not express branching, fan-out, or joins (facts 12–14); `graph.run()` returns a bare value with no `End` to assert (fact 15); `'json'` is not a valid Python annotation (fact 17); and the harness sandbox root is the subprocess cwd, which `cwd` alone does not set (fact 19). The group order was inverted so the schema-defining codegen spike comes first. **Fact 19 no longer applies** to the chosen architecture — it was one of the reasons to drop the harness tie-in.

-----

<a id="why-not-the-harness"></a>
## Why not the harness

The original premise was "build on top of Factored Harness." Probing the actual boundary showed the tie-in bought little and cost a lot, so v1 keeps only the part with real value: **inheriting the user's configured model routes**.

**What the probes established.**

- **The harness is not needed for LLM access.** `BedrockConverseModel('us.anthropic.claude-sonnet-5')` under `AWS_PROFILE=factored-dev-profile` returned `DIRECT-OK` — PydanticAI resolves the same AWS SSO profile through boto3's own credential chain, reaching the identical models the harness reaches (fact 24).
- **The harness is not needed for the fill step.** A plain PydanticAI agent with three hand-written tools (`read_file`, `write_region`, `run_python`) was given the marker-region task and completed it: it wrote the body, then imported and executed the result to verify (fact 25). This is the whole of what Phase 3 needs.
- **What the harness genuinely provides has native equivalents.** `Agent(retries=...)` covers `llm-retry`; `UsageLimits` covers `token-meter` with more precision (`cost_limit`, `tool_calls_limit`, `request_limit`, token caps); a four-line `Path.resolve()` + `is_relative_to` guard covers path confinement, verified allowing `step.py` and blocking `../../etc/passwd` (fact 26).

**What dropping it removes.** Every one of these was a blocker or a documented risk in the earlier draft:

| Removed problem | Was |
|---|---|
| Same-version link constraint | `resolveDshBinFromManifests` throws unless `dsh` and the SDK client versions match exactly (fact 2) |
| Dependence on harness build state | A partial rebuild removing `apps/cli/lib/bin.js` silently changes launch mode (I6) |
| Two-cwd sandbox trap | `workspaceRoot: process.cwd()` is the *subprocess* cwd; passing only `cwd` left the model free to write across the whole app (fact 19) |
| Approval fails closed | The SDK protocol defines no approval method, so any approval-requiring operation deadlocks then denies (fact 19) |
| No wire-level cancel | Cancel could only mean killing the subprocess (I4) |
| TypeScript/Python split | Codegen targets Python while the driver was TypeScript, duplicating the model-route translation logic |
| ~1.2 s boot per compile | Measured harness boot + handshake before any model work |

**What it costs.** We now own the agent loop for the fill step: roughly 150–250 lines comprising a tool set, the confinement guard, a retry/limit policy, and structured logging. That is a real cost, accepted deliberately because it is small, directly testable, and replaces four failure modes with one owned code path. We also lose harness session logging of compiles; the compile job's own event log covers the product need.

**What is retained.** Model-route inheritance from `$DSH_HOME/settings.yaml` (facts 21–23) — the one thing the harness offers that PydanticAI cannot derive on its own, and the thing the user actually asked for. If the harness is absent or the file has no routes, Swarm Builder falls back to explicit env configuration and still runs.


-----

<a id="verified-facts-from-probes"></a>
## Verified facts from probes

Everything below was executed during planning, not assumed. These facts drive specific plan choices and must not be silently "corrected" to match older public documentation.

**Facts 1–5 and 19 are retained as evidence, not as design inputs.** They describe the harness SDK bridge, which v1 no longer uses (see [Why not the harness](#why-not-the-harness)). They are kept because they are what made the trade-off decidable, and because facts 3–4 still govern how model routes are *read* from settings. Anything marked **[superseded]** must not be implemented.

**1. [superseded] The SDK bridge works against this checkout.** Linking `packages/sdk/client` from a directory outside the pnpm workspace resolves and runs. A real turn returned `PONG` with 12 session events and 14 notifications.

**2. [superseded] The SDK client requires a same-version `@deepseek-ai/dsh`.** `resolveDshBinFromManifests` throws unless the `dsh` package version equals the SDK client version, and it prefers built `lib/bin.js`, falling back to a tsx source launch. Local checkout is `0.1.3-alpha.1`; npm publishes `0.1.3-alpha.2`, `0.1.5-*`, `0.1.6-alpha.1`, so mixing npm and local packages fails the version check. One of the reasons the tie-in was dropped.

**3. `provider`/`model` live in the `agent-default-model` settings section.** The section of `$DSH_HOME/settings.yaml` (`provider`, `model`, optional `reasoningEffort`) is owned by `AgentDefaultModelConfig`. **Still load-bearing**: this is the default Swarm Builder inherits. (The part about the `initialize` handshake requiring them is superseded.)

**4. Harness providers come from the user's settings document, not from a profile.** Booting `--profile sdk` with a private `DSH_HOME` failed with `no adapter registered for provider "amazon-bedrock"` until `settings.yaml` was copied in. The user's real routes (`amazon-bedrock`, `kornerstone`) live in `~/.dsh/settings.yaml` under the `llm-pi-ai` adapter. **Still load-bearing**: it establishes where route inheritance reads from, and that a different home inherits nothing.

**5. [superseded] The spawned agent writes files into the directory given as `cwd`.** A probe asked it to create `node_body.py` in `cwd` and the file appeared with exactly the requested content. Superseded by facts 25–26: the in-process agent writes through owned tools with an explicit confinement guard.


**6. `pydantic-graph` 2.43 uses `GraphBuilder`, not the documented `Graph(nodes=...)` constructor.** `Graph.__init__` now takes 11 required positional arguments and must not be called directly. The working, executed pattern is:

```python
builder = GraphBuilder(name="w", state_type=State, input_type=str, output_type=str)

@builder.step
async def research(ctx: StepContext[State, None, str]) -> str:
    ctx.state.topic = ctx.inputs          # NOTE: ctx.inputs, NOT ctx.input
    return f"notes about {ctx.inputs}"

builder.add_edge(builder.start_node, research)   # start_node/end_node are properties
builder.add_edge(research, write)
builder.add_edge(write, builder.end_node)
graph = builder.build()

await graph.run(inputs="topic", state=State())   # keyword-only; no positional start node
```

Confirmed signatures: `GraphBuilder.build(validate_graph_structure=True)`, `Graph.run(*, state, deps, inputs, span, infer_name)` — **keyword-only**, `Graph.render(*, title, direction)` (there is no `mermaid_code`), `StepContext.inputs`, and `builder.start_node` / `builder.end_node` as properties. This exact file ran and printed `NOTES ABOUT SWARM BUILDER`.

**7. Agents must not be constructed at import time.** `Agent('openai:gpt-4o')` raises `UserError` at **construction** time without `OPENAI_API_KEY` (under `pydantic-ai-slim` without the matching extra it raises `ImportError` instead — the exception type depends on installed extras, so never branch on it). Re-probed nuance: `Agent('openai:gpt-4o', defer_model_check=True)` **constructs fine with no key**, so deferring construction is a deliberate design choice rather than a hard requirement. The validation seam is fact 16, not `Agent.override`.

**8. ChromaDB's default embedder downloads an ONNX model to `~/.cache/chroma`** and failed under sandbox with `PermissionError`. Passing an explicit `EmbeddingFunction` avoids all network and cache access; a deterministic hash embedder made `PersistentClient` + `upsert` + `query` succeed offline. The RAG template therefore takes an explicit, injectable embedder and defaults to a real provider embedder only when a key exists. (RAG is deferred out of v1 — see [scope](#explicitly-out-of-scope-for-v1) — but the fact is retained for when it lands.)

**9. `WebSearchTool` is a first-class `pydantic_ai` export** (`from pydantic_ai import WebSearchTool`), with `search_context_size`, `allowed_domains`, `blocked_domains`, `max_uses`. **Corrected after plan review — the call site matters:** `Agent.__init__` in 2.43 has **no `builtin_tools` parameter**. Re-probed: `Agent('test', builtin_tools=[w])` → `TypeError`; `Agent('test', tools=[w])` → `AttributeError: 'WebSearchTool' object has no attribute '__name__'`; `Agent('test', toolsets=[w])` → **accepted**. The websearch template must pass `toolsets=[WebSearchTool(...)]`.

**10. `uv` needs a writable cache.** `uv sync` failed on `~/.cache/uv` under sandbox and succeeded with `UV_CACHE_DIR` set. The validation runner always sets an explicit cache dir.

**11. Toolchain present.** Node 26.7.0, pnpm 11.7.0, uv 0.11.18, Python 3.14.5 (via uv). The sibling folder `/Users/pedro.zaterka/factored-projects/swarm-builder` already exists.

**12. `build(validate_graph_structure=True)` is not a cycle or orphan backstop.** Re-probed: `add_edge(work, work)` (self-cycle) **builds and runs**, returning `done after 1`. A decorated-but-unedged step is **silently dropped** from both `graph.nodes` and `render()`, with no error. What `build()` does check: edges exist from start, some edge reaches end, no non-End dead ends, end reachable from start, all *referenced* nodes reachable. **Swarm Builder's own Phase-1 validator is therefore the only defense against cycles and orphans.**

**13. Plain multi-successor fan-out is silently lossy; real fan-in needs an explicit join.** Re-probed: `a → left`, `a → right`, both → `end_node` builds and runs both steps, but the graph output was one arbitrary branch (`R:x`, execution log `['a','right','left']`). Adding `jn = builder.join(reduce_list_append, initial_factory=list)` and edging both branches into it returned **both** values: `['R:x','L:x']`. `build()` also injects a synthetic `a_broadcast_fork` node, so `graph.nodes` keys are not exactly the canvas node set. Available reducers: `reduce_list_append`, `reduce_list_extend`, `reduce_dict_update`, `reduce_sum`, `reduce_null`.

**14. `Decision` is immutable and emission order is load-bearing.** `Decision.branch()` returns a **new** `Decision`. Re-probed: adding `add_edge(check, d)` against the pre-branch object and rebinding the branched result afterwards fails at build with `GraphValidationError: The following nodes have no outgoing edges: ['decision']`. A branchless decision that is edged in builds and then fails **at run time** with `RuntimeError: No branch matched inputs`. Branches come from `builder.match(Literal["big"]).to(big)` handed to `Decision.branch(...)`, and `emit_graph.py` must emit the complete `.branch(...)` chain **before** the `add_edge` targeting that decision.

**15. `graph.run()` returns the output value itself, not a result object.** Re-probed: the return was a bare `list` and `hasattr(out, 'output')` was `False`. `Graph`'s entire public surface is `get_parent_fork, is_final_join, iter, render, run, run_sync`. There is no `End` object to assert on — reaching End is implied by `run()` returning at all. Per-node evidence requires `graph.iter(...)`.

**16. The `deps_type` seam is how validation injects `TestModel`.** Re-probed and working end-to-end: `GraphBuilder(..., deps_type=Deps)` with `@dataclass Deps: model: Model | str`, steps typed `StepContext[State, Deps, str]` building agents from `ctx.deps.model`, then `graph.run(inputs=..., state=..., deps=Deps(model=TestModel()))` → `'success (no tool calls)'`. This replaces the original override idea, which could not work: `Agent.override` is a context manager on an existing instance, and agents constructed inside step bodies expose no instance the validator can reach.

**17. `'json'` is not a valid Python type hint.** Re-probed: `output_type=json` → `NameError: name 'json' is not defined`. `PortType` members cannot be interpolated verbatim into annotations; the emitter needs an explicit mapping table.

**18. Explicit `node_id=` is honored and `render()` is a stable golden oracle.** Re-probed: `@builder.step(node_id="answer")` produced `graph.nodes` keys `['__start__','answer','__end__']` and `render()` emitted deterministic `stateDiagram-v2` text. Without explicit ids, node ids derive from the Python function name, and a duplicate raises `GraphBuildingError: All nodes must have unique node IDs`.

**19. [superseded] The harness sandbox root is the subprocess cwd, which `cwd` alone does not set.** `sandbox-policy` is configured `workspaceRoot: process.cwd()`, while `resolveDshLaunch` sets the child process cwd only from `processCwd` and `DeepSeekHarness` sends `cwd ?? processCwd ?? process.cwd()` as the *wire* cwd — two different values. Passing only `cwd` left the sandbox rooted at the server's cwd, permitting writes across all of `swarm-builder/`. Also, `approval` defaults to `policy: 'ask'` while the SDK protocol defines no approval method, so an approval-requiring operation **fails closed** opaquely. Both problems disappear with the in-process agent, whose confinement is fact 26; this fact is retained as one of the reasons the tie-in was dropped.

**20. `hatchling` src-layout needs no extra config.** Re-probed: `uv sync` installed the project from a bare `src/<pkg>/` with no `[tool.hatch.build]` section, and a `validate/` script imported the installed package with no `sys.path` manipulation. Recorded so nobody adds needless build config.

**21. The SDK wire cannot enumerate models — inheritance must read settings.** `HarnessSdkRequestMap` defines exactly three client-to-server methods: `initialize`, `session/prompt`, `shutdown`. `LlmService.listModels(provider)` exists (`packages/llm/llm/src/index.ts`, implemented by the `llm-deepseek` and `llm-pi-ai` adapters) but is **in-process only** and is not exposed over the SDK protocol. Model inheritance is therefore resolved by Swarm Builder reading `$DSH_HOME/settings.yaml` — the `llm-pi-ai.providers` routes plus the `agent-default-model` section — and falling back to the `dsh-base` bundle default. A route that serves the installed pi-ai catalog without an explicit `models:` list enumerates nothing from settings alone, which is why the picker also accepts free text.

**22. The user's harness routes do translate to PydanticAI models.** Probed against the real `KnownModelName` union (112 `bedrock:` entries) and by constructing models:

| Harness route / model | PydanticAI form | Requirement |
|---|---|---|
| `amazon-bedrock` / `us.anthropic.claude-opus-5` | `bedrock:us.anthropic.claude-opus-5` (a known name) | `pydantic-ai-slim[bedrock]`, `boto3`, `AWS_PROFILE` |
| `deepseek-official` / `deepseek-v4-flash` | `deepseek:deepseek-v4-flash` (a known name) | `pydantic-ai-slim[openai]` + API key |
| `kornerstone` / `qwen38-27b-fp8` (custom `baseURL`) | **not** a known name; built structurally as `OpenAIChatModel('qwen38-27b-fp8', provider=OpenAIProvider(base_url=..., api_key=...))` — probed working | `pydantic-ai-slim[openai]` |

So the emitter needs both a plain-string path for known names and a structural path for custom `baseURL` routes; a bare model id is never emitted alone.

**23. Every harness wire protocol has a PydanticAI counterpart class.** The `api` values in `llm-pi-ai` map as probed: `openai-completions` → `OpenAIChatModel` (importable now), `openai-responses` → `OpenAIResponsesModel` (importable now), `anthropic-messages` → `AnthropicModel` (needs the `anthropic` extra), `bedrock-converse-stream` → `BedrockConverseModel` (needs the `bedrock` extra / `boto3`). The scaffolder therefore derives `pyproject.toml` extras from the inherited route's protocol, and a missing extra is a generation-time decision rather than a runtime `ImportError`.

**24. PydanticAI reaches the user's Bedrock models directly, with no harness.** Executed with `AWS_PROFILE=factored-dev-profile AWS_REGION=us-east-1`: `Agent(BedrockConverseModel('us.anthropic.claude-sonnet-5')).run_sync(...)` returned `'DIRECT-OK'`. boto3 resolves the AWS SSO profile through its own credential chain, including the cached `aws sso login` token, so the harness provides no access this path lacks. This is the finding that removed the harness from the critical path.

**25. A plain PydanticAI agent performs the code-fill job.** Executed end-to-end: an `Agent` with three `@agent.tool_plain` functions — `read_file`, `write_region` (marker-region replacement), and `run_python` (verification subprocess) — was told to fill a marker region and verify it. It wrote `async def run(x): return x.upper()` inside the markers, then imported and ran the module to confirm, leaving the markers intact. Phase 3 needs nothing more than this loop.

**26. Retries, spend limits, and path confinement all have native or trivial equivalents.** Probed: `Agent.__init__` accepts `retries`; `Agent.run` accepts `usage_limits`; `UsageLimits` exposes `cost_limit`, `request_limit`, `tool_calls_limit`, `input_tokens_limit`, `output_tokens_limit`, `total_tokens_limit`, and per-request caps — strictly more control than the harness `token-meter` gave us. Path confinement is a four-line guard using `Path.resolve()` + `is_relative_to`, verified allowing `step.py` and raising on `../../etc/passwd`. These three replace `llm-retry`, `token-meter`, and `sandbox-policy` for this application's needs.

-----

<a id="spike-amendments"></a>
## Spike amendments (established in Group 0)

The Group 0 vertical spike executed the full keyless gate against four hand-written oracle projects (`spike/linear`, `spike/linear_custom_baseurl`, `spike/branching`, `spike/fanout`), all passing `uv sync` → keyless import → `build()` → `render()` golden → node-set assertion → `TestModel` dry run. Evidence is in `spike/FINDINGS.md`. Facts 6, 7, 9, 10, 12–20, 22 and 23 reproduced exactly as written. The spike also established four things the facts above do **not** cover; these are binding on the implementation.

**27. Data does not pass through a `decision` node.** A branch target step receives, via `ctx.inputs`, the **match value returned by the decision's source step** — not the payload that flowed into that source step. Probed: `classify` returns `Literal["big","small"]` and the `big` step observed `ctx.inputs == "big"`, not the original string. Consequences:

- a branch target's declared `io.inputType` must equal the **decision source step's** `outputType`, not any node further upstream — Phase 1 enforces this as a hard error;
- a workflow that needs the pre-classification payload in a branch target must carry it in `State` (the spike does this with a state field), and the fill prompt must say so;
- `DecisionSpec` documents this in its docstring so the Inspector can surface it.

**28. `hatchling` hard-fails `uv sync` when the declared `readme` file is absent.** Missing `README.md` fails during the build step with `OSError: Readme file does not exist: README.md`, which reads like a dependency-resolution problem. `scaffold.py` must always emit `README.md` — it is a build prerequisite, not documentation politeness. (Adjacent to fact 20, not covered by it.)

**29. `builder.step(fn, node_id=...)` plain-call form is the emission target.** Fact 6 shows only the decorator form. The plain-call form is confirmed equivalent and is what the emitter must use, because it lets `graph.py` stay a thin wiring file that imports step functions from `steps/<slug>.py` — the Phase-2 layout. The decorator form would force step bodies into `graph.py`, which is generated and never model-written.

**30. The keyless import gate does not prove the declared extra is sufficient.** `Agent("bedrock:...", defer_model_check=True)` constructs with **zero** extras installed; the `[bedrock]` extra is needed only for a real `.run()`. So Phase 5's import step would pass even if the route's extra were omitted entirely. The extra is still emitted per fact 23 (a live run needs it), but nothing in the keyless gate tests extras sufficiency — `validate.py` therefore adds a **static assertion** that the emitted `pyproject.toml` declares the extra the inherited route's `api` protocol requires, since no runtime check can cover it keylessly.

**31. Golden diagrams are generated, never hand-authored.** Phase 2 emits `validate/golden_render.txt` by invoking `graph.render()` once at scaffold time and writing the bytes verbatim. `render()` emits **no trailing newline** — byte-exact comparison matters. A `decision`'s `note right of ... end note` block appears only when `builder.decision(note=...)` is given a note.

**32. The synthetic fork id is predictable, so the expected node set is computed, not discovered.** `build()` names the injected node `<source_node_id>_broadcast_fork`. Phase 1 and Phase 5 both compute the expected `graph.nodes` key set purely from the canvas document (node slugs + `__start__`/`__end__` + one predicted `<fanout_source>_broadcast_fork` per fan-out source), then assert equality — never introspecting whatever `build()` happened to produce.

Also settled: `ReducerId`'s four literals map to `reduce_list_append`, `reduce_list_extend`, `reduce_dict_update`, `reduce_sum`. `reduce_null` exists upstream but has no `ReducerId` counterpart, so the emitter's lookup table has exactly four entries.

-----

<a id="coding-conventions"></a>
## Coding conventions (`.claude/rules/`)

The developer added `.claude/rules/{python-conventions,typescript-react-conventions,mermaid-diagrams}.md` mid-implementation. They are binding, with these **explicitly arbitrated** exceptions, decided by the developer rather than inferred.

**Python (`python-conventions.md`) — retrofitted across all groups, including 1–3.** Full type annotations, Google-style docstrings (`Args`/`Returns`/`Raises`, imperative one-line summary, never restating annotations), grouped/sorted imports, raise-don't-return-`None` error signaling, `snake_case`/`PascalCase`/`UPPER_SNAKE_CASE` naming, why-not-what comments, no commented-out code, named constants over magic literals.

Two carve-outs:

- **Lazy imports are permitted as a documented seam.** The rules forbid imports inside function bodies. Where a route lazily imports another subsystem (the template registry, `review()`, the compile pipeline) the purpose is graceful degradation: a missing or broken subsystem must fail *that endpoint*, not server startup. Every such import carries a comment saying so. Everywhere else, imports are module-level.
- **`Any` is permitted only in the `PortType` mapping**, where `json` → `dict[str, Any]` is the annotation the generated code must contain (fact 17). It is not a typing shortcut and appears nowhere else.

**TypeScript/React (`typescript-react-conventions.md`) — the stack sections do NOT apply.** The rules describe a different application (Bloomberg/terminal aesthetic, Tailwind v4 + shadcn/ui, TanStack Query, wouter, kebab-case files, `@/` aliases). PLAN.md's approved frontend is a CSS-only **hand-drawn/excalidraw** canvas whose single source of truth is one **zustand** store with React Flow as a projection of it. The developer chose to **keep the approved stack**, so these are deliberately not adopted: Tailwind/shadcn, TanStack Query, wouter, the terminal aesthetic, kebab-case filenames, and `@/` alias imports.

What **is** adopted from that file, because it is stack-independent: `strict: true` + `noUncheckedIndexedAccess: true`; no `any` (`unknown` + narrowing); `interface` for props, `type` for unions; `satisfies`/`as const`; named function exports and no `React.FC`; the hooks → derived → handlers → early-returns → JSX ordering; `handle*` event-handler naming; `UPPER_SNAKE_CASE` module constants; React 19 patterns (ref-as-prop, no speculative memoization); explicit loading/error/empty states; accessibility (semantic elements, keyboard reachability, `aria-hidden` on decorative icons, `aria-live` on the streaming compile log); and the security rules (no `dangerouslySetInnerHTML`, validate URLs, `VITE_`-prefixed env vars only).

**One rule supersedes the plan:** the SSE client uses `fetch` + `ReadableStream`, **not** `EventSource`. `EventSource` cannot send the `Last-Event-ID` request header the reconnect design in "Compile job lifecycle (I4)" depends on, so this is a correctness fix, not a style preference.

**Mermaid (`mermaid-diagrams.md`)** applies to Markdown this project emits, including generated `README.md` files: quote labels containing `<br/>`, slashes, colons or punctuation, and never use `{}` or literal `\n` inside labels.

-----

<a id="repository-layout"></a>
## Repository layout

A standalone project outside the harness repo and outside its pnpm workspace, so no harness gate (100% coverage, doc-sync, i18n, verify-*) applies. The server is Python (same language as the compile agent and the generated code); only the frontend is a Node build.

```
swarm-builder/                (= /Users/pedro.zaterka/factored-projects/swarm-builder)
  PLAN.md                     this plan
  README.md                   quickstart, architecture sketch, troubleshooting
  pyproject.toml              server + agent deps (uv); ruff + pytest config
  .python-version             pins the server interpreter
  .gitignore                  node_modules, dist, workspace/, .venv, .uv-cache
  .env.example                DSH_HOME, SWARM_WORKSPACE, PORT, UV_CACHE_DIR, SWARM_MODEL
  src/swarm_builder/
    __init__.py
    main.py                   FastAPI app, static web serving, printed URL
    config.py                 env resolution + explicit defaults
    models.py                 pydantic graph-document schema (the one source of truth)
    routes/
      graphs.py               CRUD over JSON graph files
      templates.py            template catalog
      llm_routes.py           GET /api/models: inherited routes + resolved default
      compile.py              POST /api/compile, SSE events, DELETE cancel
      export.py               GET /api/graphs/{id}/export -> path + run command
      health.py               GET /api/health
    inherit/
      settings.py             read $DSH_HOME/settings.yaml: routes, models, default
      routes.py               harness route -> PydanticAI model (facts 22-23)
    compile/
      pipeline.py             orchestrates: review -> scaffold -> fill -> check -> validate
      review.py               Phase 1 validator (cycles, orphans, joins, state ownership)
      scaffold.py             deterministic file emission from templates
      emit_graph.py           canvas graph -> GraphBuilder wiring code
      agent.py                the fill agent: tools, retries, UsageLimits (facts 25-26)
      confine.py              path guard: resolve() + is_relative_to (fact 26)
      boundary.py             two-tier hash + marker enforcement
      validate.py             uv sync + import + build + render golden + TestModel run
      jobs.py                 compile registry: cancel, eviction, Last-Event-ID replay
    store/
      graphs.py               read/write workspace/graphs/*.json (atomic)
      projects.py             workspace/projects/<graphId>/ lifecycle
    templates/
      registry.py             catalog + keyword inference rules
      chat/ orchestrator/ websearch/       per-template file sets (rag deferred)
  tests/                      pytest: unit, codegen fixtures, pipeline, boundary
  web/
    package.json
    vite.config.ts
    index.html
    src/
      main.tsx
      App.tsx
      types.ts               graph types generated from the server's OpenAPI schema
      canvas/
        Canvas.tsx            React Flow surface, hand-drawn theme
        nodes/AgentNode.tsx  programmatic vs agent visuals
        nodes/ProgrammaticNode.tsx
        nodes/DecisionNode.tsx  nodes/JoinNode.tsx
        edges/               styled edges per edge kind
        theme.css            excalidraw-ish styling tokens
      panels/
        Inspector.tsx        per-node intent, template, I/O, state fields, delegation
        Palette.tsx          drag sources
        CompilePanel.tsx     review -> model picker -> compile -> log -> result
      state/
        graphStore.ts        zustand store, dirty tracking, autosave
      api/client.ts          typed fetch + SSE subscription
      infer.ts               pure keyword template inference (client-side)
  workspace/                 runtime data (gitignored)
    graphs/<graphId>.json
    projects/<graphId>/      generated PydanticAI project
```

**One schema, two consumers.** The graph document is defined once as pydantic models in `models.py`; the frontend's TypeScript types are generated from the FastAPI OpenAPI schema rather than hand-maintained. This removes the dual-definition drift the earlier TS/Python split would have required.

-----

<a id="architecture-and-data-flow"></a>
## Architecture and data flow

```
Browser (React + React Flow)
   │  JSON over HTTP + SSE
   ▼
FastAPI server (Python, uvicorn, 127.0.0.1)
   ├── store/       graph JSON + project dirs on disk
   ├── inherit/     read $DSH_HOME/settings.yaml -> routes + default (facts 21-23)
   ├── templates/   deterministic skeletons (no LLM)
   └── compile/
        ├── scaffold + emit_graph   deterministic; owns all graph wiring
        ├── agent.py                PydanticAI agent, in-process (facts 24-25)
        │      tools: read_file / write_region / parse_check
        │      guarded by confine.py; bounded by retries + UsageLimits (fact 26)
        │      writes node bodies into
        │      ▼
        │   workspace/projects/<graphId>/
        └── validate.py  ── uv run ──▶ keyless validation gate
```

No subprocess boundary, no JSON-RPC, and no `dsh` dependency: the compile agent runs in the server process and calls the model provider directly. The only harness contact is reading its settings file.

Compilation is deliberately **template-first**: the server emits every structural file itself and asks the model only for the marked body regions. This keeps token use low, makes failures local, and means a model error can never break graph wiring.

-----

<a id="the-graph-document-schema"></a>
## The graph document (schema)

`src/swarm_builder/models.py`, pydantic models validated on every read and write, and the source the frontend's types are generated from. Version the document from day one so later changes migrate rather than break.

The edge model is a **discriminated union**, not a bare `{source,target}` pair. Facts 12–14 make this mandatory: `add_edge` alone cannot express branching (needs a match expression on a `Decision`), cannot express fan-in (needs a `Join` with a reducer), and turns two successors into a silently lossy fan-out.

```python
NodeKind = Literal['agent', 'programmatic', 'decision', 'join']
TemplateId = Literal['chat', 'orchestrator', 'websearch']   # 'rag' deferred; registry keeps the seam
PortType = Literal['str', 'json', 'list[str]']
ReducerId = Literal['list_append', 'list_extend', 'dict_update', 'sum']

class SwarmNode(BaseModel):
    id: str                         # emitted verbatim as node_id (fact 18)
    kind: NodeKind
    title: str                      # slugified for the Python function name
    intent: str                     # plain English: what this step must do
    position: Position
    template: TemplateId | None = None      # agent nodes; inferred when absent
    io: NodeIo                              # inputType / outputType
    reads: list[str] = []                   # State fields this step may read   (I2)
    writes: list[str] = []                  # State fields this step may write  (I2)
    agent: AgentSpec | None = None          # instructions, tools, delegatesTo, outputSchema
    programmatic: ProgrammaticSpec | None = None   # needs, signatureHint
    decision: DecisionSpec | None = None    # branches: match -> targetNodeId (fact 14)
    join: JoinSpec | None = None            # reducer + initial (fact 13)

class SeqEdge(BaseModel):      kind: Literal['seq'];      id: str; source: str; target: str; label: str | None = None
class BranchEdge(BaseModel):   kind: Literal['branch'];   id: str; source: str; target: str; match: str
class FanoutEdge(BaseModel):   kind: Literal['fanout'];   id: str; source: str; target: str; join_node_id: str
class JoinEdge(BaseModel):     kind: Literal['join'];     id: str; source: str; target: str
class DelegateEdge(BaseModel): kind: Literal['delegate']; id: str; source: str; target: str  # never an add_edge (I1)

SwarmEdge = Annotated[SeqEdge | BranchEdge | FanoutEdge | JoinEdge | DelegateEdge, Field(discriminator='kind')]

class SwarmGraph(BaseModel):
    version: Literal[1] = 1
    id: str
    name: str
    entry_node_id: str
    exit_node_id: str
    state_fields: list[StateField]          # becomes the @dataclass State
    nodes: list[SwarmNode]
    edges: list[SwarmEdge]
    model: ModelSelection | None = None     # absent = inherit the resolved default
    updated_at: datetime

class ModelSelection(BaseModel):
    """One inherited route, resolved to something PydanticAI can construct."""
    provider: str                  # route key, e.g. 'amazon-bedrock'
    model: str                     # provider-owned id, e.g. 'us.anthropic.claude-opus-5'
    reasoning_effort: str | None = None
```

**Model inheritance (facts 21–24).** `inherit/settings.py` resolves configured models by reading `$DSH_HOME/settings.yaml`: the `llm-pi-ai.providers` map supplies routes with their `api`, `baseURL`, `apiKeyEnv`/`awsProfile`, and any explicit `models:` list, while `agent-default-model` supplies the default selection. Settings are hot-reloaded and user-editable, so this is read **per compile**, never cached at boot. When the file, the adapter section, or the default is absent — including when the harness is not installed at all — Swarm Builder falls back to explicit env configuration (`SWARM_MODEL`, optional `SWARM_BASE_URL`) and reports the source. The app never requires the harness to run.

The resolved selection is used in two distinct places, and conflating them is the mistake to avoid:

1. **The compile agent** — `inherit/routes.py` turns the selection into a PydanticAI model object, which `compile/agent.py` uses directly in-process (fact 24). A graph's `model` overrides the default; absent, the default applies.
2. **The generated project's runtime model** — emitted as the **default** in `.env.example` and the README, read at run time from `SWARM_MODEL` (plus `SWARM_BASE_URL`/`SWARM_API_KEY_ENV` for a custom route). The project stays portable: changing the env var runs it anywhere.


**Harness route → PydanticAI emission.** Two paths, per fact 22:

| Route kind | Emitted `SWARM_MODEL` default | Emitted construction |
|---|---|---|
| Known-name route (`bedrock:`, `deepseek:`, `anthropic:`, …) | the prefixed known name | plain string handed to `Agent` |
| Custom `baseURL` route | the bare model id | `OpenAIChatModel(id, provider=OpenAIProvider(base_url=..., api_key=...))` |

`pyproject.toml` extras derive from the route's `api` protocol (fact 23): `openai-completions`/`openai-responses` → `[openai]`, `anthropic-messages` → `[anthropic]`, `bedrock-converse-stream` → `[bedrock]`. A route whose protocol has no counterpart is refused at Phase 1 with the route named, rather than emitting a project that cannot import.

**Port type → Python annotation.** `PortType` members are never interpolated into annotations verbatim (fact 17). The emitter uses exactly this table, and adds the matching import:

| `PortType` | Python annotation | Import |
|---|---|---|
| `str` | `str` | — |
| `list[str]` | `list[str]` | — |
| `json` | `dict[str, Any]` | `from typing import Any` |

**State ownership (I2).** `reads`/`writes` name `stateFields` entries. Phase 1 rejects a write to an undeclared field, two nodes writing the same field, and a read of a field no upstream node writes. The fill prompt quotes each node's `reads`/`writes` so generated bodies cannot touch unowned state. This matters because pydantic-graph performs **no runtime type or state checking at edges** — a step returning `list[str]` into a step annotated `str` builds and runs, delivering the list. Every I/O guarantee here is Swarm Builder's own, enforced in Phase 1.

**Delegation vs. sequence (I1).** An orchestrator's children are called **as agent tools**, not as graph steps. A `delegate` edge is drawn on the canvas and rendered in the Inspector, but `emit_graph.py` deliberately emits **no `add_edge`** for it — otherwise the child would execute twice, once as a step and once as a tool. `agent.delegatesTo` is the authoritative list the orchestrator template reads when generating tool wrappers. Phase 1 rejects a node that is both a `delegate` target and a `seq`/`branch` target.

**v1 control-flow support.** `seq`, `branch` (via `decision` nodes), and `fanout` + `join` are supported. **Cycles are rejected** in Phase 1 as a hard error — fact 12 proves `build()` will not catch them, so allowing one would ship a graph that can loop unbounded at run time.

-----

<a id="http-api"></a>
## HTTP API

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/api/health` | server version, resolved `DSH_HOME`, resolved default provider/model, `uv` availability, `dsh` launch mode (fact 2 / I6) |
| `GET` | `/api/graphs` | list saved graph summaries |
| `GET` | `/api/graphs/:id` | full graph document |
| `PUT` | `/api/graphs/:id` | validate + atomically write graph JSON |
| `DELETE` | `/api/graphs/:id` | remove graph (keeps generated project unless `?project=1`) |
| `GET` | `/api/templates` | template catalog: id, label, description, default tools, required env |
| `GET` | `/api/models` | harness routes and models read from settings, the resolved default, and each route's source (facts 21–23) |
| `POST` | `/api/graphs/:id/review` | static validation (cycles, orphans, state ownership, type mismatches) — the "initial review" before compile |
| `POST` | `/api/compile` | `{ graphId }` → `{ compileId }`; starts one compile job |
| `GET` | `/api/compile/:compileId/events` | SSE: `phase`, `log`, `warning`, `done`, `error` |
| `GET` | `/api/compile/:compileId` | status snapshot for reconnects |
| `DELETE` | `/api/compile/:compileId` | cancel a running compile (I4) |
| `GET` | `/api/graphs/:id/export` | reveal the generated project path + the `uv run` command |

Template inference is a **pure function in `shared/`**, called client-side — it is deterministic keyword scoring with no model call, so an HTTP endpoint would buy nothing.

**Compile job lifecycle (I4).** Jobs live in an in-memory `Map` keyed by `compileId`:

- **Cancel** is `DELETE /api/compile/:compileId` → `harness.close()`. This is the only mechanism available: the SDK protocol has **no wire-level prompt cancel**, and a timed-out request keeps running server-side until the runtime is closed.
- **Fill timeout.** `requestTimeoutMs` defaults to `undefined` (wait forever), so the fill run sets it explicitly (10 minutes) — otherwise a wedged model hangs the compile permanently with the UI streaming nothing.
- **Reconnect** is `Last-Event-ID`-based: every SSE frame carries a monotonic id, and a reconnecting client replays strictly *after* its last seen id from the retained ring buffer (last 2000 lines). `GET /api/compile/:compileId` is for a client that has no `Last-Event-ID` at all, e.g. a fresh tab. This avoids both the duplicate-log and lost-middle failures of replaying from zero.
- **Eviction.** The map holds at most 20 finished jobs, dropped oldest-first; a running job is never evicted.
- One concurrent compile per `graphId`; a second request gets `409`.

-----

<a id="templates"></a>
## Templates

Each template is a directory of literal files plus a manifest declaring which regions the model fills. Templates are ordinary code the maintainer can read and run — not prompt text.

| Template | Purpose | Key generated content |
|---|---|---|
| `chat` | single conversational agent | agent factory taking the model from `ctx.deps`, instructions from `intent`, optional structured output |
| `orchestrator` | delegates to child agents | parent agent whose **tool functions** wrap the agents named in `agent.delegatesTo`; children are not graph steps (I1) |
| `websearch` | research step | `Agent(..., toolsets=[WebSearchTool(...)])` — per corrected fact 9, **not** `builtin_tools=` |

`rag` is deferred out of v1 (see [scope](#explicitly-out-of-scope-for-v1)); the registry keeps the seam so it can land without reshaping the pipeline.

**Template inference.** Scores the node's `intent` against per-template keyword sets (`search|browse|news|latest` → websearch; `delegate|coordinate|route|sub-agent|plan and assign` → orchestrator; else chat). The result is a **suggestion**: the Inspector preselects it and shows the matched keywords, and the user can override.

**Shared agent contract.** Every agent template emits this shape, which facts 7 and 16 together require — the model arrives through graph deps so validation can inject `TestModel()`:

```python
def build_agent(model: Model | str) -> Agent[None, OutT]:
    return Agent(model, instructions=..., toolsets=[...])

# in the step body:
async def step(ctx: StepContext[State, Deps, str]) -> str:
    agent = build_agent(ctx.deps.model)
    result = await agent.run(ctx.inputs)
    return result.output
```

No module constructs an `Agent` at import time, so import succeeds with no keys, and `graph.run(..., deps=Deps(model=TestModel()))` validates the whole workflow offline. The real default model is resolved in `main.py`/`Deps` construction from `SWARM_MODEL`, never hardcoded to a provider string the declared extras may not install.


-----

<a id="the-compile-pipeline"></a>
## The compile pipeline

Five phases, each emitting SSE progress. Phases 1, 2, 4 and 5 are fully deterministic; only phase 3 calls the model.

**Phase 1 — Review (deterministic).** This phase is the **only** defense against several classes of defect, because fact 12 proves `build()` catches none of them. Hard errors that refuse the compile:

- unknown node ids on edges; no path from `entryNodeId` to `exitNodeId`; nodes unreachable from entry (fact 12: an unedged step is silently dropped);
- **any cycle** — v1 rejects cycles outright (fact 12: a self-cycle builds *and runs*);
- a multi-successor node whose successors are not `fanout` edges converging on a declared `join` node (fact 13: plain fan-out silently discards branches);
- a `decision` node with zero branches (fact 14: it builds, then fails at run time), or a branch whose `targetNodeId` is not also a `branch` edge target;
- port-type mismatch across an edge; missing `intent`;
- state-ownership violations (I2): a `writes` entry not in `stateFields`, two nodes writing one field, or a `reads` field no upstream node writes;
- a node that is both a `delegate` target and a `seq`/`branch` target (I1).

Soft findings become warnings. This validator, not the framework, is the correctness gate.

**Phase 2 — Scaffold (deterministic).** Emit the complete project from templates:

```
workspace/projects/<graphId>/
  pyproject.toml            deps = union(route extras per fact 23, template deps, programmatic `needs`)
  .python-version           pins the interpreter the codegen suite validates against
  README.md                 how to run, required env vars, the inherited route
  .env.example              SWARM_MODEL default from the inherited route (+ BASE_URL/API_KEY_ENV)
  src/swarm_workflow/
    __init__.py
    state.py                @dataclass State from stateFields
    deps.py                 @dataclass Deps carrying the model (fact 16) + env resolution
    graph.py                GraphBuilder wiring — generated, never model-written
    steps/<node_slug>.py    one module per node: imports region + body region
    agents/<node_slug>.py   agent factories for agent nodes
  validate/dry_run.py       TestModel-injected gate
```

`graph.py` is emitted by `emit_graph.py` strictly from the verified API (facts 6, 13, 14, 18): `GraphBuilder(..., deps_type=Deps)`, `@builder.step(node_id=<slug>)` with **explicit** node ids, `ctx.inputs`, `builder.add_edge`, `builder.join(<reducer>, initial_factory=...)` for every fan-in, the full `.branch(...)` chain emitted **before** the `add_edge` targeting a decision, and `builder.build()`. `delegate` edges emit no `add_edge`. Because wiring is deterministic, a model mistake cannot corrupt graph structure.

**Phase 3 — Fill (in-process PydanticAI agent).** One `Agent` run inside the server, per facts 24–26. No subprocess, no JSON-RPC, no `dsh`.

The agent is constructed from the resolved route and given exactly three tools, each confined by `confine.py`:

| Tool | Contract |
|---|---|
| `read_file(name)` | read a scaffolded file; rejects any path resolving outside the project directory |
| `write_region(name, node_id, region, body)` | replace one `imports` or `body` marker region; refuses unknown files, unknown markers, and any write that would disturb text outside the region |
| `parse_check(names?)` | parse the filled step modules with `ast.parse` **in-process** and report any syntax error as data; executes nothing |

Confinement is enforced **in the tool**, not in the prompt: every path goes through `Path.resolve()` + `is_relative_to(project_root)` (fact 26), so a traversal attempt raises before any I/O. `write_region` is the only mutator, which is what makes the Phase-4 boundary check cheap — the agent has no general-purpose file write, no shell, and no network.

Bounds come from PydanticAI directly (fact 26): `Agent(retries=2)` for tool-level recovery, and `UsageLimits(request_limit=..., tool_calls_limit=..., cost_limit=...)` per run so a pathological graph cannot spend unboundedly. A run is cancellable by cancelling its asyncio task — no process kill required, unlike the SDK path (I4).

The instructions are assembled from the graph and state the boundaries explicitly:

- the editable regions — `# --- swarm:imports <nodeId> ---` and `# --- swarm:begin/end <nodeId> ---` blocks inside `steps/*.py` and `agents/*.py` (the imports region exists because a body needing `import httpx` has nowhere else to put it — I3);
- the files it cannot touch: `graph.py`, `state.py`, `deps.py`, `pyproject.toml`, `validate/` — enforced by the tool, restated for clarity;
- per node: `title`, `intent`, `io` types mapped to Python annotations, tools, `reads`/`writes` field ownership, `delegatesTo`, and for programmatic nodes the `signatureHint` and `needs`;
- the pinned library API quoted from facts 6, 9, and 16 — `ctx.inputs` not `ctx.input`, `toolsets=[WebSearchTool()]` not `builtin_tools=`, models from `ctx.deps.model` — because training data contains the older idioms;
- the rule that no `Agent` is constructed at import time and no key is ever hardcoded.

The model comes from the graph's own `model` when set, else the resolved inherited default (facts 21–24, I5). Agent tool calls and messages stream to the UI as coarse phases.

> **Amendment (implementation).** The plan specified `run_check(code)` as "run Python in the project venv to self-verify". That is not implementable as written — Phase 5's `uv sync` runs *after* the fill, so no venv exists yet, and letting the tool sync writes `uv.lock`, which then trips Phase 4. More importantly, running arbitrary model-authored code **defeats assumption 9**: it was verified writing a file outside the project directory, where Phase 4 cannot see it. Since the only verification the prompt ever asked for was `ast.parse` over the modules just written, the tool was narrowed to `parse_check`, which does exactly that **in-process**. The agent therefore spawns no subprocess and executes no model-authored code, so "the agent's filesystem reach is the project directory" is now true by construction rather than by prompt instruction. Phase 5 remains what executes the built graph.

**Phase 4 — Boundary check (deterministic).** Two-tier, because Phase 3 legitimately rewrites the permitted files so a whole-file hash cannot cover them (I3). This remains a real gate even though `write_region` is the only mutator, because it catches a template or emitter bug as well as a tool bug:

- **forbidden files** — whole-file SHA-256 compared against hashes taken at the end of Phase 2; any change fails the compile naming the path;
- **permitted files** — parsed into regions; every `imports`/`begin`/`end` marker must still exist, body regions must be non-empty, and the **outside-marker** regions are hashed and compared, so the model cannot edit around its own sandbox;
- any file created outside the scaffolded set is reported and fails the compile.

**Phase 5 — Validate (deterministic).** With `UV_CACHE_DIR` set explicitly (fact 10):

1. `uv sync` in the project directory;
2. `uv run python -c "import swarm_workflow.graph"` — proves import with no keys (fact 7);
3. `uv run python validate/dry_run.py`, which asserts exactly (facts 15, 16, 18):
   - `graph.build()` succeeds;
   - `graph.render()` output equals the golden diagram emitted in Phase 2 (the emitter writes the expected diagram, so drift is caught);
   - the set of `graph.nodes` keys equals the canvas node slugs plus `__start__`/`__end__` plus any synthetic `*_broadcast_fork` (fact 13) — this catches the silently dropped orphan of fact 12;
   - `await graph.run(inputs=<sample>, state=State(), deps=Deps(model=TestModel()))` returns **without raising**, and the returned value matches the declared `output_type`. There is no `End` object to assert on and no `.output` attribute (fact 15);
   - for any graph with a `join`, the returned value is the **joined collection**, not a single branch value (fact 13).

A non-zero exit sends the captured stderr tail to the UI and marks the compile `failed` while leaving the project on disk for inspection. On success the UI shows the rendered diagram and the run command.

**Retry policy.** Phase 3 is retried at most once, and only when phase 4 or 5 fails, with the failure output appended to the prompt. A second failure is reported, not retried again.

**`SWARM_FAKE_FILL=1`.** Replaces Phase 3 with deterministic stub bodies, so phases 1, 2, 4, 5 and the whole SSE/cancel/409/reconnect lifecycle are testable with no model and no `$DSH_HOME`. This is the same stub machinery the codegen suite needs, so it costs nearly nothing.


-----

<a id="codegen-contract"></a>
## Codegen contract

Rules the implementation must hold, each traceable to a probe. These are the assertions the codegen suite enforces.

1. `graph.py` uses only the verified API: `GraphBuilder`, `@builder.step`, `builder.add_edge`, `builder.join`, `builder.decision`/`match`, `builder.start_node`/`end_node` properties, `builder.build()`, `graph.run(...)` keyword-only, `graph.render()`.
2. `ctx.inputs` — never `ctx.input` (fact 6).
3. Agents are built from `ctx.deps.model`; no module constructs an `Agent` at import time (facts 7, 16).
4. `Graph(...)` is never called directly (fact 6).
5. `WebSearchTool` is passed as `toolsets=[...]` — never `builtin_tools=` or `tools=` (fact 9).
6. Every `@builder.step` carries an explicit `node_id=<slug>`, so the slug mapping is authoritative and `render()` goldens stay stable (fact 18).
7. Every fan-in goes through an explicit `builder.join(<reducer>, initial_factory=...)`; two `add_edge` calls are never used as a fan-out (fact 13).
8. A `decision`'s complete `.branch(...)` chain is emitted **before** the `add_edge` that targets it (fact 14).
9. `PortType` is translated through the annotation table, never interpolated verbatim (fact 17).
10. `graph.run()`'s return value is used directly; no `.output` access, no `End` assertion (fact 15).
11. Node slugs are derived from `title`, deduplicated, and validated against a Python-identifier regex plus a Python-keyword blocklist.
12. Every generated Python file starts with `from __future__ import annotations`.
13. Generated projects carry a `.python-version` pinning the interpreter the codegen suite validates against, so `uv run` cannot silently pick a different one.
14. No `[tool.hatch.build]` configuration is emitted — a bare `src/<pkg>/` layout installs correctly as-is (fact 20).
15. When RAG lands, Chroma always receives an explicit embedding function; no default-embedder path (fact 8).

-----

<a id="frontend"></a>
## Frontend

**Canvas.** React Flow with custom node types for the four node kinds (`agent`, `programmatic`, `decision`, `join`), each visually distinct with a badge. Edge kinds are visually distinct too: `seq` solid, `branch` labeled with its match expression, `fanout`/`join` drawn toward the join node, and `delegate` dashed to signal "called as a tool, not a step" (I1). The hand-drawn feel comes from CSS only — slightly rotated borders, sketch-like radii, a handwriting-ish display font, and a dotted background — so graph semantics stay typed and validated rather than free-form.

**Inspector.** For the selected node: title, plain-English `intent` (the primary field), template selector showing the inferred suggestion and its matched keywords, input/output port types, `reads`/`writes` state-field pickers (I2), tool toggles, `delegatesTo` picker for orchestrator nodes, and for programmatic nodes the `needs` list and `signatureHint`. Decision nodes edit their branch match expressions; join nodes pick a reducer. Editing marks the graph dirty; autosave debounces a `PUT` at 800 ms.

**Compile panel.** Shows the review findings first — errors block, warnings do not — then a **model picker** defaulting to the inherited harness selection (dropdown of routes/models from `/api/models`, plus a free-text field for catalog-only routes that settings cannot enumerate — fact 21), a **Compile** button, a streamed phase list with a live log tail and a **Cancel** button, and finally the result: rendered diagram, project path, and the `uv run` command to copy. The picker shows which route is inherited and whether it came from settings or the bundle default, so a compile never silently spends credentials on an unexpected route.

**State.** One zustand store holds the graph, selection, dirty flag, and compile status. The store is the single source of truth; React Flow is a projection of it, so save and export never read view-only state.

-----

<a id="grouped-changes-by-subsystem"></a>
## Grouped changes by subsystem

Reordered after review: the riskiest, most schema-defining work comes **first**, because discovering the control-flow model late would simultaneously invalidate the schema, the emitter, the validator, every fixture, and the Inspector UI (I8).

**Group 0 — Vertical codegen spike (do this first).** Hand-write three target `graph.py` files — one linear, one branching via `decision`, one `fanout`+`join` — and make each pass `uv sync` → keyless import → `build()` → `render()` golden → `TestModel` dry run via `deps`. Write one of them twice, once with a known-name model string and once with the structural custom-`baseURL` form (fact 22), so both emission paths are proven before the emitter exists. Then derive `models.py` and `emit_graph.py` **from** those known-good files. This converts the control-flow, injection-seam, assertion, annotation, and model-route blockers from rework into design input, at roughly a day's cost.

**Group 1 — Project skeleton.** `pyproject.toml` with the server and agent deps, `.python-version`, `.gitignore`, `.env.example`, `README.md`, ruff/pytest config, and `models.py` as derived in Group 0.

**Group 2 — Deterministic codegen.** Template registry, the three template file sets, `emit_graph.py`, `scaffold.py`, and `review.py` with its cycle/orphan/fan-out/state-ownership checks. Verifiable with no model and no network.

**Group 3 — Server core.** `main.py`, `config.py`, graph store with atomic writes, graph CRUD routes, `/api/health`, `/api/models` with `inherit/` (settings reading only), static serving, and OpenAPI-generated frontend types.

**Group 4 — Compile pipeline minus the model.** `pipeline.py` phases, `confine.py`, `boundary.py`, `validate.py`, `jobs.py` with cancel/eviction/`Last-Event-ID` replay, and `SWARM_FAKE_FILL=1`. The entire pipeline becomes testable here with no model calls.

**Group 5 — The fill agent.** `compile/agent.py`: the three confined tools, instruction assembly from the graph, `retries` + `UsageLimits` bounds, task-based cancellation, and progress streaming. This swaps the fake fill for the real one. Small and self-contained precisely because Groups 2–4 own everything deterministic.

**Group 6 — Frontend.** Vite app, canvas, the four node types and five edge kinds, theme, Inspector, Palette, CompilePanel with the model picker, API client, zustand store.

**Group 7 — Tests and docs.** Remaining suites, README quickstart, troubleshooting entries for every failure mode.



-----

<a id="dependency-versions"></a>
## Dependency versions

Latest versions, resolved from the registries during planning.

**Python / server + compile agent:** `fastapi` 0.141.1, `uvicorn` 0.53.0, `sse-starlette` 3.4.11 (SSE), `pyyaml` 6.0.3 (reading `settings.yaml`), `pydantic-ai-slim` 2.43.0 with the extras the inherited route needs, `pytest` 9.1.1, `ruff` 0.16.7. No harness package is a dependency.

**Node / web:** `react` 19.3.0, `react-dom` 19.3.0, `@xyflow/react` 12.11.6, `zustand` 5.0.15, `vite` 8.3.0, `@vitejs/plugin-react` 6.1.1.

**Python (generated projects):** `pydantic-ai-slim` 2.43.0 with the extras derived from the **inherited route's** `api` protocol (fact 23: `[openai]`, `[anthropic]`, or `[bedrock]`) unioned with any template needs, `pydantic-graph` 2.43.0, `hatchling` build backend, `requires-python = ">=3.11"`, plus a `.python-version` pinning the validated interpreter. `chromadb` 1.5.9 arrives only when RAG lands.

The server's own `pydantic-ai` version and the generated projects' version are pinned to the same 2.43.0, so the compile agent runs the exact library it writes code against — a property the earlier TypeScript-driver design could not have.

React 19 with `@xyflow/react` 12.11.6 is the pairing to install and smoke-test first in Group 6; if the canvas misbehaves, pin React 18.3 for the web package only, since the server and generated projects are unaffected.

-----

<a id="edge-cases"></a>
## Edge cases

- **Empty graph / single node.** A single node that is both entry and exit must still emit a valid two-edge graph (`start → step → end`).
- **Cycles.** Rejected outright in Phase 1. Fact 12 proves `build()` is **not** a backstop — a self-cycle builds and runs — so Swarm Builder's validator is the only gate; the review names the participating nodes.
- **Orphan nodes.** Rejected in Phase 1 and re-asserted in Phase 5 by comparing `graph.nodes` keys against the canvas slugs, because fact 12 shows an unedged step is silently dropped from the built graph and from `render()`.
- **Multi-successor node without a join.** Hard Phase-1 error (fact 13: the output would be one arbitrary branch).
- **Duplicate titles** → slug collision; deduplicate with a numeric suffix, keep the mapping stable across recompiles, and emit it as an explicit `node_id` (fact 18, which otherwise raises `GraphBuildingError`).
- **Non-ASCII or punctuation-only titles** → slug falls back to `step_<index>`.
- **Port-type mismatch** across an edge is a hard review error. Note pydantic-graph performs **no** runtime type checking at edges — a `list[str]` flowing into a `str`-annotated step runs and delivers the list — so this check exists only here.
- **Programmatic node needing a package** that conflicts with a template pin → surface the conflict from `uv sync` verbatim.
- **`uv` missing from `PATH`** → `/api/health` reports it and the Compile button is disabled with an explanatory message, rather than failing mid-compile.
- **No providers configured** in `$DSH_HOME/settings.yaml` → health check reports it; compile is refused before spawning (fact 4's failure, caught early).
- **No `agent-default-model` section at all** → a normal state, since the authoritative default lives in the base bundle patch, not in settings. Swarm Builder falls back to the bundle default (`deepseek-official` / `deepseek-v4-flash`) and shows the resolved pair plus its source in `/api/health` and the picker, so the user is never silently routed to a model they did not configure (I5).
- **A harness route settings cannot enumerate** (a catalog-only route with no explicit `models:` list, fact 21) → the dropdown shows the route with no models; the free-text field is the way in.
- **A harness route with no PydanticAI counterpart protocol** (fact 23) → refused at Phase 1 naming the route, rather than emitting a project that cannot import.
- **An inherited route needing an extra the generated project lacks** → the extra is derived from the route's `api` at scaffold time, so this surfaces as a generation decision, not a runtime `ImportError`.
- **Recompile of an existing project.** Rescaffold everything and regenerate the fill; v1 does not merge prior edits.
- **Large graphs** (>40 nodes) → warn that one fill run may exceed a comfortable context. No batching path in v1.
- **Browser reload mid-compile** → SSE reconnect replays strictly after the client's `Last-Event-ID`.
- **Concurrent compiles of one graph** → `409`.

-----

<a id="failure-modes"></a>
## Failure modes

| Failure | Detection | Response |
|---|---|---|
| No model route resolvable | `inherit/` finds no settings section and no `SWARM_MODEL` | `/api/health` reports it; Compile is disabled with the two ways to fix it (configure the harness, or set `SWARM_MODEL`) |
| Route credentials missing or expired | the provider raises on first call (e.g. expired AWS SSO token) | fail the compile with the provider's own message and the route name; suggest `aws sso login` for a `bedrock` route |
| Route has no PydanticAI counterpart | `inherit/routes.py` cannot map the `api` protocol (fact 23) | refuse at Phase 1 naming the route, before any scaffolding |
| Fill run wedges or overruns | `UsageLimits` (`request_limit`, `tool_calls_limit`, `cost_limit`) trips, or the job is cancelled | mark failed with the limit that tripped; cancellation is an asyncio task cancel — no process to kill (fact 26) |
| Agent attempts a write outside the project | `confine.py` raises inside the tool before any I/O (fact 26) | tool returns the error to the agent; repeated attempts end the run and fail the compile |
| Agent edits a forbidden file | `write_region` refuses unknown files; Phase-4 whole-file hash re-checks | fail compile, name the path, keep the project for inspection |
| Marker region left empty | Phase-4 marker check | one retry with the failure appended, then fail |
| `uv` missing from `PATH` | `/api/health` probe | disable Compile with an explanatory message rather than failing mid-compile |
| `uv sync` failure | non-zero exit | stream stderr tail; keep project; mark failed |
| Import requires an API key | Phase-5 step 2 | fail with the `ctx.deps.model` rule quoted (facts 7, 16) |
| Branchless decision | Phase-1 check | hard error before compile — it would otherwise build and fail at run time (fact 14) |
| Validation subprocess leaks | only `validate.py` spawns a subprocess (the fill agent spawns none) | explicit timeouts, process-group kill, and `try/finally` termination; server shutdown cancels live compile tasks |
| Disk full / unwritable workspace | store write errors | fail the request with the resolved path |
| SSE client vanishes | write error on the stream | job continues; log retained for `Last-Event-ID` reconnect |

-----

<a id="tests"></a>
## Tests

`pytest` for the server and codegen, Vitest for the frontend, plus generated-project validation as the codegen gate. Aim at behavior that would actually break.

**Unit.**
- `models.py`: schema accepts a valid document; rejects unknown node kinds, bad port types, dangling edge endpoints, and a `fanout` edge with no `join_node_id`.
- Slugification: duplicates, non-ASCII, Python keywords, leading digits.
- Template inference: one case per template plus an ambiguous intent defaulting to `chat`.
- `review.py`: cycle, orphan, unreachable exit, multi-successor without join, branchless decision, type mismatch, missing intent, duplicate state writer, read of an unwritten field, node that is both a delegate and a sequence target, unmappable route protocol.
- `inherit/settings.py`: enumerates routes and models from a fixture `settings.yaml` (`llm-pi-ai.providers` with and without an explicit `models:` list); resolves the effective selection as graph override → `agent-default-model` → env fallback; **runs correctly with no settings file at all** (harness absent); reads per compile rather than caching at boot.
- `inherit/routes.py` (facts 22–23): a known-name route emits a prefixed plain string; a custom `baseURL` route emits the structural `OpenAIChatModel`/`OpenAIProvider` form; each `api` protocol selects the right `pyproject.toml` extra; an unmappable protocol is refused with the route named.
- `confine.py` (fact 26): allows an in-project path; rejects `..` traversal, an absolute path outside the root, and a symlink escaping the root.

**Codegen (the important suite, no model needed).** Fixture graphs — linear chat, websearch, orchestrator with delegation, `fanout`+`join`, `decision` branching, and one mixed graph with a programmatic node — are scaffolded and validated offline with an explicit `UV_CACHE_DIR`. Assertions the emitter cannot trivially satisfy, chosen because facts 12–15 show the naive checks all pass on broken graphs:

- `uv sync`, keyless import, `build()`, and the `TestModel` dry run via `deps` all succeed;
- **`render()` equals a golden diagram** per fixture — probed to be stable, and it includes decision markers and note text, so it is a genuine oracle;
- **`graph.nodes` keys equal the expected set** (canvas slugs + `__start__`/`__end__` + synthetic `*_broadcast_fork`), which catches the silently dropped orphan;
- **join fixtures return the joined collection**, not one arbitrary branch value;
- **negative fixtures**: a cycle, an orphan, a branchless decision, and a joinless fan-out must each be **rejected by Phase 1** — proving the validator, not the framework, holds the line.

**Boundary.** Phase 4 rejects a mutated `graph.py`, an emptied marker region, an edit outside the markers, and an unexpected new file.

**Fill agent (offline, `TestModel`/`FunctionModel`).** The agent's tools are tested without a real provider: `write_region` replaces only the named region and leaves surrounding text byte-identical; a scripted `FunctionModel` that attempts a traversal path gets an error back rather than writing; `UsageLimits` tripping ends the run with the limit named. This is the suite that makes the ~200 owned lines trustworthy.

**Pipeline (via `SWARM_FAKE_FILL=1`).** Full five-phase run with stub fill: phase ordering, cancel mid-run, `409` on concurrent compile, `Last-Event-ID` reconnect replaying no duplicates and no gaps, and job eviction past the retention cap.

**API.** Graph CRUD round-trip preserves the document after re-serialization; export reports a path that exists and contains `pyproject.toml` and `graph.py`; generated frontend types match the OpenAPI schema.

**Live model (opt-in).** One test behind an env flag compiles the linear chat fixture against the real inherited route and asserts the validation gate passes — the automated version of the probes, skipped by default.

**Frontend smoke.** Store round-trip (add node, connect, autosave payload) and a render test that the canvas mounts with a fixture graph.


-----

<a id="acceptance-criteria"></a>
## Acceptance criteria

1. `uv sync && uv run swarm-builder` (plus a one-time `pnpm --dir web build`) starts the server, prints its `127.0.0.1` URL, and serves the canvas.
2. `/api/health` reports the resolved `DSH_HOME`, the resolved provider/model **with its source** (settings section, graph override, or `SWARM_MODEL` fallback), and `uv` presence — and every pre-compile failure mode above surfaces there before a compile is attempted.
3. Building a 3-node workflow (websearch → agent summarize → programmatic format), saving, reloading the browser, and reopening restores the graph exactly.
4. **Compile** streams phases and finishes `succeeded`, and the generated project passes `uv sync`, keyless import, graph build, the `render()` golden, the node-set check, and the `TestModel` dry run.
5. The offline codegen suite passes for all six positive fixtures, and Phase 1 rejects all four negative fixtures, with no API key present.
6. The generated project directory runs `uv sync && uv run python validate/dry_run.py` successfully when copied elsewhere.
7. Template inference selects websearch, orchestrator, and chat correctly for the documented example intents, and the user can override any of them.
8. `/api/models` lists the routes configured in harness settings, the compile uses the inherited default unless overridden in the picker, and the generated project's `.env.example` names that same route while still honoring a `SWARM_MODEL` override.
9. A generated project inheriting a custom `baseURL` route emits the structural `OpenAIChatModel`/`OpenAIProvider` form and the right `pyproject.toml` extra, and still passes the keyless validation gate.
10. A compile whose agent attempts a write outside the project directory is refused by `confine.py` before any I/O, and Phase 4 independently confirms nothing outside the marker regions changed.
11. Swarm Builder starts and compiles with the harness **absent**, using `SWARM_MODEL`; `/api/health` reports the fallback and its source.
12. No file inside the Factored Harness checkout is modified by this work.
13. `README.md` documents quickstart, both model-configuration paths (inherited or `SWARM_MODEL`), and one troubleshooting entry per failure mode.

-----

<a id="assumptions"></a>
## Assumptions

1. **The harness is optional.** Swarm Builder reads `$DSH_HOME/settings.yaml` when it exists to inherit routes (facts 21–23) and otherwise falls back to `SWARM_MODEL`. It links no harness package, spawns no `dsh`, and is unaffected by the harness's build state or version — the reason the tie-in was dropped ([Why not the harness](#why-not-the-harness)).
2. **`DSH_HOME` defaults to the user's real `~/.dsh`**, because that is where the working `amazon-bedrock` route lives (fact 4). Compilation therefore consumes the user's configured credentials and spends tokens on the route shown in the picker.
3. **Provider credentials come from the ambient environment.** Bedrock routes authenticate through boto3's own chain including cached AWS SSO tokens (fact 24), so an expired token is a user-fixable `aws sso login`, not a Swarm Builder concern beyond reporting it.
4. **The inherited model is capable enough** to fill marker regions; no model is pinned. Weak models may need the retry, and the per-workflow picker is the escape hatch.
5. **A generated project's runtime model is a default, not a binding.** It inherits the resolved route into `.env.example` and the README but reads `SWARM_MODEL` at run time, so the export stays portable to a machine with different credentials.
6. **The generated project targets Python ≥3.11**, pinned by `.python-version` to the interpreter the codegen suite validates (3.14.5 in probes), so `uv run` cannot drift.
7. **`pydantic-ai` / `pydantic-graph` 2.43 API stays as probed.** Versions are pinned exactly and shared between the server and generated projects, so an upstream rename cannot silently change generated code; a version bump is a deliberate change with the codegen suite — golden diagrams included — as its gate.
8. **Local single-user trust.** The server binds `127.0.0.1`, has no auth, and treats the graph document as trusted input; it is a developer tool, not a hosted service.
9. **The agent's filesystem reach is the project directory**, enforced inside the tools by `confine.py` (fact 26) and re-checked in Phase 4. Since the agent has no shell, no general file write, **and no way to execute code at all**, this is the one security-relevant invariant in v1 and it is owned code with its own tests. This holds only because the plan's `run_check` was narrowed to the in-process `parse_check` during implementation: as specified it ran arbitrary model-authored Python and was verified writing outside the project, where Phase 4 cannot see it (see the amendment under Phase 3). `compile/agent.py` now imports no `subprocess`, `os`, or `signal`.
10. **Export is a folder, not a deployment.** No tarball, packaging, Docker, or CI is generated.

-----

<a id="explicitly-out-of-scope-for-v1"></a>
## Explicitly out of scope for v1

Confirmed exclusions: authentication and multi-user accounts; realtime collaboration or shared cursors; frameworks other than PydanticAI (the exporter interface stays a seam, with exactly one implementation); deployment or packaging of generated workflows.

Cut during plan review to keep v1 lightweight, each recoverable later without reshaping the pipeline:

- **the `rag` template and ChromaDB** — fact 8 already shows embedder and cache friction, and it is the least related to compiling a canvas into a graph; the template registry is the seam that lets it land later;
- **`tar.gz` export** — the generated project is already a self-contained folder, so export reveals the path and the `uv run` command;
- **a `POST /api/templates/infer` endpoint** — inference is a pure function shared with the client, called client-side;
- **recompile edit-preservation and `.bak` retention** — real diff machinery; v1 regenerates and says so;
- **node-batched fill for large graphs** — speculative; the warning stays, the batching path does not.

Also deferred: undo/redo history, graph versioning beyond `version: 1`, cloud vector stores, cycles and loop constructs, executing the compiled workflow with real credentials from the UI, and any modification of the Factored Harness repository.

**Deliberately not built (architecture revision):** the harness SDK bridge — no `dsh` subprocess, no JSON-RPC client, no profile or patch management, no harness version coupling. If a future need arises that PydanticAI genuinely cannot serve (harness-session auditing of compiles, or reusing harness tools such as `web_search`/skills inside the fill loop), `compile/agent.py` is a small enough surface to sit behind an interface with a harness-backed implementation added then. Facts 1–5 and 19 are retained so that decision can be revisited with evidence rather than re-probed from scratch.
