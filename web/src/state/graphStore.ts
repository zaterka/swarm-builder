// zustand store: the single source of truth for the graph document,
// selection, dirty/save status, and compile status (PLAN.md
// "Frontend" -> "State"). React Flow is a PROJECTION of this store --
// `canvas/Canvas.tsx` derives its `nodes`/`edges` props from `graph`
// via pure selectors and writes back through the narrow actions below,
// never by assigning a whole React-Flow-owned object into `graph`
// (GROUP6_PLAN.md review findings A6/A7).
import { create } from 'zustand';
import type {
  CompileSnapshot,
  DatabaseKind,
  DatabaseStarterMap,
  ModelSelection,
  NodeIo,
  NodeKind,
  NormalizedAgentSpec,
  NormalizedDecisionSpec,
  NormalizedJoinSpec,
  NormalizedNosqlSpec,
  NormalizedProgrammaticSpec,
  NormalizedSqlSpec,
  NormalizedSwarmGraph,
  NormalizedSwarmNode,
  NormalizedVectorSpec,
  Position,
  StateField,
  SwarmEdge,
  SwarmEdgeKind,
  SwarmGraph,
} from '../api/schema';
import {
  buildDatabaseStarterMap,
  isDatabaseKind,
  normalizeGraph,
} from '../api/schema';
import { api } from '../api/client';
import type { CompileSseEvent } from '../api/compileWire';
import type { CompileState } from './compileState';
import {
  applyCompileEvent,
  applyCompileSnapshot,
  applyCompileStreamError,
  initialCompileState,
  mergeCompilePatch,
} from './compileState';
import { assignNodeId } from '../slug';
import type { RunState } from './runState';
import {
  applyRunEvent,
  applyRunSnapshot,
  applyRunStreamError,
  initialRunState,
  mergeRunPatch,
  runStateFromRecord,
} from './runState';
import type { RunRecord } from '../api/schema';

// ---------------------------------------------------------------------------
// Kind-appropriate default specs (GROUP6_PLAN.md review finding A11 --
// AgentSpec.instructions and JoinSpec.reducer are REQUIRED fields with
// no valid empty form; the other three specs must be null, never {}).
// Typed as their Normalized* counterparts (api/schema.ts) since this
// store only ever holds normalized nodes past the loadGraph/newGraph
// boundary.
// ---------------------------------------------------------------------------

function defaultAgentSpec(): NormalizedAgentSpec {
  return { instructions: '', tools: [], delegatesTo: [] };
}
function defaultProgrammaticSpec(): NormalizedProgrammaticSpec {
  return { needs: [], signatureHint: null };
}
function defaultDecisionSpec(): NormalizedDecisionSpec {
  return { branches: [], note: null };
}
function defaultJoinSpec(): NormalizedJoinSpec {
  return { reducer: 'list_append', initialFactory: null };
}
// The three database kinds' empty-but-valid specs: every field the generated
// document declares, at the schema's own default. These are what a database
// node gets when it is created *without* a starter patch -- the operation is
// empty on purpose, because "which query" is the user's declaration and Phase
// 1 reports `db_empty_operation` naming the field until they make it.
function defaultSqlSpec(): NormalizedSqlSpec {
  return { query: '', seedSql: '', write: false, note: null };
}
function defaultNosqlSpec(): NormalizedNosqlSpec {
  return { collection: '', operation: 'find', filter: {}, limit: 20, seed: [], note: null };
}
function defaultVectorSpec(): NormalizedVectorSpec {
  return { collection: '', topK: 4, minScore: 0, seed: [], note: null };
}

function defaultIo() {
  return { inputType: 'str' as const, outputType: 'str' as const };
}

/** The I/O pair a node of `kind` starts with when no starter patch supplies
 * one. A database node's pair is *fixed by its kind* (`str -> list[json]`,
 * models.py) rather than chosen, so the store may know it without asking the
 * server -- and it must, because a node created with no catalog at all still
 * has to be a document Phase 1 accepts. The starter's own pair, when present,
 * overrides this (it is the server's declaration, not this file's copy of it).
 */
function defaultIoForKind(kind: NodeKind): NodeIo {
  if (isDatabaseKind(kind)) return { inputType: 'str', outputType: 'list[json]' };
  return defaultIo();
}

function nowIso(): string {
  return new Date().toISOString();
}

// ---------------------------------------------------------------------------
// Compile sub-state
//
// The shape and every transition live in `state/compileState.ts` -- this store
// only exposes them as actions, so the reload/reconnect paths are testable as
// pure functions against the server's real payloads.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Store shape
// ---------------------------------------------------------------------------

