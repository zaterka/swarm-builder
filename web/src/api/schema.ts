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

// The three database node kinds' specs (`models.py`). One field per kind on
// `SwarmNode`, exactly like `agent`/`programmatic`/`decision`/`join`.
export type SqlSpec = components['schemas']['SqlSpec'];
export type NosqlSpec = components['schemas']['NosqlSpec'];
export type VectorSpec = components['schemas']['VectorSpec'];
export type VectorDocument = components['schemas']['VectorDocument'];

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
export type NosqlOperation = NosqlSpec['operation'];

// The three database kinds, as a *sub*-union of `NodeKind` rather than a
// second hand-written union: `Extract` is empty for a member the generated
// union does not have, so `DATABASE_KINDS` below is what makes "the canvas
// knows exactly the database kinds the server schema declares" a
// compile-time fact (see schema.test.ts's parity assertions).
export type DatabaseKind = Extract<NodeKind, 'sql' | 'nosql' | 'vector'>;

export type HealthResponse = components['schemas']['HealthResponse'];
export type ResolvedModelSummary = components['schemas']['ResolvedModelSummary'];
export type ModelsResponse = components['schemas']['ModelsResponse'];
export type AppRouteOut = NonNullable<ModelsResponse['appRoute']>;
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

// The database starter catalog (`GET /api/database-starters`,
// `templates/database/__init__.py` -> `DatabaseKindEntry`). `spec` is the
// union of the three spec models on the wire -- the kind and its spec are
// independent fields in the JSON schema, so the pairing is checked at
// runtime (see `isSqlSpec`/`isNosqlSpec`/`isVectorSpec` below) and re-typed
// per kind by `DatabaseStarter`/`DatabaseStarterMap`.
export type DatabaseStarterOut = components['schemas']['DatabaseStarterOut'];
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
export type ClarifyRequest = components['schemas']['ClarifyRequest'];
export type ClarifyResponse = components['schemas']['ClarifyResponse'];
export type ClarifyQuestionOut = components['schemas']['ClarifyQuestionOut'];
export type ClarifyOptionOut = components['schemas']['ClarifyOptionOut'];
export type ClarifyAnswer = components['schemas']['ClarifyAnswerInRaw'];
export type AttachmentSummaryOut = components['schemas']['AttachmentSummaryOut'];

// Supplementary files (`routes/attachments.py`). The server owns the caps and
// the supported formats; the panel renders what it is told rather than restating
// those rules, so the two cannot drift.
export type AttachmentOut = components['schemas']['AttachmentOut'];
export type AttachmentUploadResponse = components['schemas']['AttachmentUploadResponse'];
export type AttachmentDeleteResponse = components['schemas']['AttachmentDeleteResponse'];

/** The structured detail the attachment, clarify and generate routes return:
 * `{code, message, problems}`. `code` is what lets the panel tell "this
 * attachment expired" from "this model cannot read images" without parsing prose.
 * Hand-written rather than generated, because it is the *error* envelope shared
 * by several routes, not one route's response schema. */
export interface ProblemDetail {
  code: string;
  message: string;
  problems: string[];
}

// In-app model settings (`routes/settings.py`). `SavedModelOut` deliberately
// has no field for the API key itself: the server reports only whether one is
// stored and its last four characters, so there is no type here that could
// carry a secret into the client even by mistake.
export type SettingsResponse = components['schemas']['SettingsResponse'];
export type SettingsUpdateRequest = components['schemas']['SettingsUpdateRequest'];
export type ModelInput = components['schemas']['ModelInput'];
export type SavedModelOut = components['schemas']['SavedModelOut'];
export type ProviderOut = components['schemas']['ProviderOut'];
export type TestConnectionRequest = components['schemas']['TestConnectionRequest'];
export type TestConnectionResponse = components['schemas']['TestConnectionResponse'];

export const PORT_TYPES: readonly PortType[] = ['str', 'json', 'list[str]', 'list[json]'];
export const REDUCER_IDS: readonly ReducerId[] = [
  'list_append',
  'list_extend',
  'dict_update',
  'sum',
];
export const TEMPLATE_IDS: readonly TemplateId[] = ['chat', 'orchestrator', 'websearch'];
export const NODE_KINDS: readonly NodeKind[] = [
  'agent',
  'programmatic',
  'decision',
  'join',
  'sql',
  'nosql',
  'vector',
];

/** The three database kinds, in the order the server's catalog serves them
 * (`templates/database/__init__.py` -> `KIND_ORDER`). Typed as
 * `DatabaseKind`, i.e. `Extract<NodeKind, ...>`, so a database kind the
 * generated `NodeKind` union does not have is a compile error here rather
 * than a member the palette could silently create. */
export const DATABASE_KINDS: readonly DatabaseKind[] = ['sql', 'nosql', 'vector'];

