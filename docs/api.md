# Swarm Builder — HTTP API reference

The server is a FastAPI app that binds `127.0.0.1` only (see
`src/swarm_builder/config.py` for the default `PORT`). Every endpoint lives
under `/api`; everything else is the built frontend served from `web/dist`.

This document is written to be sufficient to implement a client against —
the SSE framing and reconnect contract in particular is specified down to
the byte. For the *why* behind these shapes see
[`../PLAN.md`](../PLAN.md); for how the pieces fit together see
[`architecture.md`](./architecture.md); for getting the server running see
[`../README.md`](../README.md).

- [Conventions](#conventions)
- [GET /api/health](#get-apihealth)
- [GET /api/models](#get-apimodels)
- [GET /api/templates](#get-apitemplates)
- [Graph CRUD](#graph-crud)
- [POST /api/graphs/:id/review](#post-apigraphsidreview)
- [GET /api/graphs/:id/export](#get-apigraphsidexport)
- [Compile endpoints](#compile-endpoints)
- [The route-resolution warning: `unknown_model_name`](#the-route-resolution-warning-unknown_model_name)
- [SSE event stream](#sse-event-stream)
- [Appendix: the graph document](#appendix-the-graph-document)

## Conventions

**camelCase on the wire.** Every response body and every request body uses
camelCase keys. On the Python side the graph document's fields are
snake_case, with `alias_generator=to_camel` producing the JSON spelling and
`populate_by_name=True` accepting either spelling on input. Response models
in `routes/*.py` replicate that two-line config locally. Serialization
always emits the camelCase alias.

**Errors are FastAPI's.** A failure is an HTTP status plus a JSON body
`{"detail": ...}`. `detail` is a string for most failures and a structured
object for the two that carry extra data: the
[ring-buffer gap](#last-event-id-and-reconnecting) 400 and the
[cancel-after-finish](#delete-apicompilecompileid) 409.

**Nothing is cached.** `config.get_*()` and `inherit.settings.*` are called
fresh inside every handler. Editing `$DSH_HOME/settings.yaml` or an
environment variable changes what `/api/health` and `/api/models` report
**without restarting the server** (verified live). The one deliberate
exception is the compile job registry, which is process-lifetime state by
definition.

**Graph ids** must match `^[A-Za-z0-9_-]{1,128}$`. A path parameter that
does not is a `422`, never a `500`, and never reaches the filesystem.

## GET /api/health

Pre-flight state: what is configured, what is writable, and whether a
compile is expected to succeed right now.

```bash
curl -s http://127.0.0.1:8420/api/health | python3 -m json.tool
```

```json
{
  "version": "0.1.0",
  "dshHome": "/Users/you/.dsh",
  "settingsError": null,
  "resolvedModel": {
    "provider": "deepseek-official",
    "model": "deepseek-flash",
    "source": "settings-default"
  },
  "uvAvailable": true,
  "uvCacheDir": "/Users/you/swarm-builder/.uv-cache",
  "uvCacheWritable": true,
  "workspaceDir": "/Users/you/swarm-builder/workspace",
  "workspaceWritable": true,
  "webDistPresent": true,
  "compileReady": true,
  "blockers": []
}
```

| Field | Meaning |
|---|---|
| `version` | `swarm_builder.__version__`. |
| `dshHome` | The resolved `DSH_HOME` (default `~/.dsh`). |
| `settingsError` | Non-null when `settings.yaml` exists but could not be read or parsed — a real user mistake, distinct from the file being absent. |
| `resolvedModel.provider` / `.model` | The model a compile would spend right now. |
| `resolvedModel.source` | `graph-override`, `settings-default`, `env-fallback`, or `bundle-default`. **This is the field that tells you where the route came from.** |
| `uvAvailable` | Whether `uv` is on `PATH`. |
| `uvCacheDir` / `uvCacheWritable` | The resolved `UV_CACHE_DIR` and whether it can be written. |
| `workspaceDir` / `workspaceWritable` | Where graphs and generated projects live, and whether it is writable. |
| `webDistPresent` | Whether `web/dist/index.html` exists, i.e. whether `/` serves the canvas. |
| `compileReady` | `true` only when `blockers` is empty. |
| `blockers` | One human-readable sentence per reason a compile is not expected to succeed. |

`compileReady` is `false`, with a matching entry in `blockers`, when any of:
the resolved model came from the bundle fallback (nothing the user actually
configured), `uv` is not on `PATH`, the workspace is not writable, or
`settings.yaml` exists but failed to parse. `status: 200` in all of these
cases — a blocker is reported, not raised.

## GET /api/models

The model picker's data source: every route `settings.yaml` declares, plus
the resolved default and where it came from.

```json
{
  "routes": [
    {
      "key": "amazon-bedrock",
      "api": "bedrock-converse-stream",
      "hasExplicitModels": true,
      "emission": "known-name",
      "requiredExtra": "bedrock",
      "unmappableReason": null,
      "models": [{ "id": "us.anthropic.claude-opus-5", "name": "Claude Opus 5 (US)" }]
    },
    {
      "key": "kornerstone",
      "api": "openai-completions",
      "hasExplicitModels": true,
      "emission": "structural",
      "requiredExtra": "openai",
      "unmappableReason": null,
      "models": [{ "id": "qwen38-27b-fp8", "name": "Qwen 3.8-27B" }]
    }
  ],
  "resolvedDefault": {
    "provider": "amazon-bedrock",
    "model": "us.anthropic.claude-opus-5",
    "source": "settings-default"
  },
  "settingsError": null
}
```

| Field | Meaning |
|---|---|
| `routes[].key` | The `llm-pi-ai.providers` map key — what a graph's `model.provider` override matches. |
| `routes[].api` | The wire protocol, explicit or (for a Bedrock route with only `awsProfile`/`awsRegion`) inferred as `bedrock-converse-stream`. `null` when undeterminable. |
| `routes[].hasExplicitModels` | Whether the route lists `models:` in settings. A catalog-only route has none, which is why the picker also accepts free text. |
| `routes[].emission` | `known-name` (a prefixed string handed to `Agent`), `structural` (`OpenAIChatModel` + `OpenAIProvider` from `baseURL`), or `unmappable`. |
| `routes[].requiredExtra` | The `pydantic-ai-slim[...]` extra the generated project needs for this route. |
| `routes[].unmappableReason` | Why the route cannot be used, when `emission` is `unmappable`. |
| `settingsError` | Same value as in `/api/health`; explains an empty `routes` array. |

A route whose `api` protocol has no PydanticAI counterpart *and* has no
`baseURL` is reported `unmappable` here and refused at Phase 1 with the
route named — before any scaffolding — rather than producing a project that
cannot import.

### THE CORRECTNESS TRAP, stated as a client rule

**A declared `baseURL` always wins, before the known-name prefix table is
consulted.** A route's model id can collide, as a bare string, with a real
PydanticAI known name from an entirely different provider — the settings in
this repo's own task brief have a self-hosted `kornerstone` route whose id
`deepseek-v4-flash` is also a member of PydanticAI's `deepseek:` family.
Choosing the known-name path because the id matched would silently send the
compile to the official DeepSeek API instead of the user's server: same
string, wrong endpoint, no error. Hence `emission: "structural"` for every
route with a `baseURL`, unconditionally.

## GET /api/templates

The three v1 templates, in catalog order.

```json
[
  {
    "id": "chat",
    "label": "Chat",
    "description": "A single conversational agent.",
    "defaultTools": [],
    "requiredEnv": []
  },
  {
    "id": "orchestrator",
    "label": "Orchestrator",
    "description": "Delegates to child agents, wrapped as tool functions -- children are never graph steps.",
    "defaultTools": [],
    "requiredEnv": []
  },
  {
    "id": "websearch",
    "label": "Web search",
    "description": "A research step using WebSearchTool passed via toolsets=, never builtin_tools= or tools=.",
    "defaultTools": ["web_search"],
    "requiredEnv": []
  }
]
```

`rag` is deferred out of v1; the registry keeps the seam.

**There is deliberately no `/api/templates/infer`.** Template inference is a
pure keyword-counting function with no I/O and no model call
(`templates/registry.py:infer_template`). The frontend carries a copy of the
same scoring rule (`web/src/infer.ts`) and calls it locally, so the
Inspector can preselect a template without a round trip. The two
implementations must stay in parity: an intent is lowercased, each
template's keywords are counted as substrings, the highest count wins, and a
tie — including the 0-0 tie of an intent matching nothing — resolves to
`chat`.

| Template | Keywords |
|---|---|
| `websearch` | `search`, `browse`, `news`, `latest` |
| `orchestrator` | `delegate`, `coordinate`, `route`, `sub-agent`, `plan and assign` |

## Graph CRUD

### GET /api/graphs

Summaries plus any file that failed to load. One corrupt file never hides
every other saved graph — that request still succeeds with `200`.

```json
{
  "graphs": [
    { "id": "linear-chat", "name": "Linear chat", "updatedAt": "2024-01-01T00:00:00Z", "nodeCount": 3, "edgeCount": 2 }
  ],
  "errors": [{ "id": "broken", "detail": "..." }]
}
```

### GET /api/graphs/:id

The full graph document (see [the appendix](#appendix-the-graph-document)).

| Status | When |
|---|---|
| `200` | The document, validated against `SwarmGraph`. |
| `404` | No such graph. The detail names the resolved `.json` path that was checked. |
| `422` | The id is malformed or path-unsafe, **or** the file exists but is not valid JSON or fails `SwarmGraph` validation. A file this endpoint cannot read is a bad request against this id, not a server malfunction. |
| `500` | Any other filesystem failure while reading. |

### PUT /api/graphs/:id

Validate and atomically write a graph document. The body is a full
`SwarmGraph`; `extra="forbid"` means an unknown key is a `422`.

Two server-side rules:

- **The path id is authoritative.** A body `id` that disagrees with the path
  parameter is `422`, naming both, rather than being silently overwritten or
  silently accepted.
- **`updatedAt` is always server-stamped** to `datetime.now(UTC)`, replacing
  whatever the client sent. The response body carries the stamp that was
  actually persisted.

Returns `200` with the stored document. `422` for schema validation failure
(including `SwarmGraph`'s own structural-integrity validator) or id mismatch;
`500` for a store I/O failure.

### DELETE /api/graphs/:id

Removes the graph document. Query parameter `project=true` additionally
removes the generated project directory — idempotent, so a graph that was
never compiled is not an error.

```json
{ "deleted": true, "projectDeleted": false }
```

`404` when the graph itself does not exist; the `?project` flag never
changes that. `422` for a malformed id; `500` for a store I/O failure.

## POST /api/graphs/:id/review

Phase 1's static validator, run on demand — "the initial review" before a
compile. Same code path the compile pipeline uses, so a clean review here
means Phase 1 will pass there (subject to the route-resolution check, which
depends on settings rather than the document).

```json
{
  "ok": true,
  "errors": [],
  "warnings": [
    { "code": "join_with_few_inputs", "message": "...", "nodeIds": ["merge"] }
  ]
}
```

`ok` is `true` only when `errors` is empty. `errors` block a compile;
`warnings` never do. `404` for an unknown graph, `422` for a malformed id or
an unreadable document, `503` if the compile subsystem cannot be imported.

**Review error codes** (all block a compile):

| Code | Meaning |
|---|---|
| `unknown_edge_endpoint` | An edge names a source or target node id that does not exist. |
| `invalid_node_id` | A node id is not a valid Python identifier, or collides with a Python keyword. |
| `kind_spec_mismatch` | A node's `kind` does not match the single spec field it carries. |
| `missing_intent` | A node has no `intent` text. |
| `delegate_and_sequence_target` | A node is both a `delegate` target and a `seq`/`branch` target — it would execute twice. |
| `no_path_to_exit` | No path from `entryNodeId` to `exitNodeId`. |
| `unreachable_node` | A node is not reachable from the entry. |
| `cycle` | Any cycle. Rejected outright: `pydantic_graph`'s `build()` accepts a self-cycle and runs it. |
| `fanout_without_join` | A multi-successor node whose successors are not `fanout` edges converging on a declared join. |
| `fanout_arm_missing_join_edge` | A `fanout` arm has no matching `join` edge. |
| `branchless_decision` | A decision node with zero branches — it builds, then fails at run time with `RuntimeError: No branch matched inputs`. |
| `decision_branch_mismatch` | A `branch` edge whose target is not a declared decision branch, or vice versa. |
| `decision_branch_type_mismatch` | A branch target's `io.inputType` does not equal the **decision's source step's** `outputType`. |
| `decision_no_source` / `decision_multiple_sources` | A decision with no single feeding step, or with several. |
| `port_type_mismatch` | Port types disagree across an edge. |
| `sink_output_type_mismatch` | The exit node's output type does not match the graph's declared output. |
| `undeclared_state_write` | A `writes` entry naming a field not in `stateFields`. |
| `duplicate_state_writer` | Two nodes writing the same state field. |
| `unwritten_state_read` | A `reads` field no upstream node writes. |

**Review warning codes** (never block): `join_with_few_inputs`
(fewer than two inbound arms), `join_output_type_unusual` (a list reducer
with a non-`list[str]` output type), `delegate_without_edge` (an
`agent.delegatesTo` entry with no `delegate` edge on the canvas).

**This endpoint returns review findings only.** Phase 1 also resolves the model
route, and one resolution result is a warning — see
[the route-resolution warning](#the-route-resolution-warning-unknown_model_name)
under the compile endpoints. It is **not** in this response's `warnings` array:
`POST /api/graphs/:id/review` answers from the validator alone, while
route-resolution warnings are emitted on the compile's event stream. A clean
review here therefore does not promise a warning-free compile.

## GET /api/graphs/:id/export

Optional `?target=pydantic-graph|langgraph` (default `pydantic-graph`) selects
which export to report; the response carries `target` back. A `404` names the
directory the requested target would have been written to.

Hands back the generated project's location and the exact command that runs
it. This route checks for a **project directory**, not a graph document:
`projectExists` is validated against `graph_id` as a path segment, so an
invalid id is a `422` before any filesystem check.

```json
{
  "projectPath": "/Users/you/swarm-builder/workspace/projects/linear-chat",
  "runCommand": "cd /Users/you/swarm-builder/workspace/projects/linear-chat && UV_CACHE_DIR=/Users/you/swarm-builder/.uv-cache uv sync && UV_CACHE_DIR=/Users/you/swarm-builder/.uv-cache uv run python validate/dry_run.py"
}
```

`runCommand` is byte-identical to the `runCommand` in the compile `done`
event, and spells `UV_CACHE_DIR` out rather than assuming the reader's shell
has it set. Then `uv run python -c "import swarm_workflow.graph"` is the
third thing to try; the generated project's own `README.md` lists all three.

| Status | When |
|---|---|
| `200` | A compiled project exists at `workspace/projects/<graphId>/`. |
| `404` | No compiled project yet. The detail names the path that was expected, which was never created. |
| `422` | Malformed `graph_id`. |

## Compile endpoints

A compile is one asyncio task inside the server process. There is no
subprocess and no watcher, so there is nothing to reap and nothing to leak.

### POST /api/compile

```json
{ "graphId": "linear-chat", "target": "pydantic-graph" }
```

`target` is optional: `pydantic-graph` (default) runs the five phases;
`langgraph` runs them and then four more (`lg_scaffold`, `lg_convert`,
`lg_boundary`, `lg_validate`) that convert the validated project into a
LangGraph export under `workspace/projects-langgraph/<graphId>/`. Every
`phase` frame of a LangGraph compile reports `total: 9` and indices 6-9 for
the extra phases; the `done` payload gains a `langgraph` block:
`{projectPath, runCommand, diagram, convertedNodeIds, attempts}` (the diagram
is `draw_mermaid()` output). An unknown target is a `422`.

Returns as soon as the job is registered and its task is scheduled; it never
awaits the compile.

```json
{ "compileId": "5783640a1b0440a8bad3ebda0f6725f7" }
```

`compileId` is a UUID4 hex string. Everything else about the compile comes
from the event stream and the status snapshot.

| Status | When |
|---|---|
| `200` | Job registered. |
| `404` | No such saved graph. |
| `409` | A live (queued or running) compile already exists for this graph. The detail names the live `compileId`. One concurrent compile per graph; cancel it or wait. |
| `422` | A malformed or path-unsafe `graphId`, or a graph file that is not valid JSON or fails `SwarmGraph` validation. |
| `500` | A filesystem failure while reading the graph. |
| `503` | The compile subsystem could not be imported. |

The project directory is **not** created or cleared here. Phase 2 clears a
stale one itself, after Phase 1 has passed, which is what keeps "a Phase-1
refusal leaves no project directory behind" true.

### GET /api/compile/:compileId

The status snapshot. This is the path a **fresh tab** uses: it has no
`Last-Event-ID` and no way to reconstruct history.

```json
{
  "compileId": "5783640a1b0440a8bad3ebda0f6725f7",
  "graphId": "linear-chat",
  "status": "succeeded",
  "createdAt": "2026-09-16T23:36:50.614212+00:00",
  "startedAt": "2026-09-16T23:36:50.626227+00:00",
  "finishedAt": "2026-09-16T23:36:53.138777+00:00",
  "latestEventId": 14,
  "result": {
    "projectPath": "/private/tmp/.../projects/linear-chat",
    "runCommand": "cd ... && UV_CACHE_DIR=... uv sync && UV_CACHE_DIR=... uv run python validate/dry_run.py",
    "diagram": "stateDiagram-v2\n  intake\n  chat_step\n  summarize\n\n  [*] --> intake\n  intake --> chat_step\n  chat_step --> summarize\n  summarize --> [*]",
    "filledNodeIds": ["intake", "summarize"],
    "attempts": 1,
    "model": { "provider": "deepseek-official", "model": "deepseek-flash", "source": "settings-default" }
  },
  "error": null
}
```

Above is a **real snapshot** from this checkout for a three-node graph compiled
with `SWARM_FAKE_FILL=1`. Its job emitted 14 events because the resolved route
earned an extra two — a `log` naming the resolved model, and the
[`unknown_model_name` warning](#the-route-resolution-warning-unknown_model_name).
Event counts are graph- and route-dependent, so do not assert a fixed number.

`status` is one of `queued`, `running`, `succeeded`, `failed`, `cancelled`.
`reasoningEffort` and route internals are deliberately not exposed here; the
picker-facing detail lives in `/api/models`.

`latestEventId` is the cursor a resuming client passes back as
`Last-Event-ID`, and it is `null` for a job with an empty log.

On `succeeded`, `result` carries the project path, the run command, the
golden diagram Phase 2 captured, the ids Phase 3 filled, the number of fill
attempts (1 or 2), and the resolved model **with its source** — so a client
can always show which route a compile spent. On `failed`, `error` is the
failing module's own message; the *where* is in the last `phase` event with
`status: "failed"` in the event stream. On `cancelled`, both are `null`.

**The snapshot carries no `phases`, no `logTail` and no `warnings`.** Those
three live in the job's retained event log and reach a client only through the
SSE stream. This is the one asymmetry a resuming client has to be built for:
apply `status` / `latestEventId` / `result` / `error` from the snapshot, then
resubscribe with `Last-Event-ID: <latestEventId>` and let the replay refill the
phase list and the log. Treating the snapshot as a complete client-side state
and overwriting those three with `undefined` is what used to blank the panel
after a mid-compile reload.

**`done` carries one field the snapshot's `result` does not:**
`validationSteps` (Phase 5's step names, e.g. `["uv_sync", "keyless_import",
"dry_run"]`). Everything else in the two payloads is identical, so the same
renderer can consume both — but a client that wants the step list must read it
from the event, or from the `validate` phase's `succeeded` event (which carries
it as `steps`).

| Status | When |
|---|---|
| `200` | Snapshot. |
| `404` | No such job — never existed, or evicted (see the retention cap below). |
| `503` | The compile subsystem could not be imported. |

### DELETE /api/compile/:compileId

Cancels a live compile. Cancelling the job's asyncio task **is** the whole
mechanism: the fill agent runs in-process, so there is no subprocess to
kill. The job ends `cancelled` and the response is the same snapshot shape
as `GET`, with `status: "cancelled"`.

**A job that already finished is a `409`, not a no-op `200`.** The request
asks for a transition that is no longer possible and cannot be retried into
success, so the honest answer is a conflict naming the status the job
actually reached — with the full snapshot in the body, so the client needs
no second request to learn the outcome it was too late to prevent.

```json
{
  "detail": {
    "message": "cannot cancel job '5783...': already 'succeeded'",
    "compileId": "5783640a1b0440a8bad3ebda0f6725f7",
    "status": "succeeded",
    "snapshot": {
      "compileId": "5783640a1b0440a8bad3ebda0f6725f7",
      "graphId": "linear-chat",
      "status": "succeeded",
      "latestEventId": 13,
      "result": { "projectPath": "/...", "runCommand": "cd ...", "diagram": "stateDiagram-v2\n...", "filledNodeIds": ["intake"], "attempts": 1, "model": { "provider": "deepseek-official", "model": "deepseek-flash", "source": "settings-default" } },
      "error": null
    }
  }
}
```

| Status | When |
|---|---|
| `200` | The job transitioned to `cancelled`. |
| `404` | No such job. |
| `409` | The job is no longer live. |
| `503` | The compile subsystem could not be imported. |

Cancelling a job mid-phase does not delete the project directory; a
partially scaffolded or partially filled project is left on disk.

### GET /api/compile/:compileId/events

The SSE stream. See the next section.

## Run endpoints

A run executes an already-compiled project in a subprocess with the server's
real credentials and streams one event per step. It is a job of kind `run`
in the **same registry** as a compile, so its live stream, status snapshot and
cancel are the generic job endpoints below; only starting a run and its
persisted history are run-specific. A live compile and a live run on the same
graph exclude each other (`409`), because both touch the project directory.

### POST /api/graphs/:id/runs

```json
{ "input": "swarm builder", "compileIfStale": true }
```

`input` is interpreted by the entry node's `inputType`: a `str` port takes the
string verbatim; `json` and `list[str]` take either the parsed value or a JSON
string of it. `compileIfStale` (default `true`) recompiles first when the
project is missing, predates the tracer script, or its `graph.py` is older
than the graph's `updatedAt`; the compile's `phase` frames then stream into
the run's own job. With `compileIfStale: false` a stale project is a `409`.

```json
{ "runId": "2f7770d13af6461dbe417ab5dbd7a289", "willCompile": true }
```

| Status | When |
|---|---|
| `202` | Job registered and scheduled. |
| `404` | No such saved graph. |
| `409` | A live compile or run already exists for this graph, or the project is stale and `compileIfStale` is off. |
| `422` | The input does not fit the entry node's port type (the detail says which). |
| `503` | The run subsystem could not be imported. |

### GET /api/jobs/:id/events, GET /api/jobs/:id, DELETE /api/jobs/:id

The compile job endpoints under a kind-neutral path: identical framing,
`Last-Event-ID` replay, snapshot shape (now with `kind: "compile" | "run"`)
and cancel semantics. A cancelled run kills the subprocess **group** (`uv`
plus the interpreter it forked) before the job ends `cancelled`.

### GET /api/graphs/:id/runs and GET /api/graphs/:id/runs/:runId

Persisted run records, newest first, at most 20 per graph
(`workspace/runs/<graphId>/<runId>.json`). A record carries `status`, the
`input`, the final `output` and `state`, `model`, `durationMs`, `compiled`
(whether a compile preceded the run), `error`, and `nodes`: per step, the
last reported `status`, `inputs`, `output`, `stateDelta`, `error`,
`durationMs`. Written when the run starts and rewritten when it ends, so
history survives a server restart.

### Run frames on the event stream

Two frame types beyond the compile's five:

| `event` | Payload |
|---|---|
| `run` | `{status: "compiling" \| "starting" \| "started", model?, input?, compiled?}` — where the job is. `compiling` precedes a compile-if-stale; `started` carries the model the project resolved and the coerced input. |
| `node` | `{nodeId, status: "started" \| "succeeded" \| "failed", inputs?, output?, stateDelta?, error?, traceback?, durationMs?}` — one `started` and one terminal frame per step (`agent`/`programmatic`). Decision and join nodes are builder constructs with no step function and are not traced; the canvas derives their state from their neighbours. Values longer than 4000 characters arrive as `{"__preview__": "...", "__truncated__": true}`. |

A run's terminal `done` payload is `{output, state, durationMs, model,
compiled, finishedAt}` (also the snapshot `result`), and its `error` payload
is `{code: "run_failed", message, exception}`. Stderr from the subprocess
arrives as `log` frames with `stream: "stderr"`.

## POST /api/graphs/generate

Describe a workflow in prose; get a saved, review-clean graph back.

```json
{ "description": "Take a support ticket. Classify it as billing or technical. ...", "name": "Triage", "graphId": null, "modelOverride": null }
```

The model emits a relaxed draft (nodes by title, edges by title, no ids or
positions); the server derives ids with the same slugifier the canvas uses,
builds decision branches and fan-out/join wiring, lays the nodes out
left-to-right, and runs Phase 1's `review`. Errors are fed back to the model
for up to two repair rounds. `graphId` reuses an existing id (replacing that
document); omitted, a new UUID is minted. The result is saved through the
graph store before it is returned.

```json
{ "graph": { "...": "a SwarmGraph" }, "warnings": [], "attempts": 1, "model": { "provider": "...", "model": "...", "source": "settings-default" } }
```

| Status | When |
|---|---|
| `200` | Saved and returned. `warnings` are the reviewer's non-blocking findings. |
| `422` | Empty/oversized description, or every attempt failed — the detail is `{message, problems, attempts}` with the last round's review findings. |
| `503` | No model route configured (bundle default), an unmappable route, or the generator could not be imported. |

`SWARM_FAKE_GENERATE=1` replaces the model with a deterministic
sentence-per-step draft so the endpoint works with no credentials.

## The route-resolution warning: `unknown_model_name`

The fourth finding code a compile can report, alongside the three review
warnings. It is documented separately from the review codes because
`POST /api/graphs/:id/review` does **not** return it — the review endpoint
answers from the static validator, while this one comes from Phase 1's route
resolution and rides the compile's own `warning` frame:

```json
{
  "code": "unknown_model_name",
  "message": "the inherited default 'deepseek:deepseek-flash' is not a model name this pydantic-ai knows, so the exported project will fail when run with real credentials even though the keyless gate passes; check the 'deepseek-official' model id in your harness settings",
  "nodeIds": []
}
```

It fires when the resolved route takes the **known-name** emission path and the
prefixed name it would write into the generated project is not a member of the
installed `pydantic-ai`'s `KnownModelName` union
(`inherit/routes.py:is_known_model_name`). Three properties matter to a client:

- **It never blocks, and the compile still succeeds.** The union is pinned to
  the installed `pydantic-ai`, so a model id from a genuinely newer provider
  release must not be refused. Agents in the generated project are built with
  `defer_model_check=True` and the dry run injects `TestModel()`, so the keyless
  gate passes either way — which is exactly why the warning exists, since
  otherwise the mismatch would surface only when someone ran the exported
  project with real credentials.
- **Only the known-name path can be checked.** A custom-`baseURL` route names a
  model on someone else's endpoint, so membership in PydanticAI's union says
  nothing about it. The check reads the emitted `SWARM_MODEL=` line, so a bare
  model id (no `:` prefix) or a route that also emits `SWARM_BASE_URL=` is
  skipped silently.
- **It is not counted in `warningCount`.** The `review` phase's `succeeded`
  event reports `warningCount: len(ReviewResult.warnings)`, and this warning is
  emitted after that count is taken. A client that wants every warning must read
  the `warning` frames, not the count. `nodeIds` is always empty, since the
  warning is about the route rather than about any node.

`/api/models` does not flag an unknown name either — it reports what
`settings.yaml` declares, not what the installed `pydantic-ai` recognizes. A
client that wants to warn earlier can apply the same membership test to
`resolvedDefault`.

## SSE event stream

`GET /api/compile/:compileId/events` returns `text/event-stream`. The
response is deliberately **finite**: it ends after the compile's terminal
`done` or `error` frame, so a client's reader completes rather than hanging
on a stream that will never produce another frame.

### Response headers

```
HTTP/1.1 200 OK
content-type: text/event-stream; charset=utf-8
cache-control: no-store
x-accel-buffering: no
transfer-encoding: chunked
```

### Framing

Frames are separated by a blank line, and this server writes **CRLF** line
endings — a parser must accept `\r\n\r\n`, `\n\n` and `\r\r` as frame
separators, and `\r\n`, `\n`, `\r` as line separators. Each frame has three
fields, in this order:

```
id: 1\r\n
event: phase\r\n
data: {"name":"review","index":1,"total":5,"status":"started","eventId":1,"createdAt":"2026-09-16T23:40:20.053687+00:00"}\r\n
\r\n
```

- `id` — the event's own monotonic integer, unique within the job and
  starting at 1. **Echo this back as `Last-Event-ID` on reconnect.**
- `event` — the compile event type, so a client dispatches on the SSE event
  name instead of inspecting a discriminator inside the JSON.
- `data` — the payload as **JSON** (not a Python `repr`), plus `eventId` and
  `createdAt`. A consumer that parses only `data` still sees the complete
  record.

Every 15 seconds the server emits an SSE comment line whose text begins
`: ping - ` (followed by a UTC timestamp) as a keep-alive, because a phase —
Phase 5's `uv sync` in particular — can hold the connection idle for
minutes. Comment lines have no `data` field at all, so a parser that only
looks for `data` fields can ignore them; a client that wants to prove its
reader loop is still alive should treat the arrival of any bytes as
liveness.

### The five event types

| `event` | Payload |
|---|---|
| `phase` | `{name, index, total, status, ...}` — see below. |
| `log` | `{message}`. Sometimes also `{code, nodeIds, severity}` when a Phase-1 *error* finding is reported; the SSE vocabulary has no per-finding error type, so an individual error rides a `log` frame carrying the same shape a `warning` uses plus `severity: "error"`. A plain `log` frame has **no `code` and no `nodeIds`** — a client must not assume they are present. |
| `warning` | `{code, message, nodeIds}` for a non-blocking finding. Every Phase-1 review warning uses this shape, and so does `unknown_model_name`. |
| `done` | The full success payload, identical to `result` in the status snapshot, **plus `validationSteps`**. Terminal. |
| `error` | `{code, message, exception}`. `code` is always `phase_failed`; `exception` is the exception class name. Terminal. |

Every payload additionally carries `eventId` and `createdAt`, as [Framing](#framing)
states — so a consumer that parses only `data` still sees the complete record.

A real `warning` frame from this checkout, which is what `unknown_model_name`
looks like on the wire:

```
id: 3\r\n
event: warning\r\n
data: {"code":"unknown_model_name","message":"the inherited default 'deepseek:deepseek-flash' is not a model name this pydantic-ai knows, so the exported project will fail when run with real credentials even though the keyless gate passes; check the 'deepseek-official' model id in your harness settings","nodeIds":[],"eventId":3,"createdAt":"2026-09-17T01:18:32.711581+00:00"}\r\n
\r\n
```

`phase` events carry `name` (one of `review`, `scaffold`, `fill`,
`boundary`, `validate`), `index` (the phase's 1-based position, so a client
never hardcodes an ordering), `total` (5, or 9 for a `target: "langgraph"` compile), and `status`:

- `started` — each phase emits exactly one.
- `succeeded` — with phase-specific extras: `review` adds `errorCount`,
  `warningCount` and `route: {provider, model, source}`; `scaffold` adds
  `projectPath` and `fileCount`; `fill` adds `attempt` and `filledNodeIds`;
  `validate` adds `steps: ["uv_sync", "keyless_import", "dry_run"]`.
- `failed` — with `message` and a structured `details` object (review
  findings, boundary violations, the failing validation step's name, exit
  code and command).

A phase emits one `started` and then either one `succeeded` or one `failed`
— never both, and never a second `started` without a terminal status in
between. **A retried Phase 3 shows up as a second `started`** for `fill`,
which is how a UI renders "retrying".

**The stream also ends on a `failed` `phase` event**, not only on
`done`/`error`. A failing phase emits its `failed` phase frame and then the
pipeline's terminal `error` frame, so ending on both means a job that dies
without its own terminal event still closes the response.

### Last-Event-ID and reconnecting

Send `Last-Event-ID: <n>` to resume. The server replays every retained event
with id **strictly greater than** `n`, then continues live. Replaying from
`0` is equivalent to requesting the entire retained log.

**The replay happens exactly once.** A live job's history is replayed by
`stream_events` itself, which yields `events_after(last_event_id)` before it
subscribes to the live tail; the route does not replay on top of that. This
matters because it did once: a second replay in the route sent every retained
frame twice (measured on a 4-event job with cursor `2`: ids 3, 4, 3, 4), which
is the duplicate-log failure the `Last-Event-ID` design exists to prevent. So
the contract a client can rely on is:

- Every retained event with id greater than the cursor is delivered **once**,
  in ascending id order, and then each subsequently appended event once. No
  duplicates, no gaps within the retained range.
- Duplicate *suppression* is therefore the server's job, not something a client
  must defensively dedupe by id — though a client that does dedupe by id is
  still correct, and is the cheapest way to be sure.

Contract, precisely:

1. A header that is absent or blank → no cursor; the full retained log is
   replayed before the live tail.
2. A header that is not an integer → **`400`**, with a detail naming the
   compile and the offending value. A client that sent a malformed cursor is
   told so rather than being silently served a replay it did not ask for.
3. A valid integer → replay strictly after it, then live events.
4. A cursor that has **fallen off the back of the ring buffer** → a gap. See
   below.

**A finished job's log is complete, so its whole response is a replay.** A job
that already reached a terminal status sent no live events and never will, so
`GET .../events` on it replays its retained log and then sends nothing more —
which means a cursor at or past its newest retained id yields an **empty
response that ends immediately**. That is not a failure and not a lost
connection: it is the expected answer for "I have already seen everything this
finished job has to say." A resuming client should compare its cursor with the
snapshot's `latestEventId` before subscribing, or it will read the empty
stream as a dropped connection and retry into a reported error for a compile
that actually succeeded. `web/src/api/compileWire.ts:hasUnseenEvents` is that
comparison.

Each job retains the last **2000** events (`RING_BUFFER_LINES`). Finished
jobs are retained up to **20** (`MAX_RETAINED_JOBS`), dropped oldest-first; a
running job is never evicted. A `404` on any `/api/compile/:compileId*` path
means the job never existed or has been evicted, and the client should fall
back to a fresh `POST /api/compile`.

**The gap case, and why it is answered two different ways.** A cursor older
than the oldest retained event cannot be served a correct replay — returning
what remains would look like a complete history while silently skipping the
middle. So:

- Raised **before any frame has been written**, the response is a real
  **`400`** with a structured body, which is strictly better than a body the
  client has to scrub for a warning:

  ```json
  {
    "gap": true,
    "message": "compile log history was evicted before this client could reconnect: events after id 12 are no longer retained (oldest retained id is 300); fetch the status snapshot instead of resuming the stream",
    "compileId": "...",
    "requestedLastEventId": 12,
    "oldestRetainedEventId": 300,
    "snapshotUrl": "/api/compile/<compileId>"
  }
  ```

- Raised **after frames are already on the wire**, the status is committed
  to `200` and only an in-band report is possible. The server emits a single
  `:`-prefixed **comment frame** whose text is
  `event-log-gap <compact JSON of the same body>` and closes the stream. A
  naive `data:`-only parser ignores the comment and simply sees the stream
  end; a client that understands it can act on `snapshotUrl` without another
  round trip. Closing is what forces a reconnect, and that next request —
  carrying its unchanged cursor — is refused up front with the `400` above,
  so a client converges on the snapshot instead of looping on a partial log.

**Recommended client loop** (what `web/src/api/client.ts` does):

1. `POST /api/compile` → `compileId`.
2. Record `compileId` (and later the last seen `id`) so a reload can resume.
3. `fetch` the events endpoint with `Accept: text/event-stream`. On a resume,
   also send `Last-Event-ID`.
4. Track the last seen `id` from every frame.
5. Stop when an `event: done` or `event: error` frame arrives, or when a
   `phase` frame with `status: "failed"` arrives.
6. If the transport ends **without** a terminal frame, reconnect with
   `Last-Event-ID` and exponential backoff.
7. On `400` with `gap: true`, or on `404`, stop reconnecting and read
   `GET /api/compile/:compileId` instead.

**A client may ignore the in-band gap comment entirely**, and the shipped one
does: `parseFrame` discards any frame with no `data:` field, so the
`: event-log-gap ...` comment is invisible to it. That is sufficient, because
the comment frame is followed by the stream closing, which forces a reconnect,
and that reconnect carries the unchanged cursor and is refused up front with
the `400` whose body names `snapshotUrl`. Handling the comment directly saves
one round trip; not handling it still converges correctly. What a client must
**not** do is treat the closed stream as a transport blip and reconnect with
backoff as if nothing had happened — step 7 is what makes the convergence
happen.

> **Do not use `EventSource`.** It cannot set a `Last-Event-ID` request header
> on its *initial* connection — browsers only resend it on their own internal
> reconnect — so the cursor above cannot be supplied on the first request of
> a resumed session. Use `fetch` plus a `ReadableStream` reader. This is a
> correctness requirement, not a style preference.

**Client disconnect never affects the job.** A vanished subscriber is simply
unsubscribed; the job continues and its log stays retained for a later
`Last-Event-ID` reconnect. Each subscriber has its own unbounded queue, so a
slow or dead consumer can never block the pipeline.

## Appendix: the graph document

The schema is defined once in `src/swarm_builder/models.py` and the
frontend's TypeScript is generated from this server's OpenAPI document. The
field-level rationale — including the naming rule, `extra="forbid"`, and the
two authoritative lookup tables — is in
[`architecture.md`](./architecture.md#the-graph-document); this appendix is
the wire shape only.

`GET /api/graphs/:id` returns, and `PUT /api/graphs/:id` accepts:

```json
{
  "version": 1,
  "id": "linear-chat",
  "name": "Linear chat",
  "entryNodeId": "intake",
  "exitNodeId": "summarize",
  "stateFields": [{ "name": "topic", "type": "str", "default": "\"\"", "description": null }],
  "nodes": [
    {
      "id": "intake",
      "kind": "programmatic",
      "title": "Intake",
      "intent": "Normalize the raw input topic string.",
      "position": { "x": 0, "y": 0 },
      "template": null,
      "io": { "inputType": "str", "outputType": "str" },
      "reads": [],
      "writes": ["topic"],
      "agent": null,
      "programmatic": { "needs": [], "signatureHint": "strip whitespace" },
      "decision": null,
      "join": null
    }
  ],
  "edges": [{ "kind": "seq", "id": "e1", "source": "intake", "target": "chat_step", "label": null }],
  "model": null,
  "updatedAt": "2024-01-01T00:00:00Z"
}
```

Literals:

| Field | Values |
|---|---|
| `version` | `1` |
| `nodes[].kind` | `agent`, `programmatic`, `decision`, `join` |
| `nodes[].template` | `chat`, `orchestrator`, `websearch`, or `null` (inferred from `intent`) |
| `io.inputType` / `io.outputType`, `stateFields[].type` | `str`, `json`, `list[str]` |
| `nodes[].join.reducer` | `list_append`, `list_extend`, `dict_update`, `sum` |
| `nodes[].join.initialFactory` | `list`, `dict`, `int`, or `null` (derived from `reducer`) |
| `edges[].kind` | `seq`, `branch`, `fanout`, `join`, `delegate` |

Per-kind spec objects (exactly one is non-null, matching `kind`; Phase 1
enforces the correspondence):

| `kind` | Spec fields |
|---|---|
| `agent` | `instructions`, `tools`, `delegatesTo`, `outputSchema` (a JSON Schema object) |
| `programmatic` | `needs` (PyPI requirement strings, unioned into the generated `pyproject.toml`), `signatureHint` (free text guidance for the fill agent — never executed or parsed) |
| `decision` | `branches: [{match, targetNodeId}]`, `note` |
| `join` | `reducer`, `initialFactory` |

Edge kinds and their extra fields:

| `kind` | Extra fields | Emitted as |
|---|---|---|
| `seq` | `label` (optional) | `add_edge` |
| `branch` | `match` | the decision's `.branch(...)` chain |
| `fanout` | `joinNodeId` | a source step with multiple successors, all converging on a join |
| `join` | — | the join node's inbound arm |
| `delegate` | — | **no edge at all** — the child is called as an agent tool, so an edge would execute it twice |

`model` is a graph-level override, absent meaning "inherit the resolved
default":

```json
{ "provider": "amazon-bedrock", "model": "us.anthropic.claude-opus-5", "reasoningEffort": null }
```

`updatedAt` is overwritten by the server on every `PUT`; whatever the client
sends is ignored.