export type EdgeExtra =
  | { kind: 'seq'; label?: string | null }
  | { kind: 'branch'; match: string }
  | { kind: 'fanout'; joinNodeId: string }
  | { kind: 'join' }
  | { kind: 'delegate' };

/** What a new database node is created from: the starter's spec **and** its
 * io. Both, deliberately -- a database node's I/O pair is mandatory
 * (`str -> list[json]`) and the catalog is what declares it, so a patch that
 * carried only the spec could leave behind a node Phase 1 rejects for a
 * reason the user never typed. `kind` makes the pairing checkable: a SQL spec
 * cannot be applied to a noSQL node. */
export type NodeStarterPatch =
  | { kind: 'sql'; spec: NormalizedSqlSpec; io?: NodeIo }
  | { kind: 'nosql'; spec: NormalizedNosqlSpec; io?: NodeIo }
  | { kind: 'vector'; spec: NormalizedVectorSpec; io?: NodeIo };

/**
 * The patch a new node of `kind` is created from, or `undefined` when the
 * cached catalog has no usable entry for it.
 *
 * `undefined` is the palette's disabled state and nothing else: there is
 * deliberately no hand-written fallback starter here, because a second copy
 * of the server's default operation or seed would drift from the file the
 * generated project is rendered from (the failure mode this project already
 * documents for `infer_template`).
 */
export function starterPatch(
  kind: DatabaseKind,
  starters: DatabaseStarterMap | null,
): NodeStarterPatch | undefined {
  switch (kind) {
    case 'sql': {
      const entry = starters?.sql;
      return entry ? { kind: 'sql', spec: entry.spec, io: entry.io } : undefined;
    }
    case 'nosql': {
      const entry = starters?.nosql;
      return entry ? { kind: 'nosql', spec: entry.spec, io: entry.io } : undefined;
    }
    case 'vector': {
      const entry = starters?.vector;
      return entry ? { kind: 'vector', spec: entry.spec, io: entry.io } : undefined;
    }
  }
}

export interface GraphStoreState {
  // Normalized, not the raw wire type (see `normalizeGraph` in
  // `api/schema.ts`): every default-backed array field
  // (`nodes`/`edges`/`stateFields` and their nested spec arrays) is
  // guaranteed present here, never `undefined`. This is the
  // narrow-once boundary -- everything below this line assumes the
  // invariant holds and never re-checks it. `loadGraph` is the only
  // place a wire-shaped `SwarmGraph` enters the store; it normalizes
  // before ever calling `set`.
  graph: NormalizedSwarmGraph | null;

  // Save-path bookkeeping (GROUP6_PLAN.md review finding A8): a
  // monotonic mutation counter, and the revision an in-flight/most
  // recent save answered for -- so a save response only clears `dirty`
  // if no mutation happened since the request it belongs to.
  mutationRevision: number;
  savedRevision: number;

  selectedNodeId: string | null;
  selectedNodeIds: string[];
  selectedEdgeId: string | null;

  dirty: boolean;
  saveStatus: 'idle' | 'saving' | 'saved' | 'error';
  saveError: string | null;
  saveRetryCount: number;

  compile: CompileState;
  /** The run sub-state (`state/runState.ts`): a job of kind `run` on the
   * same transport as `compile`, tracing the workflow node by node. */
  run: RunState;

  // ---- database starter catalog ----
  // App-scoped, not graph-scoped: the catalog describes what a *kind* can be
  // created as, so it survives every `loadGraph`/`newGraph` and is fetched
  // once per session (`loadDatabaseStarters`, wired at app start) with an
  // explicit, user-triggered retry (`retryDatabaseStarters`). `null` means
  // "no usable catalog right now": the palette's three database entries are
  // disabled and the Inspector says why, rather than creating a node whose
  // operation nothing could fill in.
  databaseStarters: DatabaseStarterMap | null;
  databaseStartersStatus: 'idle' | 'loading' | 'ready' | 'unavailable';
  databaseStartersError: string | null;
  /** Fetch the catalog unless it is already loading or loaded. Idempotent by
   * design, so calling it from an app-start effect is safe under React
   * StrictMode's double mount. */
  loadDatabaseStarters(): Promise<void>;
  /** Fetch it again, whatever the current status -- the retry path behind
   * "Retry" in the palette and the Inspector. */
  retryDatabaseStarters(): Promise<void>;

