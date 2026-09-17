# Group 6 — Frontend — Implementation Plan (v2, post-review)

> Sub-plan for `PLAN.md`'s Group 6, written by the Group-6 subagent. No
> human interviewer was reachable in this delegated session
> (`ask_user_question` errored: "human interaction is unavailable while
> the calling agent is owned by another live agent"), so every open
> question below is resolved with a documented decision + rationale
> instead of being asked, and is re-reported at the end of the work for
> the delegating agent / developer to override if wrong. This mirrors
> the precedent set by Group 2's `GROUP2_PLAN.md` decision 8 and Group
> 3's `GROUP3_PLAN.md` preamble.
>
> **v2 note.** A `planReview` subagent read `PLAN.md`, `models.py`,
> `web/src/types.ts`, and every `routes/*.py` file and found 16 blockers
> against v1 of this plan (type-plumbing errors, referential-integrity
> gaps, a broken SSE frame-parsing spec, and one wrong id-stability
> decision). All are folded in below; each fix is marked `[R:<id>]`
> referencing the review finding it addresses.

## Goal

Ship `web/`: `package.json`, `vite.config.ts`, `index.html`, and
`src/{main.tsx,App.tsx,canvas/{Canvas.tsx,nodes/*.tsx,edges/*.tsx,
theme.css},panels/{Inspector,Palette,CompilePanel}.tsx,
state/graphStore.ts,api/client.ts,infer.ts}`, plus Vitest smoke tests
(store round-trip, canvas render). Build against the documented HTTP
API contract; `/api/compile*` may still 501 (Group 4 concurrent) —
degrade gracefully, never touch `src/swarm_builder/`.

## Decisions (resolved without a human interview)

1. **Top-level app shell / graph picker.** `App.tsx` renders a minimal
   graph list screen first (`GET /api/graphs`), rendering both
   `graphs[]` **and `errors[]`** `[R:D9]` (a corrupted file is shown by
   id + detail, never silently dropped), with "New graph" and per-row
   "Open" / "Delete" (the delete UI includes the `?project=1` choice,
   `[R:D9]`) actions. Selecting a graph switches to the three-pane
   workspace (Palette | Canvas | Inspector-or-StateFields, with
   CompilePanel as a drawer). This is **component state, never a URL
   route** `[R:A16]`, since `main.py` mounts `StaticFiles(html=True)`
   with no SPA-fallback rewrite — any real route other than `/` 404s on
   a hard reload. **Flagged for override.**

2. **Where `stateFields` are authored.** A `StateFieldsPanel` occupies
   the Inspector's slot when no node is selected, **and is also
   reachable as a pinned top tab of the Inspector while a node *is*
   selected** `[R:C4]` (a disclosure/tab, not a full deselect), so
   adding a field while wiring a node's `reads`/`writes` needs no
   navigation round-trip. `StateField.default` is documented as
   *literal Python source*, not a plain value (`'""'`, `"0"`, not `""`,
   `0`) — the editor exposes a type-driven helper (for `str`: wraps
   user text in `repr()`-equivalent quoting; for `list[str]`: `"[]"` or
   a literal-list builder; for `json`: raw literal textarea with a
   `Default is literal Python source, e.g. '{}' or "0"` hint) rather
   than accepting an unannotated free-text field `[R:C4]`.

3. **Node id assignment — corrected.** `[R:C1, blocker]` **`id` is
   assigned exactly once, at node creation, from the initial title via
   the slug algorithm below, and never changes again for the node's
   lifetime.** Renaming `title` thereafter is pure free text with zero
   effect on `id` — this matches `models.py`'s own "stable across
   edits" / PLAN.md's "stable across recompiles" language literally,
   which v1 of this plan contradicted. An explicit, separate "Change
   node id…" action may exist as a stretch goal, but only if it
   performs a full atomic rewrite of every reference class in one
   store transaction: `entryNodeId`, `exitNodeId`, every edge's
   `source`/`target`, every `DecisionBranch.targetNodeId`, every
   `FanoutEdge.joinNodeId`, every `agent.delegatesTo` entry. **Not
   required for v1** — it is explicitly out of scope; only automatic
   creation-time assignment ships.
   The slugification algorithm mirrors `slugify.py` exactly (NFKD-fold
   to ASCII, lowercase, collapse non-`[a-z0-9]+` to `_`, `step_<index>`
   fallback, leading-digit underscore) and the keyword blocklist is
   **the complete `keyword.kwlist + keyword.softkwlist` for the pinned
   server interpreter** (captured as a literal array with a comment
   naming the exact Python version at implementation time), not a
   "small hardcoded" subset `[R:C2, blocker]` — `review.py`'s
   `_check_node_ids` re-checks with the identical two stdlib functions
   and hard-errors on a mismatch, so an incomplete list produces
   documents that pass client-side but fail server review.

4. **Test stack.** Vitest + `@testing-library/react` + `jsdom`, pinned
   to exact resolved versions at implementation time (not "current
   majors" — `[R:C5]`: this project's stated discipline is exact
   pinning, and dev-only is not an exemption; the resolved versions are
   recorded in the final report and in `package.json`).

5. **SSE client — corrected wire format and reconnect ownership.**
   `EventSource` cannot set `Last-Event-ID` on the initial connect, so
   a hand-rolled `fetch` + `ReadableStream` client is still the right
   call `[R: reviewer agreed, section B]`, but v1's parsing spec was
   wrong against the real `sse-starlette` 3.4.11 wire format. Corrected
   contract (see "`api/client.ts`" below for the full spec):
   - frame separator is `\r\n\r\n` (sse-starlette's `DEFAULT_SEPARATOR`
     is `\r\n`), not `\n\n` — parser splits on
     `/\r\n\r\n|\n\n|\r\r/` `[R:B1, blocker]`;
   - comment frames (`: ping - <ts>`, emitted every 15s) are discarded;
     any frame with no `data` field is never dispatched `[R:B2]`;
   - multi-line `data:` fields are rejoined with `\n`; exactly one
     leading space after `:` is stripped `[R:B3]`;
   - the tracked "last event id" is the id of the most recently
     *received* frame, never a numeric/lexicographic max `[R:B4]`;
   - **this module owns reconnection explicitly**: on a stream error
     (not a clean `done`/`error` app-level frame, not an explicit
     cancel) it retries with capped exponential backoff, resuming with
     `Last-Event-ID` set to the last frame id seen; it never
     reconnects after a `done`, an app-level `error` frame, or a caller
     `cancel()` `[R:B5, blocker]`;
   - `compileId` + last-seen-event-id are persisted to
     `sessionStorage` (keyed by graph id) on every received frame, so a
     hard reload mid-compile can resume — `App`/`CompilePanel` check
     this on mount and, if present, call `getCompileSnapshot(compileId)`
     first (satisfying the documented "fresh tab, no Last-Event-ID"
     path) then subscribe with the persisted id `[R:B6, blocker]`;
   - before touching `response.body`, the client checks `response.ok`
     and a `text/event-stream` content-type, since the events endpoint
     can (and today does, via `[R: compile.py]`) return flat JSON
     instead of a stream; the returned unsubscribe function owns an
     `AbortController` and is idempotent `[R:B7]`.

6. **Compile-panel behavior while `/api/compile*` 501s.** Detected via
   the documented flat body's `notYetWired: true` field (not status
   code alone — `compile.py`'s own docstring calls this out as being
   for exactly this client's benefit), surfaced as a distinct notice,
   no SSE attempt made.

7. **Model picker — corrected "source" semantics.** `[R:A13, blocker]`
   `resolve_effective_model` is called with **no** `graph_override`
   argument by both `/api/health` and `/api/models` (confirmed by
   reading `health.py`/`llm_routes.py`), so the server-reported
   `source` can only ever be `settings-default` / `env-fallback` /
   `bundle-default` — **never** `graph-override`. The frontend
   therefore computes the fourth state itself: when the *loaded
   graph's own* `model` field is non-null, the picker shows "Overridden
   by this graph" (naming the graph's `provider`/`model`) as the
   effective selection, with the server's `resolvedDefault` (**and its
   real `source`**) shown alongside as "would otherwise inherit: ...".
   When `graph.model` is null, the effective selection **is** the
   server's `resolvedDefault`, shown with its real source label. This
   is the literal, corrected version of I5's four-state requirement.
   The picker also surfaces `RouteOut.unmappableReason` (grayed out /
   annotated, not selectable) and top-level `settingsError` when
   present `[R:A14]`, and refuses to write `graph.model` with an empty
   `model` string `[R:D12]`.