/** `nosql.operation`'s closed set (`models.py` -> `NosqlSpec.operation`).
 * `insert_one` is the one write mode a NoSQL node can declare, which is why
 * this list is also what the Inspector's read-only-tool filter consults. */
export const NOSQL_OPERATIONS: readonly NosqlOperation[] = [
  'find',
  'find_one',
  'count',
  'insert_one',
];

export function isNodeKind(value: unknown): value is NodeKind {
  return typeof value === 'string' && (NODE_KINDS as readonly string[]).includes(value);
}

export function isDatabaseKind(kind: NodeKind): kind is DatabaseKind {
  return (DATABASE_KINDS as readonly string[]).includes(kind);
}

/** kind -> the input port types that kind's step can bind, mirroring
 * `compile/review.py`'s `_BINDABLE_INPUT_TYPES` (the compiler is the
 * authority; this is what lets the Inspector offer only pairs that compile).
 * A `str` binds as the node's whole incoming value, a `json` dict's keys bind
 * by name -- SQL and noSQL only, since a similarity search's parameter is the
 * query text. A `list` input is rejected for all three kinds. */
export const DATABASE_INPUT_TYPES: Record<DatabaseKind, readonly PortType[]> = {
  sql: ['str', 'json', 'list[str]'],
  nosql: ['str', 'json', 'list[str]'],
  vector: ['str'],
};

/** A database step returns rows, so this is the only output type any of the
 * three kinds may declare (`review.py`'s `_DATABASE_OUTPUT_TYPE`). */
export const DATABASE_OUTPUT_TYPES: readonly PortType[] = ['list[json]'];

/** True when this document would be rejected by Phase 1's `db_write_as_tool`
 * if an agent listed it: a SQL node with `write: true`, or a NoSQL node whose
 * operation is `insert_one`. A vector node has no write operation at all.
 * Mirrors `review.py`'s rule, so the Inspector can refuse to offer a tool the
 * compiler would refuse anyway. The parameter is the node's database-relevant
 * surface, which a `SwarmNode` and a `NormalizedSwarmNode` both satisfy. */