  // ---- graph lifecycle ----
  // `loadGraph` accepts the wire-shaped `SwarmGraph` (from
  // `api.getGraph`/`api.putGraph`) and normalizes it before storing.
  loadGraph(graph: SwarmGraph): void;
  newGraph(id: string, name: string): NormalizedSwarmGraph;

  // ---- selection ----
  selectNode(id: string | null): void;
  selectNodes(ids: string[]): void;
  selectEdge(id: string | null): void;

  // ---- node actions ----
  addNode(kind: NodeKind, title: string, position: Position, patch?: NodeStarterPatch): string;
  updateNode(id: string, patch: Partial<NormalizedSwarmNode>): void;
  updateNodePosition(id: string, position: Position): void;
  removeNode(id: string): void;

  // ---- edge actions ----
  addEdge(kind: SwarmEdgeKind, source: string, target: string, extra?: EdgeExtra): string;
  updateEdge(id: string, patch: Partial<SwarmEdge>): void;
  removeEdge(id: string): void;
  /** Move an existing edge's source or target to a different node, keeping
   * the edge's kind and syncing the paired spec fields (decision branches,
   * agent delegatesTo). Returns false and mutates nothing when the move would
   * violate a kind constraint (branch/delegate source kind) or is a no-op. */
  reconnectEdge(id: string, source: string, target: string): boolean;

  // ---- graph-level fields ----
  setStateFields(fields: StateField[]): void;
  setModelOverride(sel: ModelSelection | null): void;
  setEntryNodeId(id: string): void;
  setExitNodeId(id: string): void;

  // ---- paired invariant actions (review findings D2/D4) ----
  addDecisionBranch(decisionNodeId: string, match: string, targetNodeId: string): void;
  removeDecisionBranch(decisionNodeId: string, match: string): void;
  setDelegatesTo(orchestratorNodeId: string, targetNodeIds: string[]): void;

  // ---- compile ----
  /** Merge a partial update. An omitted (or explicitly `undefined`) field
   * keeps the value it already had, so the array fields can never be
   * undefined. */
  setCompileState(patch: Partial<CompileState>): void;
  resetCompileState(): void;
  /** Fold one dispatched SSE frame into compile state. */
  applyCompileEvent(event: CompileSseEvent): void;
  /** Rebuild what a reloaded page can know from the status snapshot (which
   * carries no phases/log/warnings -- the stream refills those). */
  applyCompileSnapshot(snapshot: CompileSnapshot): void;
  /** Record a transport-level stream failure; never downgrades a job the
   * server already declared terminal. */
  markCompileStreamError(message: string): void;

  // ---- run ----
  setRunState(patch: Partial<RunState>): void;
  resetRunState(): void;
  applyRunEvent(event: CompileSseEvent): void;
  applyRunSnapshot(snapshot: CompileSnapshot): void;
  markRunStreamError(message: string): void;
  /** Replace the run view with a persisted record (browsing history). */
  loadRunRecord(record: RunRecord): void;

  // ---- internal: called only by the autosave effect wired from App.tsx ----
  markSaving(): number; // returns the mutationRevision being saved
  applySaved(updatedAt: string, forRevision: number): void;
  markSaveError(detail: string): void;

  // ---- pure selector used by both the autosave effect and tests ----
  // Returns the normalized graph, which is always structurally
  // assignable back to the wire `SwarmGraph` type (a required array is
  // assignable to an optional one of the same element type), so the
  // PUT payload stays camelCase-and-complete without a second
  // denormalization step.
  toSavePayload(): NormalizedSwarmGraph | null;
}

function touch(graph: NormalizedSwarmGraph): NormalizedSwarmGraph {
  return { ...graph, updatedAt: nowIso() };
}