8. **Autosave vs. explicit Save — corrected with recovery.** No manual
   Save button, but paired with: (a) a bounded retry (3 attempts,
   exponential backoff) on a failed autosave PUT before surfacing "Save
   failed: <detail>" with a manual "Retry" action `[R:A9, C7]`; (b) a
   synchronous flush of any pending debounced save on
   `visibilitychange`/`beforeunload` (best-effort `fetch(..., {keepalive:
   true})`) so PLAN.md acceptance-3's "reload restores the graph
   exactly" holds even for an edit inside the 800ms window `[R:A9]`.

## Corrected type plumbing `[R:A1, A2 — both blockers]`

`web/src/types.ts` exports only `paths`, `components`, `operations` —
**no** `SwarmGraph`, `SwarmNode`, `SwarmEdge`, `NodeKind`, `PortType`,
`TemplateId`, or `ReducerId` type alias exists; several of those are
inlined enums on a parent schema, not top-level schemas at all. A new
file centralizes every derived alias so nothing else hand-redeclares a
shape:

```ts
// web/src/api/schema.ts — the ONLY file that reaches into
// components["schemas"] by string key; everything else imports from here.
import type { components } from '../types';

export type SwarmGraph = components['schemas']['SwarmGraph'];
export type SwarmNode = components['schemas']['SwarmNode'];
export type Position = components['schemas']['Position'];
export type NodeIo = components['schemas']['NodeIo'];
export type StateField = components['schemas']['StateField'];
export type AgentSpec = components['schemas']['AgentSpec'];
export type ProgrammaticSpec = components['schemas']['ProgrammaticSpec'];
export type DecisionSpec = components['schemas']['DecisionSpec'];
export type DecisionBranch = components['schemas']['DecisionBranch'];
export type JoinSpec = components['schemas']['JoinSpec'];
export type ModelSelection = components['schemas']['ModelSelection'];
export type SeqEdge = components['schemas']['SeqEdge'];
export type BranchEdge = components['schemas']['BranchEdge'];
export type FanoutEdge = components['schemas']['FanoutEdge'];
export type JoinEdge = components['schemas']['JoinEdge'];
export type DelegateEdge = components['schemas']['DelegateEdge'];
export type SwarmEdge = SeqEdge | BranchEdge | FanoutEdge | JoinEdge | DelegateEdge;

export type NodeKind = SwarmNode['kind'];               // 'agent'|'programmatic'|'decision'|'join'
export type TemplateId = NonNullable<SwarmNode['template']>;
export type PortType = NodeIo['inputType'];              // 'str'|'json'|'list[str]'
export type ReducerId = JoinSpec['reducer'];

export type HealthResponse = components['schemas']['HealthResponse'];
export type ModelsResponse = components['schemas']['ModelsResponse'];
export type RouteOut = components['schemas']['RouteOut'];
export type ResolvedDefaultOut = components['schemas']['ResolvedDefaultOut'];
export type ReviewResponse = components['schemas']['ReviewResponse'];
export type FindingOut = components['schemas']['FindingOut'];
export type GraphListResponse = components['schemas']['GraphListResponse'];
export type GraphSummary = components['schemas']['GraphSummary'];
export type GraphListError = components['schemas']['GraphListError'];
export type DeleteGraphResponse = components['schemas']['DeleteGraphResponse'];
export type ExportResponse = components['schemas']['ExportResponse'];
export type TemplateEntryOut = components['schemas']['TemplateEntryOut'];
```

