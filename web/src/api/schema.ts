// The single indirection point into `web/src/types.ts` (generated from
// the FastAPI OpenAPI schema). No other file in this package reaches
// into `components["schemas"][...]` by string key — everything else
// imports the aliases below. This is what "import from types.ts, never
// redeclare the graph document's shape" means in practice: `types.ts`
// only exports `paths`/`components`/`operations`, not convenient named
// types, so this module derives the convenient names exactly once.
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
// Not a top-level schema in the generated types (it is inlined as a
// union on SwarmGraph.edges) -- reconstructed once, here, per GROUP6_PLAN.md
// review finding A1.
export type SwarmEdge = SeqEdge | BranchEdge | FanoutEdge | JoinEdge | DelegateEdge;
export type SwarmEdgeKind = SwarmEdge['kind'];

// The rest of these are inlined enums on a parent schema, not top-level
// schemas -- also per finding A1.
export type NodeKind = SwarmNode['kind'];
export type TemplateId = NonNullable<SwarmNode['template']>;
export type PortType = NodeIo['inputType'];
export type ReducerId = JoinSpec['reducer'];
export type InitialFactory = NonNullable<JoinSpec['initialFactory']>;

export type HealthResponse = components['schemas']['HealthResponse'];
export type ResolvedModelSummary = components['schemas']['ResolvedModelSummary'];
export type ModelsResponse = components['schemas']['ModelsResponse'];
export type RouteOut = components['schemas']['RouteOut'];
export type ModelInfoOut = components['schemas']['ModelInfoOut'];
export type ResolvedDefaultOut = components['schemas']['ResolvedDefaultOut'];
export type ReviewResponse = components['schemas']['ReviewResponse'];
export type FindingOut = components['schemas']['FindingOut'];
export type GraphListResponse = components['schemas']['GraphListResponse'];
export type GraphSummary = components['schemas']['GraphSummary'];
export type GraphListError = components['schemas']['GraphListError'];
export type DeleteGraphResponse = components['schemas']['DeleteGraphResponse'];
export type ExportResponse = components['schemas']['ExportResponse'];
export type TemplateEntryOut = components['schemas']['TemplateEntryOut'];
export type ValidationError = components['schemas']['ValidationError'];
export type HTTPValidationError = components['schemas']['HTTPValidationError'];

// The compile job's status snapshot, exactly as `GET`/`DELETE
// /api/compile/{compileId}` return it (`compile/jobs.py` -> `Job.snapshot()`
// -> `routes/compile.py`'s `CompileSnapshotResponse`). It carries
// `compileId`, `graphId`, `status`, the three timestamps, `latestEventId`,
// `result` and `error` -- and nothing else: there are no `phases`,
// `logTail` or `warnings` fields on the wire, because the event stream (with
// `Last-Event-ID` replay) is the delivery channel for those.
export type CompileSnapshot = components['schemas']['CompileSnapshotResponse'];
export type CompileJobStatus = CompileSnapshot['status'];
export type StartCompileRequest = components['schemas']['StartCompileRequest'];
export type StartCompileResponse = components['schemas']['StartCompileResponse'];

// Runs (`routes/runs.py`). A run's live stream/snapshot/cancel reuse the
// compile job endpoints (`/api/jobs/:id...`), so `CompileSnapshot` is also a
// run job's snapshot; only starting a run and its persisted history have
// their own shapes.
export type StartRunRequest = components['schemas']['StartRunRequest'];
export type StartRunResponse = components['schemas']['StartRunResponse'];
export type RunRecord = components['schemas']['RunRecord'];
export type NodeRunRecord = components['schemas']['NodeRunRecord'];
export type RunListResponse = components['schemas']['RunListResponse'];

// Describe -> generate (`routes/generate.py`).
export type GenerateGraphRequest = components['schemas']['GenerateGraphRequest'];
export type GenerateGraphResponse = components['schemas']['GenerateGraphResponse'];