export const useGraphStore = create<GraphStoreState>((set, get) => ({
  graph: null,
  mutationRevision: 0,
  savedRevision: 0,
  selectedNodeId: null,
  selectedNodeIds: [],
  selectedEdgeId: null,
  dirty: false,
  saveStatus: 'idle',
  saveError: null,
  saveRetryCount: 0,
  compile: initialCompileState(),
  run: initialRunState(),
  databaseStarters: null,
  databaseStartersStatus: 'idle',
  databaseStartersError: null,

  async loadDatabaseStarters() {
    const status = get().databaseStartersStatus;
    // Fetched once per session: 'loading' covers React StrictMode's second
    // mount effect, 'ready' covers every later caller. A retry is an explicit
    // user action (`retryDatabaseStarters`), never an accident of mounting.
    if (status === 'loading' || status === 'ready') return;
    await get().retryDatabaseStarters();
  },

  async retryDatabaseStarters() {
    set({ databaseStartersStatus: 'loading', databaseStartersError: null });
    try {
      const starters = buildDatabaseStarterMap(await api.listDatabaseStarters());
      if (!starters) {
        // The route serves all three kinds or 503s, so an incomplete answer is
        // not something this client can repair -- and it may not guess.
        set({
          databaseStarters: null,
          databaseStartersStatus: 'unavailable',
          databaseStartersError:
            'The server returned an incomplete database starter catalog (sql, noSQL and vector are needed).',
        });
        return;
      }
      set({
        databaseStarters: starters,
        databaseStartersStatus: 'ready',
        databaseStartersError: null,
      });
    } catch (err) {
      set({
        databaseStarters: null,
        databaseStartersStatus: 'unavailable',
        databaseStartersError: err instanceof Error ? err.message : String(err),
      });
    }
  },

  loadGraph(graph) {
    set({
      graph: normalizeGraph(graph),
      mutationRevision: 0,
      savedRevision: 0,
      selectedNodeId: null,
      selectedNodeIds: [],
      selectedEdgeId: null,
      dirty: false,
      saveStatus: 'idle',
      saveError: null,
      saveRetryCount: 0,
      compile: initialCompileState(),
      run: initialRunState(),
    });
  },

  newGraph(id, name) {
    // A SwarmGraph document is structurally unrepresentable with zero
    // nodes (entryNodeId/exitNodeId must each name a real node --
    // models.py's _check_structural_integrity), so newGraph always
    // creates exactly one starter node and points both entry and exit
    // at it (GROUP6_PLAN.md review finding A10).
    const starterId = assignNodeId('Start', [], 0);
    const starterNode: NormalizedSwarmNode = {
      id: starterId,
      kind: 'agent',
      title: 'Start',
      intent: '',
      position: { x: 0, y: 0 },
      template: null,
      io: defaultIo(),
      reads: [],
      writes: [],
      agent: defaultAgentSpec(),
      programmatic: null,
      decision: null,
      join: null,
      sql: null,
      nosql: null,
      vector: null,
    };
    const graph: NormalizedSwarmGraph = {
      version: 1,
      id,
      name,
      entryNodeId: starterId,
      exitNodeId: starterId,
      stateFields: [],
      nodes: [starterNode],
      edges: [],
      model: null,
      updatedAt: nowIso(),
    };
    set({
      graph,
      mutationRevision: 0,
      savedRevision: 0,
      selectedNodeId: starterId,
      selectedNodeIds: [starterId],
      selectedEdgeId: null,
      dirty: false,
      saveStatus: 'idle',
      saveError: null,
      saveRetryCount: 0,
      compile: initialCompileState(),
      run: initialRunState(),
    });
    return graph;
  },

  selectNode(id) {
    set({ selectedNodeId: id, selectedNodeIds: id ? [id] : [], selectedEdgeId: null });
  },

  selectNodes(ids) {
    set({ selectedNodeId: ids[0] ?? null, selectedNodeIds: ids, selectedEdgeId: null });
  },

  selectEdge(id) {
    set({ selectedEdgeId: id, selectedNodeId: null, selectedNodeIds: [] });
  },

  addNode(kind, title, position, patch) {
    const graph = get().graph;
    if (!graph) throw new Error('addNode called with no graph loaded');
    if (patch && patch.kind !== kind) {
      // A mismatched patch is a caller bug, not a user action: applying a SQL
      // starter to a noSQL node would produce a document whose spec and kind
      // disagree. Loudly, rather than silently dropping it.
      throw new Error(`addNode: a ${patch.kind} starter cannot be applied to a ${kind} node`);
    }
    const existingIds = graph.nodes.map((n) => n.id);
    const id = assignNodeId(title, existingIds, graph.nodes.length);

    const base = {
      id,
      title,
      intent: '',
      position,
      reads: [] as string[],
      writes: [] as string[],
      io: patch?.io ?? defaultIoForKind(kind),
      agent: null,
      programmatic: null,
      decision: null,
      join: null,
      sql: null,
      nosql: null,
      vector: null,
      // Templates are agent-only (`db_template_set`): a database node never
      // carries one, not even a stale one.
      template: null,
    };

    let node: NormalizedSwarmNode;
    switch (kind) {
      case 'agent':
        node = { ...base, kind: 'agent', agent: defaultAgentSpec() };
        break;
      case 'programmatic':
        node = { ...base, kind: 'programmatic', programmatic: defaultProgrammaticSpec() };
        break;
      case 'decision':
        node = { ...base, kind: 'decision', decision: defaultDecisionSpec() };
        break;
      case 'join':
        node = { ...base, kind: 'join', join: defaultJoinSpec() };
        break;
      case 'sql': {
        const spec = patch && patch.kind === 'sql' ? patch.spec : defaultSqlSpec();
        node = { ...base, kind: 'sql', sql: spec };
        break;
      }
      case 'nosql': {
        const spec = patch && patch.kind === 'nosql' ? patch.spec : defaultNosqlSpec();
        node = { ...base, kind: 'nosql', nosql: spec };
        break;
      }
      case 'vector': {
        const spec = patch && patch.kind === 'vector' ? patch.spec : defaultVectorSpec();
        node = { ...base, kind: 'vector', vector: spec };
        break;
      }
    }

    set((state) => ({
      graph: touch({ ...graph, nodes: [...graph.nodes, node] }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
      selectedNodeId: id,
      selectedNodeIds: [id],
    }));
    return id;
  },

  updateNode(id, patch) {
    const graph = get().graph;
    if (!graph) return;
    // `id` itself is never part of a patch's effective change -- node
    // ids are assigned once at creation and never recomputed from a
    // title edit (GROUP6_PLAN.md decision 3 / review finding C1).
    const { id: _ignoredId, ...rest } = patch;
    set((state) => ({
      graph: touch({
        ...graph,
        nodes: graph.nodes.map((n) => (n.id === id ? { ...n, ...rest, id: n.id } : n)),
      }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
    }));
  },

  updateNodePosition(id, position) {
    // The one write-back path from React Flow's onNodesChange
    // (GROUP6_PLAN.md review finding A7): touches ONLY `position`,
    // never a whole React-Flow-owned node object, so no RF-internal key
    // (selected/measured/data/...) can ever leak into the schema-typed
    // document.
    const graph = get().graph;
    if (!graph) return;
    set((state) => ({
      graph: touch({
        ...graph,
        nodes: graph.nodes.map((n) => (n.id === id ? { ...n, position } : n)),
      }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
    }));
  },

  removeNode(id) {
    const graph = get().graph;
    if (!graph) return;

    const remainingNodes = graph.nodes.filter((n) => n.id !== id);
    if (remainingNodes.length === 0) {
      // A graph cannot exist with zero nodes (entryNodeId/exitNodeId
      // must name a real node) -- block the deletion rather than
      // produce an unrepresentable document (review finding D6).
      return;
    }

    // Cascade (review finding D6), one atomic transaction:
    // 1. drop every edge whose source or target is `id`.
    const remainingEdges = graph.edges.filter((e) => e.source !== id && e.target !== id);

    // 2. drop every DecisionBranch targeting `id` (the paired
    //    BranchEdge is already gone via step 1).
    // 3. drop `id` from every agent.delegatesTo (the paired
    //    DelegateEdge is already gone via step 1).
    // 4. a FanoutEdge whose joinNodeId === id is meaningless once its
    //    join node is gone -- already dropped if `id` was itself a
    //    source/target; also strip any remaining reference defensively.
    const fixedNodes = remainingNodes.map((n) => {
      let next = n;
      if (next.kind === 'decision' && next.decision) {
        const branches = next.decision.branches?.filter((b) => b.targetNodeId !== id) ?? [];
        if (branches.length !== (next.decision.branches?.length ?? 0)) {
          next = { ...next, decision: { ...next.decision, branches } };
        }
      }
      if (next.kind === 'agent' && next.agent) {
        const delegatesTo = next.agent.delegatesTo?.filter((d) => d !== id) ?? [];
        if (delegatesTo.length !== (next.agent.delegatesTo?.length ?? 0)) {
          next = { ...next, agent: { ...next.agent, delegatesTo } };
        }
      }
      return next;
    });
    const fixedEdges = remainingEdges.filter((e) => {
      if (e.kind === 'fanout' && e.joinNodeId === id) return false;
      return true;
    });

    let entryNodeId = graph.entryNodeId;
    let exitNodeId = graph.exitNodeId;
    // `fixedNodes` has the same length as `remainingNodes`, already
    // asserted non-empty above -- an explicit guard here, rather than
    // indexing straight through, is what noUncheckedIndexedAccess asks
    // for instead of a blanket `!`.
    const fallbackNode = fixedNodes[0];
    if (fallbackNode) {
      if (entryNodeId === id) entryNodeId = fallbackNode.id;
      if (exitNodeId === id) exitNodeId = fallbackNode.id;
    }

    set((state) => ({
      graph: touch({
        ...graph,
        nodes: fixedNodes,
        edges: fixedEdges,
        entryNodeId,
        exitNodeId,
      }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
      selectedNodeId: state.selectedNodeId === id ? null : state.selectedNodeId,
      selectedNodeIds: state.selectedNodeIds.filter((sid) => sid !== id),
    }));
  },

  addEdge(kind, source, target, extra) {
    const graph = get().graph;
    if (!graph) throw new Error('addEdge called with no graph loaded');
    const id = crypto.randomUUID();

    let edge: SwarmEdge;
    switch (kind) {
      case 'seq':
        edge = { kind: 'seq', id, source, target, label: extra && extra.kind === 'seq' ? extra.label ?? null : null };
        break;
      case 'branch': {
        if (!extra || extra.kind !== 'branch') throw new Error('branch edge requires a match expression');
        edge = { kind: 'branch', id, source, target, match: extra.match };
        break;
      }
      case 'fanout': {
        if (!extra || extra.kind !== 'fanout') throw new Error('fanout edge requires a joinNodeId');
        edge = { kind: 'fanout', id, source, target, joinNodeId: extra.joinNodeId };
        break;
      }
      case 'join':
        edge = { kind: 'join', id, source, target };
        break;
      case 'delegate':
        edge = { kind: 'delegate', id, source, target };
        break;
    }

    set((state) => ({
      graph: touch({ ...graph, edges: [...graph.edges, edge] }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
    }));
    return id;
  },

  updateEdge(id, patch) {
    const graph = get().graph;
    if (!graph) return;
    set((state) => ({
      graph: touch({
        ...graph,
        edges: graph.edges.map((e) => (e.id === id ? ({ ...e, ...patch, id: e.id, kind: e.kind } as SwarmEdge) : e)),
      }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
    }));
  },

  removeEdge(id) {
    const graph = get().graph;
    if (!graph) return;
    const edge = graph.edges.find((e) => e.id === id);
    if (!edge) return;

    let nodes = graph.nodes;
    if (edge.kind === 'branch') {
      // Keep DecisionSpec.branches in sync (review finding D2): removing
      // a branch edge removes the matching DecisionBranch entry too.
      nodes = nodes.map((n) => {
        if (n.id === edge.source && n.kind === 'decision' && n.decision) {
          const branches = n.decision.branches?.filter(
            (b) => !(b.match === edge.match && b.targetNodeId === edge.target),
          ) ?? [];
          return { ...n, decision: { ...n.decision, branches } };
        }
        return n;
      });
    } else if (edge.kind === 'delegate') {
      nodes = nodes.map((n) => {
        if (n.id === edge.source && n.kind === 'agent' && n.agent) {
          const delegatesTo = n.agent.delegatesTo?.filter((d) => d !== edge.target) ?? [];
          return { ...n, agent: { ...n.agent, delegatesTo } };
        }
        return n;
      });
    }

    set((state) => ({
      graph: touch({
        ...graph,
        nodes,
        edges: graph.edges.filter((e) => e.id !== id),
      }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
      selectedEdgeId: state.selectedEdgeId === id ? null : state.selectedEdgeId,
    }));
  },

  reconnectEdge(id, source, target) {
    const graph = get().graph;
    if (!graph) return false;
    const edge = graph.edges.find((e) => e.id === id);
    if (!edge) return false;

    const sourceNode = graph.nodes.find((n) => n.id === source);
    const targetNode = graph.nodes.find((n) => n.id === target);
    if (!sourceNode || !targetNode) return false;
    if (source === target) return false;
    if (edge.source === source && edge.target === target) return false;

    // Kind constraints: a branch must originate on a decision, a delegate on
    // an agent. Rejecting here (and returning false) snaps the edge back in
    // React Flow rather than writing a document Phase-1 review would refuse.
    if (edge.kind === 'branch' && sourceNode.kind !== 'decision') return false;
    if (edge.kind === 'delegate' && sourceNode.kind !== 'agent') return false;

    // Duplicate guard: never create a second edge with the same semantic pair.
    const duplicate = graph.edges.some(
      (e) =>
        e.id !== id &&
        e.kind === edge.kind &&
        e.source === source &&
        e.target === target &&
        (edge.kind !== 'branch' || (e.kind === 'branch' && e.match === edge.match)),
    );
    if (duplicate) return false;

    let nodes = graph.nodes;
    if (edge.kind === 'branch') {
      // Keep DecisionSpec.branches in sync on either endpoint move.
      const match = edge.match;
      if (edge.source !== source) {
        // Branch moved to a different decision node: relocate the spec entry.
        nodes = nodes.map((n) => {
          if (n.id === edge.source && n.kind === 'decision' && n.decision) {
            const branches =
              n.decision.branches?.filter(
                (b) => !(b.match === match && b.targetNodeId === edge.target),
              ) ?? [];
            return { ...n, decision: { ...n.decision, branches } };
          }
          if (n.id === source && n.kind === 'decision' && n.decision) {
            const branches = [...(n.decision.branches ?? []), { match, targetNodeId: target }];
            return { ...n, decision: { ...n.decision, branches } };
          }
          return n;
        });
      } else {
        // Same decision, new target: retarget the existing spec entry.
        nodes = nodes.map((n) => {
          if (n.id === source && n.kind === 'decision' && n.decision) {
            const branches = (n.decision.branches ?? []).map((b) =>
              b.match === match && b.targetNodeId === edge.target
                ? { ...b, targetNodeId: target }
                : b,
            );
            return { ...n, decision: { ...n.decision, branches } };
          }
          return n;
        });
      }
    } else if (edge.kind === 'delegate') {
      // Keep agent.delegatesTo in sync on either endpoint move.
      if (edge.source !== source) {
        nodes = nodes.map((n) => {
          if (n.id === edge.source && n.kind === 'agent' && n.agent) {
            const delegatesTo = n.agent.delegatesTo?.filter((d) => d !== edge.target) ?? [];
            return { ...n, agent: { ...n.agent, delegatesTo } };
          }
          if (n.id === source && n.kind === 'agent' && n.agent) {
            const delegatesTo = [...(n.agent.delegatesTo ?? []), target];
            return { ...n, agent: { ...n.agent, delegatesTo } };
          }
          return n;
        });
      } else {
        nodes = nodes.map((n) => {
          if (n.id === source && n.kind === 'agent' && n.agent) {
            const delegatesTo = (n.agent.delegatesTo ?? []).map((d) =>
              d === edge.target ? target : d,
            );
            return { ...n, agent: { ...n.agent, delegatesTo } };
          }
          return n;
        });
      }
    }

    const edges = graph.edges.map((e) =>
      e.id === id ? { ...e, source, target, id: e.id, kind: e.kind } : e,
    ) as SwarmEdge[];

    set((state) => ({
      graph: touch({ ...graph, nodes, edges }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
    }));
    return true;
  },

  setStateFields(fields) {
    const graph = get().graph;
    if (!graph) return;
    set((state) => ({
      graph: touch({ ...graph, stateFields: fields }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
    }));
  },

  setModelOverride(sel) {
    const graph = get().graph;
    if (!graph) return;
    set((state) => ({
      graph: touch({ ...graph, model: sel }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
    }));
  },

  setEntryNodeId(id) {
    const graph = get().graph;
    if (!graph) return;
    set((state) => ({
      graph: touch({ ...graph, entryNodeId: id }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
    }));
  },

  setExitNodeId(id) {
    const graph = get().graph;
    if (!graph) return;
    set((state) => ({
      graph: touch({ ...graph, exitNodeId: id }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
    }));
  },

  addDecisionBranch(decisionNodeId, match, targetNodeId) {
    // Creates the spec entry and the paired BranchEdge in one
    // transaction (review finding D2) -- the Inspector's branch editor
    // must call this exclusively, never write DecisionSpec.branches
    // directly, so the two can never drift out of sync.
    const graph = get().graph;
    if (!graph) return;
    const edgeId = crypto.randomUUID();
    const nodes = graph.nodes.map((n) => {
      if (n.id === decisionNodeId && n.kind === 'decision' && n.decision) {
        const branches = [...(n.decision.branches ?? []), { match, targetNodeId }];
        return { ...n, decision: { ...n.decision, branches } };
      }
      return n;
    });
    const branchEdge: SwarmEdge = { kind: 'branch', id: edgeId, source: decisionNodeId, target: targetNodeId, match };
    set((state) => ({
      graph: touch({ ...graph, nodes, edges: [...graph.edges, branchEdge] }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
    }));
  },

  removeDecisionBranch(decisionNodeId, match) {
    const graph = get().graph;
    if (!graph) return;
    const decisionNode = graph.nodes.find((n) => n.id === decisionNodeId);
    const branch = decisionNode?.decision?.branches?.find((b) => b.match === match);
    const nodes = graph.nodes.map((n) => {
      if (n.id === decisionNodeId && n.kind === 'decision' && n.decision) {
        const branches = n.decision.branches?.filter((b) => b.match !== match) ?? [];
        return { ...n, decision: { ...n.decision, branches } };
      }
      return n;
    });
    const edges = graph.edges.filter(
      (e) => !(e.kind === 'branch' && e.source === decisionNodeId && e.match === match && (!branch || e.target === branch.targetNodeId)),
    );
    set((state) => ({
      graph: touch({ ...graph, nodes, edges }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
    }));
  },

  setDelegatesTo(orchestratorNodeId, targetNodeIds) {
    // Diffs against the current delegatesTo/DelegateEdge state and
    // creates/removes edges so the two never drift (review finding D4).
    const graph = get().graph;
    if (!graph) return;
    const node = graph.nodes.find((n) => n.id === orchestratorNodeId);
    if (!node || node.kind !== 'agent' || !node.agent) return;

    const before = new Set(node.agent.delegatesTo ?? []);
    const after = new Set(targetNodeIds);

    const nodes = graph.nodes.map((n) =>
      n.id === orchestratorNodeId && n.kind === 'agent' && n.agent
        ? { ...n, agent: { ...n.agent, delegatesTo: targetNodeIds } }
        : n,
    );

    let edges = graph.edges.filter(
      (e) => !(e.kind === 'delegate' && e.source === orchestratorNodeId && !after.has(e.target)),
    );
    for (const targetId of targetNodeIds) {
      if (!before.has(targetId)) {
        edges = [...edges, { kind: 'delegate', id: crypto.randomUUID(), source: orchestratorNodeId, target: targetId }];
      }
    }

    set((state) => ({
      graph: touch({ ...graph, nodes, edges }),
      mutationRevision: state.mutationRevision + 1,
      dirty: true,
    }));
  },

  setCompileState(patch) {
    set((state) => ({ compile: mergeCompilePatch(state.compile, patch) }));
  },

  resetCompileState() {
    set({ compile: initialCompileState() });
  },

  applyCompileEvent(event) {
    set((state) => ({ compile: applyCompileEvent(state.compile, event) }));
  },

  applyCompileSnapshot(snapshot) {
    set((state) => ({ compile: applyCompileSnapshot(state.compile, snapshot) }));
  },

  markCompileStreamError(message) {
    set((state) => ({ compile: applyCompileStreamError(state.compile, message) }));
  },

  setRunState(patch) {
    set((state) => ({ run: mergeRunPatch(state.run, patch) }));
  },
  resetRunState() {
    set({ run: initialRunState() });
  },
  applyRunEvent(event) {
    set((state) => ({ run: applyRunEvent(state.run, event) }));
  },
  applyRunSnapshot(snapshot) {
    set((state) => ({ run: applyRunSnapshot(state.run, snapshot) }));
  },
  markRunStreamError(message) {
    set((state) => ({ run: applyRunStreamError(state.run, message) }));
  },
  loadRunRecord(record) {
    set({ run: runStateFromRecord(record) });
  },

  markSaving() {
    set({ saveStatus: 'saving' });
    return get().mutationRevision;
  },

  applySaved(updatedAt, forRevision) {
    const graph = get().graph;
    set((state) => ({
      graph: graph ? { ...graph, updatedAt } : graph,
      // Only clear dirty if no mutation happened since the request
      // this response answers (review finding A8) -- otherwise an
      // edit landing between request-send and response would be
      // wrongly marked clean and never re-saved.
      dirty: state.mutationRevision !== forRevision,
      savedRevision: forRevision,
      saveStatus: 'saved',
      saveError: null,
      saveRetryCount: 0,
    }));
  },

  markSaveError(detail) {
    set((state) => ({
      saveStatus: 'error',
      saveError: detail,
      saveRetryCount: state.saveRetryCount + 1,
    }));
  },

  toSavePayload() {
    const graph = get().graph;
    if (!graph) return null;
    // Constructed from the schema's own keys only (spread of the
    // already-schema-shaped `graph` object) -- never a raw spread of
    // anything React Flow has touched (review finding A6). Because
    // every mutating action above already only ever writes
    // schema-shaped fields (updateNodePosition touches only
    // `position`; onNodesChange's select/dimensions changes are
    // ignored entirely by the canvas wiring, never reaching the
    // store), `graph` itself is always already clean -- this function
    // exists as the one, explicit place that contract is asserted and
    // named, so a future change has one obvious place to keep honest.
    return graph;
  },
}));