**Provisional, hand-written compile types** `[R:A2]` — `routes/
compile.py` declares `response_model=None` on every handler (Group 4
has not landed), so `types.ts` has no success shapes for
`/api/compile*` at all. These are isolated in one file, clearly marked
provisional, and must be regenerated/deleted the moment Group 4 lands
real response models:

```ts
// web/src/api/compileTypesProvisional.ts
// PROVISIONAL — Group 4 (compile pipeline) has not landed real response
// models yet (routes/compile.py declares response_model=None everywhere).
// Delete this file and re-derive from schema.ts the moment Group 4 ships
// real Pydantic response models and regenerates web/src/types.ts.
export interface StartCompileResponse { compileId: string }
export interface CompileSnapshot {
  compileId: string;
  status: 'running' | 'succeeded' | 'failed' | 'cancelled';
  phases: { name: string; status: 'pending' | 'running' | 'done' | 'failed' }[];
  logTail: string[];
  warnings: string[];
  result: { projectPath: string; renderedDiagram?: string } | null;
  error: string | null;
}
export type CompileSseEventType = 'phase' | 'log' | 'warning' | 'done' | 'error';
export interface CompileSseEvent {
  id: string;
  type: CompileSseEventType;
  data: unknown; // shape depends on `type`; not yet documented by Group 4
}
```

**The rendered diagram has no documented source today** `[R:A3,
blocker]` — `ExportResponse` is exactly `{projectPath, runCommand}`;
nothing in the current HTTP API returns `graph.render()` text. The
result section therefore renders the diagram pane **only when** a
`done` event's payload (or the snapshot's `result`) actually includes
a `renderedDiagram` string (an optional field in the provisional type
above); when absent, that pane is omitted entirely rather than shown
empty. This is an explicit, flagged dependency on Group 4's real event
payload shape.

## Repo layout produced

```
web/
  package.json
  vite.config.ts          (includes `server.proxy` for /api -> the FastAPI port, and a `test` block for Vitest so config never drifts [R:A16, E9])
  index.html
  tsconfig.json
  src/
    main.tsx
    App.tsx
    App.css
    types.ts               (pre-existing, generated — untouched by hand)
    openapi.json            (pre-existing, generated — untouched by hand)
    infer.ts
    infer.test.ts
    slug.ts                 (client-side slugify, decision 3)
    slug.test.ts
    api/
      schema.ts              (the ONLY components["schemas"][...] indirection point)
      compileTypesProvisional.ts
      client.ts
      client.test.ts
    state/
      graphStore.ts
      graphStore.test.ts
    canvas/
      Canvas.tsx
      theme.css
      nodes/
        AgentNode.tsx
        ProgrammaticNode.tsx
        DecisionNode.tsx
        JoinNode.tsx
      edges/
        SeqEdge.tsx
        BranchEdge.tsx
        FanoutEdge.tsx
        JoinEdge.tsx
        DelegateEdge.tsx
        index.ts
    panels/
      GraphPicker.tsx
      Palette.tsx
      Inspector.tsx
      StateFieldsPanel.tsx
      CompilePanel.tsx
  test/
    setup.ts                 (jsdom stubs: ResizeObserver, DOMMatrixReadOnly [R:C6])
    fixtures.ts              (a review-clean fixture AND a render-only fixture, kept separate [R:E3])
```

## `state/graphStore.ts` — zustand store (single source of truth)