export const PORT_TYPES: readonly PortType[] = ['str', 'json', 'list[str]'];
export const REDUCER_IDS: readonly ReducerId[] = [
  'list_append',
  'list_extend',
  'dict_update',
  'sum',
];
export const TEMPLATE_IDS: readonly TemplateId[] = ['chat', 'orchestrator', 'websearch'];
export const NODE_KINDS: readonly NodeKind[] = ['agent', 'programmatic', 'decision', 'join'];

// ---------------------------------------------------------------------------
// Normalized variants
//
// Several SwarmGraph/SwarmNode/*Spec fields are optional in the
// generated OpenAPI types because the Python side declares a default
// (`Field(default_factory=list)`) -- a client is allowed to OMIT them
// on write, but a document that has actually been loaded/constructed
// always has them present as (possibly empty) arrays at runtime. These
// normalized aliases say that explicitly, so the rest of this package
// never has to thread `?? []` through every read site. A Normalized*
// value is always structurally assignable back to its non-normalized
// counterpart (required is assignable to optional of the same type),
// so `toSavePayload(): SwarmGraph` returning a NormalizedSwarmGraph is
// still exactly the wire type api/client.ts expects.
// ---------------------------------------------------------------------------

export type NormalizedAgentSpec = Omit<AgentSpec, 'tools' | 'delegatesTo'> & {
  tools: string[];
  delegatesTo: string[];
};
export type NormalizedProgrammaticSpec = Omit<ProgrammaticSpec, 'needs' | 'signatureHint'> & {
  needs: string[];
  signatureHint: string | null;
};
export type NormalizedDecisionSpec = Omit<DecisionSpec, 'branches' | 'note'> & {
  branches: DecisionBranch[];
  note: string | null;
};
export type NormalizedJoinSpec = Omit<JoinSpec, 'initialFactory'> & {
  initialFactory: InitialFactory | null;
};

export type NormalizedSwarmNode = Omit<
  SwarmNode,
  'reads' | 'writes' | 'agent' | 'programmatic' | 'decision' | 'join'
> & {
  reads: string[];
  writes: string[];
  agent: NormalizedAgentSpec | null;
  programmatic: NormalizedProgrammaticSpec | null;
  decision: NormalizedDecisionSpec | null;
  join: NormalizedJoinSpec | null;
};

export type NormalizedSwarmGraph = Omit<SwarmGraph, 'nodes' | 'edges' | 'stateFields'> & {
  nodes: NormalizedSwarmNode[];
  edges: SwarmEdge[];
  stateFields: StateField[];
};

function normalizeAgentSpec(spec: AgentSpec): NormalizedAgentSpec {
  return { ...spec, tools: spec.tools ?? [], delegatesTo: spec.delegatesTo ?? [] };
}
function normalizeProgrammaticSpec(spec: ProgrammaticSpec): NormalizedProgrammaticSpec {
  return { ...spec, needs: spec.needs ?? [], signatureHint: spec.signatureHint ?? null };
}
function normalizeDecisionSpec(spec: DecisionSpec): NormalizedDecisionSpec {
  return { ...spec, branches: spec.branches ?? [], note: spec.note ?? null };
}
function normalizeJoinSpec(spec: JoinSpec): NormalizedJoinSpec {
  return { ...spec, initialFactory: spec.initialFactory ?? null };
}

function normalizeNode(node: SwarmNode): NormalizedSwarmNode {
  return {
    ...node,
    reads: node.reads ?? [],
    writes: node.writes ?? [],
    agent: node.agent ? normalizeAgentSpec(node.agent) : null,
    programmatic: node.programmatic ? normalizeProgrammaticSpec(node.programmatic) : null,
    decision: node.decision ? normalizeDecisionSpec(node.decision) : null,
    join: node.join ? normalizeJoinSpec(node.join) : null,
  };
}

/** Normalize a SwarmGraph as loaded from the server/local construction
 * into a shape where every default-backed array field is guaranteed
 * present. Called once, at every graph load/creation boundary. */
export function normalizeGraph(graph: SwarmGraph): NormalizedSwarmGraph {
  return {
    ...graph,
    nodes: (graph.nodes ?? []).map(normalizeNode),
    edges: graph.edges ?? [],
    stateFields: graph.stateFields ?? [],
  };
}