export function isWriteDatabaseNode(
  node: Pick<SwarmNode, 'kind' | 'sql' | 'nosql' | 'vector'>,
): boolean {
  if (node.kind === 'sql') return node.sql?.write === true;
  if (node.kind === 'nosql') return node.nosql?.operation === 'insert_one';
  return false;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

// The starters response's `spec` is a union of all three spec models, and
// the JSON schema cannot correlate it with `kind`. These three guards are the
// one place that pairing is checked: each keys on fields only its own model
// declares (`query`/`seedSql`, `operation`, `topK`), so they are mutually
// exclusive and a mismatched entry is dropped rather than copied into a node
// of the wrong kind.
export function isSqlSpec(value: unknown): value is SqlSpec {
  return isRecord(value) && typeof value['query'] === 'string' && typeof value['seedSql'] === 'string';
}

export function isNosqlSpec(value: unknown): value is NosqlSpec {
  return (
    isRecord(value) &&
    typeof value['collection'] === 'string' &&
    typeof value['operation'] === 'string'
  );
}

export function isVectorSpec(value: unknown): value is VectorSpec {
  return isRecord(value) && typeof value['collection'] === 'string' && typeof value['topK'] === 'number';
}

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

// The three database specs. `note` (and, for noSQL, `filter`/`seed`/`limit`,
// and for vector `seed`) are optional on the wire -- a spec-compliant client
// is allowed to omit a defaulted field -- so the normalized forms make every
// field the Inspector renders unconditionally present. The scalar defaults
// (`write`, `operation`, `limit`, `topK`, `minScore`) are filled from the
// schema's own documented defaults, so a document written before this feature
// existed reads exactly like a freshly created node.
export type NormalizedSqlSpec = Omit<SqlSpec, 'write' | 'note'> & {
  write: boolean;
  note: string | null;
};
export type NormalizedNosqlSpec = Omit<NosqlSpec, 'operation' | 'filter' | 'limit' | 'seed' | 'note'> & {
  operation: NosqlOperation;
  filter: Record<string, unknown>;
  limit: number;
  seed: Record<string, unknown>[];
  note: string | null;
};
export type NormalizedVectorDocument = Omit<VectorDocument, 'metadata'> & {
  metadata: Record<string, unknown>;
};
export type NormalizedVectorSpec = Omit<VectorSpec, 'seed' | 'note'> & {
  seed: NormalizedVectorDocument[];
  note: string | null;
};

export type NormalizedSwarmNode = Omit<
  SwarmNode,
  'reads' | 'writes' | 'agent' | 'programmatic' | 'decision' | 'join' | 'sql' | 'nosql' | 'vector'
> & {
  reads: string[];
  writes: string[];
  agent: NormalizedAgentSpec | null;
  programmatic: NormalizedProgrammaticSpec | null;
  decision: NormalizedDecisionSpec | null;
  join: NormalizedJoinSpec | null;
  sql: NormalizedSqlSpec | null;
  nosql: NormalizedNosqlSpec | null;
  vector: NormalizedVectorSpec | null;
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
/** The three spec normalizers, exported for the one caller that holds a spec
 * *before* it becomes part of a document: the store, when it turns a starter
 * catalog entry into a new node's starting spec. One implementation, so a
 * spec read from the catalog and a spec read back from a saved document can
 * never differ in shape. */
export function normalizeSqlSpec(spec: SqlSpec): NormalizedSqlSpec {
  return { ...spec, write: spec.write ?? false, note: spec.note ?? null };
}
export function normalizeNosqlSpec(spec: NosqlSpec): NormalizedNosqlSpec {
  return {
    ...spec,
    operation: spec.operation ?? 'find',
    filter: spec.filter ?? {},
    limit: spec.limit ?? 20,
    seed: spec.seed ?? [],
    note: spec.note ?? null,
  };
}
function normalizeVectorDocument(document: VectorDocument): NormalizedVectorDocument {
  return { ...document, metadata: document.metadata ?? {} };
}
export function normalizeVectorSpec(spec: VectorSpec): NormalizedVectorSpec {
  return {
    ...spec,
    topK: spec.topK ?? 4,
    minScore: spec.minScore ?? 0,
    seed: (spec.seed ?? []).map(normalizeVectorDocument),
    note: spec.note ?? null,
  };
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
    sql: node.sql ? normalizeSqlSpec(node.sql) : null,
    nosql: node.nosql ? normalizeNosqlSpec(node.nosql) : null,
    vector: node.vector ? normalizeVectorSpec(node.vector) : null,
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

// ---------------------------------------------------------------------------
// The database starter catalog, re-typed per kind
//
// The wire type cannot say "kind 'sql' implies `spec: SqlSpec`" (the two are
// independent fields), so the pairing is checked once, when the catalog is
// read, by the guards above -- and everything downstream (the palette, the
// store's `addNode`, the Inspector's "Reset to example") then works with a
// correlated pair and no casts. `DatabaseStarterMap` is a *partial* record on
// purpose: a kind whose entry is missing or mismatched is absent, which is
// what keeps the palette from offering a node it cannot materialize.
// ---------------------------------------------------------------------------

export type DatabaseStarter<K extends DatabaseKind = DatabaseKind> = Omit<
  DatabaseStarterOut,
  'kind' | 'spec'
> & {
  kind: K;
  spec: K extends 'sql'
    ? NormalizedSqlSpec
    : K extends 'nosql'
      ? NormalizedNosqlSpec
      : NormalizedVectorSpec;
};

export type DatabaseStarterMap = {
  readonly [K in DatabaseKind]?: DatabaseStarter<K>;
};

/** Any one kind's entry, as the map holds it: a discriminated union on `kind`,
 * so a `switch (entry.kind)` narrows `entry.spec` to that kind's spec model. */
export type AnyDatabaseStarter =
  | DatabaseStarter<'sql'>
  | DatabaseStarter<'nosql'>
  | DatabaseStarter<'vector'>;

/**
 * Turn the wire entries from `GET /api/database-starters` into the per-kind
 * map the palette and the store work with, or `null` when the catalog is not
 * usable.
 *
 * All-or-nothing on purpose. The route serves every kind or fails with the
 * 503 the client turns into `StartersUnavailableError`, so a *partial* answer
 * means something is wrong that this client cannot see -- and the safe
 * reading of "one kind is missing" is "do not offer any of the three", never
 * "create a node whose operation is empty at compile time". An entry whose
 * `kind` and `spec` disagree is dropped the same way.
 */
export function buildDatabaseStarterMap(entries: DatabaseStarterOut[]): DatabaseStarterMap | null {
  const map: {
    sql?: DatabaseStarter<'sql'>;
    nosql?: DatabaseStarter<'nosql'>;
    vector?: DatabaseStarter<'vector'>;
  } = {};

  for (const entry of entries) {
    const shared = {
      label: entry.label,
      description: entry.description,
      liveExtra: entry.liveExtra,
      envVars: entry.envVars,
      io: entry.io,
    };
    if (entry.kind === 'sql' && isSqlSpec(entry.spec)) {
      map.sql = { ...shared, kind: 'sql', spec: normalizeSqlSpec(entry.spec) };
    } else if (entry.kind === 'nosql' && isNosqlSpec(entry.spec)) {
      map.nosql = { ...shared, kind: 'nosql', spec: normalizeNosqlSpec(entry.spec) };
    } else if (entry.kind === 'vector' && isVectorSpec(entry.spec)) {
      map.vector = { ...shared, kind: 'vector', spec: normalizeVectorSpec(entry.spec) };
    }
  }

  if (!map.sql || !map.nosql || !map.vector) return null;
  return map;
}