```ts
interface GraphStoreState {
  graph: SwarmGraph | null;
  savedRevision: number;              // [R:A8] bumped on every mutation; applySaved only clears dirty if unchanged since the save it answers
  mutationRevision: number;           // [R:A8]
  selectedNodeId: string | null;
  selectedNodeIds: string[];          // [R:A4] multi-highlight for findings naming >1 node; selectedNodeId is selectedNodeIds[0]
  selectedEdgeId: string | null;      // [R:D1]
  dirty: boolean;
  saveStatus: 'idle' | 'saving' | 'saved' | 'error';
  saveError: string | null;
  saveRetryCount: number;             // [R:A9]
  compile: CompileState;

  loadGraph(graph: SwarmGraph): void;
  newGraph(name: string): SwarmGraph;   // [R:A10] builds the minimum valid document below
  selectNode(id: string | null): void;
  selectNodes(ids: string[]): void;      // [R:A4]
  selectEdge(id: string | null): void;   // [R:D1]

  addNode(kind: NodeKind, title: string, position: Position): string;  // returns the assigned id [R:C1]
  updateNode(id: string, patch: Partial<SwarmNode>): void;   // title changes never touch id [R:C1]
  removeNode(id: string): void;         // full cascade, see below [R:D6]

  addEdge(kind: SwarmEdge['kind'], source: string, target: string, extra?: EdgeExtra): string;  // [R:D3] extra: {match}|{joinNodeId}|{label}
  updateEdge(id: string, patch: Partial<SwarmEdge>): void;   // [R:D1]
  removeEdge(id: string): void;         // syncs DecisionSpec/joinNodeId refs [R:D2,D6]

  setStateFields(fields: StateField[]): void;
  setModelOverride(sel: ModelSelection | null): void;
  setEntryNodeId(id: string): void;      // [R:D5]
  setExitNodeId(id: string): void;       // [R:D5]

  addDecisionBranch(decisionNodeId: string, match: string, targetNodeId: string): void;  // [R:D2] creates the paired BranchEdge transactionally
  removeDecisionBranch(decisionNodeId: string, match: string): void;                       // [R:D2] removes the paired BranchEdge too
  setDelegatesTo(orchestratorNodeId: string, targetNodeIds: string[]): void;                // [R:D4] diffs and creates/removes DelegateEdges

  // internal, called only by the autosave effect wired from App.tsx
  markDirty(): void;
  applySaved(updatedAt: string, forRevision: number): void;   // [R:A8] merges only updatedAt when forRevision === savedRevision
}
```

**`newGraph` invariants** `[R:A10]` — `SwarmGraph` requires `version:
1`, `id`, `name`, `entryNodeId`, `exitNodeId`, `updatedAt`, and
`_check_structural_integrity` requires entry/exit to name real nodes,
so a zero-node graph is unrepresentable. `newGraph` therefore always
creates exactly one `agent` node (title "Start", default spec below),
sets it as both `entryNodeId` and `exitNodeId`, and assigns the graph's
own `id` via `crypto.randomUUID()` (matching `store/_ids.py`'s
`^[A-Za-z0-9_-]{1,128}$`, which a name-derived slug is not guaranteed
to satisfy).

**Kind-appropriate default specs** `[R:A11]` — `AgentSpec.instructions`
and `JoinSpec.reducer` are *required* fields with no valid empty form:

| kind | default spec | other three specs |
|---|---|---|
| `agent` | `{instructions: '', tools: [], delegatesTo: []}` | `null` |
| `programmatic` | `{needs: [], signatureHint: null}` | `null` |
| `decision` | `{branches: [], note: null}` | `null` |
| `join` | `{reducer: 'list_append', initialFactory: null}` | `null` |

**`removeNode(id)` cascade** `[R:D6, blocker]`, one atomic store
transaction:
1. Remove every edge whose `source` or `target` is `id`.
2. Remove every `DecisionBranch` (on any node) whose `targetNodeId ===
   id`, and remove its paired `BranchEdge` too (idempotent with 1).
3. Remove `id` from every `agent.delegatesTo` array, and remove any
   `DelegateEdge` naming it (idempotent with 1).
4. Clear any `FanoutEdge.joinNodeId === id` reference — removing the
   fanout edge entirely, since a fanout with no join target is
   meaningless (matches `review.py`'s own fan-out-without-join error).
5. If `entryNodeId === id` or `exitNodeId === id`, reassign to the
   first remaining node (arbitrary but deterministic — first by array
   order) if any nodes remain, else block the deletion with a "cannot
   delete the only node" UI message (a graph cannot exist with zero
   nodes, matching `newGraph`'s own invariant).

**`removeEdge(id)`** applies the same branch/fanout sync as steps 2 and
4 above when the removed edge is a `branch`/`fanout` edge `[R:D2, D6]`.

**`addEdge` and edge-kind selection** `[R:D3, blocker]` — React Flow's
`onConnect` only yields `{source, target}`. The Canvas therefore shows
a small post-connect popover (anchored at the connection's midpoint)
listing the valid edge kinds for that source/target pair:
- `seq` (default when the source has no existing outgoing edge yet, or
  when the source is a `join`/non-decision node),
- `branch` (only offered when the source is a `decision` node — the
  popover asks for the `match` string right there, and calls
  `addDecisionBranch` so the spec and edge are created together in one
  action, never two),
- `delegate` (only offered when the source is an `agent` node — calls
  `setDelegatesTo` under the hood so `agent.delegatesTo` and the edge
  stay paired),
- drawing a **second** plain successor from a node that already has
  one triggers a distinct flow instead of silently creating an
  unreviewable pair: the popover requires picking or creating a `join`
  node, and creates **both** arms as `fanout` edges with the chosen
  `joinNodeId`, plus the necessary `join`-kind edges from each arm's
  target into the join node — matching `review.py`'s
  `fanout_without_join` check exactly, which explicitly flags "two
  plain seq edges" as the failure this prevents.
Edge ids are `crypto.randomUUID()`.

**React Flow is a projection — corrected write-back rule** `[R:A6,
A7, both blockers]`. `toFlowNodes(graph)`/`toFlowEdges(graph)` are pure
`useMemo`-derived mappers (memoized on the `nodes`/`edges` array
identity, `[R:E6]`), never stored separately. `onNodesChange` receives
`select`/`dimensions`/`remove`/`replace`/`position` changes through one
handler — the store's handler:
- ignores `select` and `dimensions` changes entirely (a click or an
  RF-internal remeasure must never mark the graph dirty or write
  anything back);
- on a `position` change with `dragging === false` (i.e. drag just
  ended), writes `{x, y}` into that node's `position` **only**, via a
  narrow `updateNodePosition(id, pos)` action that touches no other
  key — never assigning a whole RF node object, which would violate
  `extra="forbid"` by carrying RF's own `selected`/`measured`/`data`
  keys `[R:A6]`;
- routes a `remove` change to the semantic `removeNode` action so the
  full cascade above runs, never a bare array splice.

**Autosave payload contract** `[R:A6]` — the `PUT` body is constructed
by a `toSavePayload(graph): SwarmGraph` selector that copies **only**
the schema's own keys (spread-and-pick, never a raw object spread of
anything RF has touched), so it is impossible for an RF-internal key to
leak into the wire payload even if some future code accidentally wrote
one into `graph`.

**Palette drop coordinates** `[R:E7]` — `Palette.tsx`'s drop handler
converts the browser drop event's client coordinates to flow
coordinates via the React Flow instance's `screenToFlowPosition` before
calling `addNode`, since `Position` is in flow space, not screen space.

## `api/client.ts` — typed fetch + corrected SSE

```ts
export const api = {
  health(): Promise<HealthResponse>;
  listGraphs(): Promise<GraphListResponse>;
  getGraph(id: string): Promise<SwarmGraph>;
  putGraph(id: string, graph: SwarmGraph): Promise<SwarmGraph>;
  deleteGraph(id: string, opts?: { project?: boolean }): Promise<DeleteGraphResponse>;
  listTemplates(): Promise<TemplateEntryOut[]>;   // may 503 -> TemplatesUnavailableError [R:A15]
  getModels(): Promise<ModelsResponse>;
  reviewGraph(id: string): Promise<ReviewResponse>;  // may 404 (unsaved graph), 422, 503 -> distinct typed errors [R:A5]
  exportGraph(id: string): Promise<ExportResponse>;  // 404 -> NotCompiledError

  startCompile(graphId: string): Promise<StartCompileResponse>;  // 409 -> CompileConflictError; notYetWired:true body -> NotYetWiredError [decision 6]
  getCompileSnapshot(compileId: string): Promise<CompileSnapshot>;
  cancelCompile(compileId: string): Promise<void>;
  subscribeCompileEvents(compileId: string, opts: {
    lastEventId?: string;
    onEvent: (evt: CompileSseEvent) => void;
    onError: (err: Error) => void;
  }): () => void;
};
```

- Every success/error shape not covered by `compileTypesProvisional.ts`
  comes from `api/schema.ts` — no shape is redeclared a second time
  anywhere in this file.
- `ApiError` carries `status` and the parsed `detail`
  (`string | ValidationError[]`), matching every route's
  `HTTPException` convention (confirmed by reading every `routes/*.py`
  file).
- **`reviewGraph`** callers must sequence the call *after* the
  in-flight autosave PUT resolves — the CompilePanel never calls it on
  a bare timer against a possibly-unsaved in-memory document `[R:A5]`;
  see "CompilePanel contract" item 1 below.
- **SSE frame parser** (`subscribeCompileEvents`'s internals), matching
  `sse-starlette` 3.4.11's real encoder:
  1. Buffer incoming chunks; split completed frames on
     `/\r\n\r\n|\n\n|\r\r/` (WHATWG event-stream frame boundary,
     `[R:B1]`).
  2. Within a frame, split lines on `/\r\n|\n|\r/`; discard any line
     starting with `:` (comment/ping frames, `[R:B2]`); for each
     remaining `field: value` line, strip exactly one leading space
     after the colon; accumulate multiple `data:` lines by joining
     with `\n` (`[R:B3]`); a line with no colon is a field with an
     empty value.
  3. A frame with no accumulated `data` is discarded without invoking
     `onEvent` (covers ping frames whose `:` comment line is the only
     content, `[R:B2]`).
  4. `onEvent({id, type: event ?? 'message', data: JSON.parse(data)})`;
     the module's own internal "last id" tracker is set to this frame's
     `id` unconditionally (last-received, never max, `[R:B4]`), and is
     what the next (re)connect attempt sends as `Last-Event-ID`.
  5. On a genuine transport error (not a `done`/`error` **application**
     frame, not caller `cancel()`), the module itself reconnects with
     capped exponential backoff (e.g. 500ms → 8s, 5 attempts) using the
     internally-tracked last id, and gives up loudly (`onError`) after
     the cap `[R:B5]`.
  6. On every received frame, the graph-scoped `sessionStorage` entry
     `swarm-builder:compile:<graphId>` is updated to `{compileId,
     lastEventId}` so a hard reload can resume `[R:B6]`; cleared on a
     `done`/`error` frame.
  7. Before reading `response.body`, checks `response.ok` and that
     `content-type` starts with `text/event-stream`; a non-matching
     response (e.g. today's flat 501 JSON) is surfaced through the
     normal `startCompile`-style error path, never fed to the frame
     parser `[R:B7]`. The returned closer function aborts an
     `AbortController` and is safe to call more than once.

## `infer.ts` — pure client-side template inference (corrected)

`[R:C3, blocker]` — v1 of this plan stated the tie rule as "ties → 
chat", which is **not** what `registry.py::infer_template` does: it
iterates `_KEYWORDS` in insertion order (`websearch` then
`orchestrator`) with a strict `>` comparison, so a genuine 1-1 tie
resolves to **whichever template is checked first with a nonzero
count** — verified server-side: `"delegate to search"` → `websearch`,
`"Route the news"` → `websearch`. **Only a 0-0 tie (no keyword from
either set matches) returns `chat`.** The TypeScript port preserves
this exactly:

```ts
const KEYWORDS: readonly [TemplateId, readonly string[]][] = [
  ['websearch', ['search', 'browse', 'news', 'latest']],
  ['orchestrator', ['delegate', 'coordinate', 'route', 'sub-agent', 'plan and assign']],
];  // order is load-bearing — matches registry.py's dict insertion order

export function inferTemplate(intent: string): { suggestion: TemplateId; matchedKeywords: string[] } {
  const lowered = intent.toLowerCase();
  let best: TemplateId = 'chat';
  let bestCount = 0;
  let bestMatched: string[] = [];
  for (const [template, keywords] of KEYWORDS) {
    const matched = keywords.filter((kw) => lowered.includes(kw));
    if (matched.length > bestCount) {
      bestCount = matched.length;
      best = template;
      bestMatched = matched;  // set-order, matching registry.py's tuple(kw for kw in keywords if ...)
    }
  }
  return { suggestion: best, matchedKeywords: bestCount === 0 ? [] : bestMatched };
}
```

Parity tests mirror **every** case in `tests/test_template_registry.py`
verbatim, including `test_higher_keyword_count_wins_over_first_match`,
plus one genuine 1-1-tie fixture (`"delegate to search"` →
`websearch`) proving the corrected rule, not the wrong one `[R:C3]`.

## `slug.ts` — client-side node-id slugification

Mirrors `slugify.py`'s algorithm exactly (see decision 3): NFKD-fold to
ASCII, lowercase, collapse non-`[a-z0-9]+` runs to `_`, trim, empty →
`step_<index>`, leading digit → `_`-prefixed, keyword collision → 
trailing `_`, using the **complete** `keyword.kwlist +
keyword.softkwlist` literal (decision 3, `[R:C2]`). `slugifyTitles`
dedups with the same numeric-suffix search `slugify.py` uses.
Test cases mirror `tests/test_slugify.py` exactly.

## Canvas: node/edge visual contract

- **Node badges**: `agent` (solid border, "AGENT" badge), `programmatic`
  ("FN" badge, dashed-look border), `decision` (rotated diamond-ish
  badge, "IF" + branch count), `join` (double border, "JOIN" +
  reducer name). Title + truncated `intent` preview on every node.
  Entry/exit nodes carry a small "START"/"END" corner tag `[R:D5]`.
- Edges via custom `@xyflow/react` `edgeTypes`: `seq` solid (+ optional
  `label`); `branch` solid, **always labeled with `match`**; `fanout`/
  `join` solid, sharing a distinguishing stroke color and a `→
  join:<id>` label rather than a custom curved path toward the join
  node's coordinates — the curved-path approach is deferred as a
  stretch goal, not a contract item, since it requires either
  prop-drilling coordinates into the edge renderer or a store read
  inside the renderer, both adding re-render cost with no functional
  requirement beyond "drawn toward" `[R:E5]`; `delegate` **dashed**,
  muted color, small tool-call glyph.
- Edge selection sets `selectedEdgeId` `[R:D1]`; a lightweight edge
  panel (in the Inspector's slot when an edge, not a node, is selected)
  edits `SeqEdge.label`, `BranchEdge.match` (kept in sync with the
  decision node's spec via the same `addDecisionBranch`-style
  transactional update), and `FanoutEdge.joinNodeId`.
- **Hand-drawn theme (`theme.css`)**: a stable-per-node `rotate()`
  jitter (1–2°, hashed from `node.id`) applied to an **inner
  presentational wrapper `div`, never `.react-flow__node` itself**
  `[R:E4, blocker]` — rotating the outer RF-measured element
  desynchronizes handle positions from connection hit-testing.
  Irregular `border-radius`, a web-safe handwriting-ish font stack
  (`"Comic Sans MS", "Segoe Print", cursive` — no network font fetch,
  keeping the app offline-friendly per PLAN.md Assumption 8), and a
  dotted radial-gradient background on the canvas pane. All CSS-only;
  no visual state persisted in the graph document.

## Inspector contract (per selected node)

1. `title` (free text; never touches `id`, decision 3 `[R:C1]`).
2. `intent` (multiline, **primary field**).
3. Template selector (`kind === 'agent'` only): 3-way `<select>` +
   live inferred-suggestion hint from `infer.ts`.
4. `io.inputType` / `io.outputType` selects.
5. `reads`/`writes` multi-selects sourced from `graph.stateFields`.
6. Tool toggles (`kind === 'agent'`): checklist seeded from the
   template's `defaultTools` (`GET /api/templates`); if that call is
   503 (`TemplatesUnavailableError`, `[R:A15]`), the checklist falls
   back to free-form add/remove only, with an inline notice that the
   catalog is temporarily unavailable.
7. **`agent.instructions`** (`kind === 'agent'` only) `[R:A12,
   blocker]` — a required field the plan omitted entirely. Exposed as
   its own textarea, **pre-filled by mirroring `intent` whenever the
   user has not yet diverged it manually** (a per-node "instructions
   follow intent" toggle, on by default, turned off automatically the
   first time the user edits `instructions` directly) — this matches
   PLAN.md's template contract ("instructions from `intent`") while
   still letting an advanced user write different instructions text.
   `agent.outputSchema` is explicitly **not editable in v1** (a raw
   JSON-schema authoring UI is out of scope) — noted once here, never
   silently left for the implementer to guess `[R:D11]`.
8. `delegatesTo` picker (`kind === 'agent'` only): multi-select of
   other agent node ids/titles, backed by `setDelegatesTo` so the
   paired `DelegateEdge`s are created/removed transactionally
   `[R:D4]`.
9. Programmatic-only: `needs` (tag list) and `signatureHint`.
10. Decision-only: branch editor (`match` text + `targetNodeId`
    select) that calls `addDecisionBranch`/`removeDecisionBranch`
    **exclusively** — never writing `DecisionSpec.branches` directly —
    so the paired `BranchEdge` set can never drift out of sync
    `[R:D2, blocker]`; `note` textarea; and the persistent fact-27
    callout: *"Data does not pass through this node — a branch target
    receives this node's own return value (the match value), not
    whatever flowed into it. If a branch target needs the original
    input, have this node write it to a state field and have the
    branch target read it."*
11. Join-only: `reducer` select (4 values) and `initialFactory` select
    (`list`/`dict`/`int`/"auto" → `null`).
12. **Entry/exit controls** `[R:D5, blocker]` — every node's header
    carries "Set as entry" / "Set as exit" buttons (disabled/checked
    state reflecting `graph.entryNodeId`/`exitNodeId`), since v1 had no
    way at all to author the required, structurally-validated
    `entryNodeId`/`exitNodeId` fields beyond the single-node
    `newGraph` default.

`ModelSelection.reasoningEffort` and `StateField.description` are
explicitly **not editable in v1** (round-tripped losslessly on
save/load, but no UI writes them) — noted once here rather than left
silently unaddressed `[R:D11]`.

## CompilePanel contract

1. **Review section**, corrected sequencing `[R:A5, blocker]`: review
   is **never** fired on a bare timer against the in-memory document.
   The panel tracks the store's `dirty` flag; whenever `dirty` is
   `true` it shows "Review is stale — unsaved changes" **in place of**
   stale findings, and only calls `POST /api/graphs/:id/review` after
   an autosave PUT has resolved successfully (subscribing to the
   store's `saveStatus` transitions, not a fixed interval) or when the
   user opens the panel with `dirty === false`. A 404 (never-saved
   graph) shows "Save the graph before reviewing" instead of a generic
   error; a 503 shows "Review is temporarily unavailable on the
   server." `[R:A15]`. Errors (red) block Compile; warnings (amber) do
   not. Clicking a finding calls `selectNodes(finding.nodeIds)`
   `[R:A4]` (highlighting all named nodes, not just the first).
   **Large-graph warning** `[R:D7, blocker]` — since no server-side
   finding code covers it (confirmed by reading `review.py`'s full
   code list), the panel itself computes and displays "This graph has
   N nodes (>40) — one fill run may exceed a comfortable context" as a
   client-only warning row alongside the server's findings.
2. **Model picker** — see decision 7 (corrected four-state semantics,
   `unmappableReason`/`settingsError` surfaced, empty-model guard).
3. **Compile button**: disabled + `health.blockers` shown verbatim
   when `compileReady: false` (re-fetched fresh before every click,
   never cached). **Recompile notice** `[R:D8]`: if
   `exportGraph(graphId)` already succeeds (a project exists), the
   button area shows "Recompiling regenerates the whole project; v1
   does not merge prior edits" before the user clicks Compile.
4. **Streamed phases + log tail + Cancel**, using the corrected SSE
   client above; on mount, if a persisted `sessionStorage` entry for
   this graph exists, snapshot-then-subscribe as described in decision
   5/`[R:B6]` rather than starting fresh.
5. **Result section**: project path + copyable `uv run` command
   (always available, from `GET /api/graphs/:id/export`); the rendered
   diagram pane is shown **only if** the `done` event/snapshot actually
   carries a `renderedDiagram` string — otherwise omitted, with no
   placeholder implying one should have appeared `[R:A3]`.
6. **501 / not-yet-wired notice** per decision 6, detected via
   `notYetWired: true`.

## Tests (Vitest)

- **`slug.test.ts`**: mirrors `tests/test_slugify.py` exactly (dedup
  suffixing, non-ASCII fallback, keyword collision using the *complete*
  list, leading-digit prefixing) `[R:C2]`.
- **`infer.test.ts`**: mirrors every case in
  `tests/test_template_registry.py`, **plus** a genuine 1-1-tie case
  proving the corrected (not the originally-wrong) tie rule `[R:C3]`.
- **`graphStore.test.ts` (required smoke test #1 — store round-trip)**:
  `newGraph`, `addNode('agent', ...)` twice, `addEdge('seq', ...)`
  connecting them, then assert `toSavePayload(graph)`:
  - has both node ids under `nodes[].id` and one edge with
    `kind: 'seq'`/correct `source`/`target`;
  - **has no extra keys beyond `SwarmGraph`'s own schema keys at every
    level** (a recursive key-set assertion against `api/schema.ts`'s
    types, not just "has both node ids") `[R:A6, blocker]`;
  - every edge endpoint and `entryNodeId`/`exitNodeId` refers to a real
    node id (mirroring, not reimplementing at length, the narrow set
    `SwarmGraph._check_structural_integrity` itself checks)
    `[R:E2 — acknowledged duplication, kept minimal on purpose]`.
  - **Cross-language closing of the gap** `[R:E1]`: the same payload is
    also written to `test/fixtures/round-trip-payload.json` during the
    test run (or checked in as a static fixture regenerated by hand
    when the store's shape changes), and a note in this plan records
    that the Python side *may* add a pytest that
    `SwarmGraph.model_validate`s this exact fixture — flagged for the
    developer as the real cross-boundary gate this Vitest test alone
    cannot provide, since it is out of this group's scope to add a
    pytest under `tests/`.
- **`Canvas.test.tsx` (required smoke test #2 — render)**: `test/
  setup.ts` stubs `ResizeObserver` (a `class` with no-op
  `observe`/`unobserve`/`disconnect`) and `DOMMatrixReadOnly` before any
  test runs `[R:C6, blocker]` — without this, `@xyflow/react` 12 never
  renders node content under jsdom and the "required" smoke test would
  be silently vacuous. Renders `<Canvas />` inside `<ReactFlowProvider>`
  with an explicit pixel size on the wrapper, store pre-loaded from a
  **render-only** fixture (one of each node kind, one of each edge
  kind — not required to be review-clean, and kept in a separate
  fixture from the round-trip test's review-clean one, `[R:E3]`), and
  asserts one DOM node per fixture node via `data-testid`.
- **`api/client.test.ts`**: SSE frame-parsing test using a fake
  `ReadableStream`/`fetch` mock, feeding **CRLF-terminated** frames
  (`\r\n\r\n` separators, matching the real encoder, `[R:B1]`)
  including: a frame split across two chunk boundaries; a ping/comment
  frame that must be silently discarded `[R:B2]`; a multi-line `data:`
  payload that must be rejoined with `\n` `[R:B3]`; and a second
  `subscribeCompileEvents` call with `lastEventId` set, asserting the
  `Last-Event-ID` header is sent.

## Environment / toolchain notes

- `pnpm --dir web install/build/test` run with `npm_config_cache`
  pointed at the project-local `.npm-cache/` (or pnpm's own
  `store-dir`, whichever empirically applies) to route around the
  pre-existing root-owned `~/.npm` cache problem.
- `vite.config.ts` sets `server.proxy['/api']` to the FastAPI dev port
  `[R:A16]`, so the dev server needs no CORS story at all and
  `Last-Event-ID` (not a CORS-safelisted header, `[R:B8]`) never
  triggers a cross-origin preflight; production still works unmodified
  since `main.py` serves `web/dist` same-origin.
- React 19.3.0 + `@xyflow/react` 12.11.6 is smoke-tested first (a
  throwaway one-node render) before any other frontend code is
  written; if it genuinely misbehaves the pin drops to React 18.3 for
  `web/` only, and that fact is reported.
- **Restart caveat** `[R:E8]`: `main.py` checks `web/dist/index.html`
  once per `create_app()` call, so a server started *before*
  `pnpm --dir web build` finishes will not serve the frontend until
  restarted — the integration-verification step always builds first,
  then starts the server, never the reverse order.

## Acceptance mapped to the task's Definition of Done

1. `pnpm --dir web build` succeeds.
2. `pnpm --dir web test` (Vitest) passes: slug, infer-parity (including
   the corrected tie case), store round-trip (including the recursive
   key-set assertion), canvas render (with jsdom stubs actually
   working), SSE frame-parsing (including CRLF + ping + multi-line
   cases).
3. Integration: build web **first**, then start `uv run swarm-builder`
   (or `UV_CACHE_DIR=$PWD/.uv-cache uv run swarm-builder`) bound to
   `127.0.0.1`, `curl` `/`, `/api/health`, `/api/models`, confirm the
   served HTML references the built JS bundle and the API responses
   match the shapes the UI expects, then shut the server down. Real
   command output captured in the final report.

## Known gaps / duplication flagged for the developer

- Decisions 1, 2, and 3's "no id-rewrite action in v1" remain genuine
  plan calls made unilaterally (no human interviewer reachable); each
  is called out again in the final report.
- `infer.ts`/`registry.py` and `slug.ts`/`slugify.py` remain two
  implementations of the same two algorithms, kept in parity only by
  hand-matched tests on both sides — an accepted, documented risk.
  `[R:E1]`'s fixture-export note is the cheapest partial mitigation
  available within this group's scope (frontend-only); a true
  single-source-of-truth fix (server-side codegen of the logic, not
  just the shapes) is out of scope for Group 6.
- The rendered-diagram pane and the exact `phase`/`log`/`warning`/
  `done`/`error` event payload shapes are provisional
  (`compileTypesProvisional.ts`) pending Group 4 — flagged for
  whoever lands Group 4 to regenerate/replace.
- `agent.outputSchema`, `ModelSelection.reasoningEffort`, and
  `StateField.description` are round-tripped but not editable in v1
  — explicitly noted, not silently omitted.
