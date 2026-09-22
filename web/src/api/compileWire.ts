// The compile wire vocabulary OpenAPI cannot express, written down once and
// narrowed once.
//
// Every *request/response* shape of the compile endpoints comes from the
// generated schema (`api/schema.ts` -> `types.ts` -> the server's Pydantic
// models); nothing here redeclares one. What is left over is:
//
//   1. the SSE frame vocabulary (`phase`/`log`/`warning`/`done`/`error`, the
//      phase slugs and the three phase statuses). SSE is not described by the
//      OpenAPI schema at all, so this mirrors `docs/api.md` -> "SSE event
//      stream", which in turn mirrors `compile/pipeline.py`'s own constants;
//   2. the `done` payload / snapshot `result`, whose generated type is the
//      deliberately open `{[key: string]: unknown}` the server returns
//      `Job.result.value` as.
//
// Both are narrowed here, once, exactly like `normalizeGraph` (#1's sibling
// in `api/schema.ts`) narrows the graph document: the readers below are the
// only place in this package that looks at an untyped payload, and every
// caller downstream gets a typed value -- never `String(someObject)`, which
// is what turned the whole streamed log into `[object Object]`.
import type { CompileJobStatus, CompileSnapshot, FindingOut, ResolvedDefaultOut } from './schema';

// ---------------------------------------------------------------------------
// Event, phase and status vocabulary
// ---------------------------------------------------------------------------

/** The SSE `event:` names the compile stream uses (docs/api.md "The five
 * event types"). */
export const COMPILE_EVENT_TYPES = ['phase', 'log', 'warning', 'done', 'error'] as const;
export type CompileSseEventType = (typeof COMPILE_EVENT_TYPES)[number];

export function isCompileEventType(value: string): value is CompileSseEventType {
  return (COMPILE_EVENT_TYPES as readonly string[]).includes(value);
}

/** The pipeline's phase slugs, in the pipeline's own order
 * (`compile/pipeline.py` `PHASE_NAMES`). Used to render the full phase list
 * -- including the phases a resuming client has not seen an event for yet --
 * without inventing slugs. */
export const COMPILE_PHASE_NAMES = ['review', 'scaffold', 'fill', 'boundary', 'validate'] as const;
export type CompilePhaseName = (typeof COMPILE_PHASE_NAMES)[number];

export function isCompilePhaseName(value: string): value is CompilePhaseName {
  return (COMPILE_PHASE_NAMES as readonly string[]).includes(value);
}

/** The *only* three `phase.status` values the pipeline emits
 * (`PHASE_STATUS_STARTED`/`SUCCEEDED`/`FAILED` in `compile/pipeline.py`,
 * documented in docs/api.md). A job-level `status` (queued/running/... ) is a
 * different, five-valued vocabulary -- `CompileJobStatus` in `api/schema.ts` --
 * and the two must not be conflated. */
export const COMPILE_PHASE_STATUSES = ['started', 'succeeded', 'failed'] as const;
export type CompilePhaseStatus = (typeof COMPILE_PHASE_STATUSES)[number];

export function isCompilePhaseStatus(value: unknown): value is CompilePhaseStatus {
  return typeof value === 'string' && (COMPILE_PHASE_STATUSES as readonly string[]).includes(value);
}

const PHASE_LABELS = {
  review: 'Review',
  scaffold: 'Scaffold',
  fill: 'Fill',
  boundary: 'Boundary check',
  validate: 'Validate',
} satisfies Record<CompilePhaseName, string>;

/** The LangGraph target's four extra phases (`compile/langgraph`). They are
 * not in `COMPILE_PHASE_NAMES` on purpose: a pydantic-graph compile never
 * emits them, and the row list appends unknown slugs after the five, which is
 * exactly their position (indices 6-9). */
export const LANGGRAPH_PHASE_NAMES = ['lg_scaffold', 'lg_convert', 'lg_boundary', 'lg_validate'] as const;
const LANGGRAPH_PHASE_LABELS: Record<(typeof LANGGRAPH_PHASE_NAMES)[number], string> = {
  lg_scaffold: 'LangGraph scaffold',
  lg_convert: 'LangGraph convert',
  lg_boundary: 'LangGraph boundary check',
  lg_validate: 'LangGraph validate',
};

/** A human label for a phase slug; an unknown slug (a future phase) is shown
 * as itself rather than dropped. */
export function phaseLabel(name: string): string {
  if (isCompilePhaseName(name)) return PHASE_LABELS[name];
  if ((LANGGRAPH_PHASE_NAMES as readonly string[]).includes(name)) {
    return LANGGRAPH_PHASE_LABELS[name as (typeof LANGGRAPH_PHASE_NAMES)[number]];
  }
  return name;
}

const PHASE_STATUS_LABELS = {
  started: 'running',
  succeeded: 'done',
  failed: 'failed',
} satisfies Record<CompilePhaseStatus, string>;

/** A human label for a phase status. `null` means "the stream has not
 * reported this phase yet", which is a UI state, not a wire value. */
export function phaseStatusLabel(status: CompilePhaseStatus | null): string {
  return status === null ? 'pending' : PHASE_STATUS_LABELS[status];
}

/** Job statuses that still have events ahead of them (`routes/compile.py`'s
 * own `_LIVE_JOB_STATUSES`). A job in any other status is terminal, so its
 * retained log is already its complete log. */
export function isLiveCompileStatus(status: CompileJobStatus | 'idle'): boolean {
  return status === 'queued' || status === 'running';
}

// ---------------------------------------------------------------------------
// One dispatched frame
// ---------------------------------------------------------------------------

/** One dispatched SSE frame as `api/client.ts` hands it over: the frame id to
 * echo back as `Last-Event-ID`, the `event:` name, and the *parsed* `data`
 * payload. `data` stays `unknown` -- the readers below are the only place it
 * is narrowed, and they always produce the human-readable message rather than
 * a stringified object. */
export interface CompileSseEvent {
  id: string;
  type: string;
  data: unknown;
}

// ---------------------------------------------------------------------------
// Narrowing primitives
// ---------------------------------------------------------------------------

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

/** Render a payload this build does not recognise without ever producing
 * "[object Object]": objects and arrays are shown as their JSON text, which
 * is at worst verbose but always informative. */
function describePayload(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return '[unreadable payload]';
    }
  }
  return String(value);
}

// ---------------------------------------------------------------------------
// Payload readers
// ---------------------------------------------------------------------------

/** A `phase` frame (`{name, index, total, status, ...}`). `null` for anything
 * that is not a phase transition this UI knows how to show -- including a
 * status outside the server's three-value vocabulary, which is a frame to
 * ignore rather than to render as a phase that is somehow both started and
 * pending. */
export interface CompilePhasePayload {
  name: string;
  status: CompilePhaseStatus;
  index: number | null;
}

export function readPhasePayload(data: unknown): CompilePhasePayload | null {
  const record = asRecord(data);
  if (record === null) return null;
  const name = asString(record.name);
  if (name === null || !isCompilePhaseStatus(record.status)) return null;
  return {
    name,
    status: record.status,
    index: typeof record.index === 'number' ? record.index : null,
  };
}

/** One `log` frame's text. A `log` frame is normally `{message}`, but it also
 * carries a Phase-1 *error* finding as `{message, code, nodeIds,
 * severity: "error"}` (the SSE vocabulary has no per-finding error type), so
 * both shapes are kept. */
export interface CompileLogLine {
  message: string;
  code: string | null;
  severity: 'error' | 'warning' | null;
  nodeIds: string[];
}

export function readLogLine(data: unknown): CompileLogLine {
  const record = asRecord(data);
  if (record === null) {
    return { message: describePayload(data), code: null, severity: null, nodeIds: [] };
  }
  const severity = asString(record.severity);
  return {
    message: asString(record.message) ?? describePayload(data),
    code: asString(record.code),
    severity: severity === 'error' || severity === 'warning' ? severity : null,
    nodeIds: asStringArray(record.nodeIds),
  };
}

/** The one-line rendering of a `log` frame, so a finding riding a `log` frame
 * still shows its code (and the nodes it names) instead of losing them. */
export function formatLogLine(line: CompileLogLine): string {
  const prefix = line.code === null ? '' : `${line.code}: `;
  const suffix = line.nodeIds.length === 0 ? '' : ` (nodes: ${line.nodeIds.join(', ')})`;
  return `${prefix}${line.message}${suffix}`;
}

/** A non-blocking `warning` frame. Its payload is exactly a review finding's
 * shape ({code, message, nodeIds}), so it is narrowed straight into the
 * generated `FindingOut` type the review pane already renders. */
export function readWarningPayload(data: unknown): FindingOut | null {
  const record = asRecord(data);
  if (record === null) return null;
  const code = asString(record.code);
  const message = asString(record.message);
  if (code === null || message === null) return null;
  return { code, message, nodeIds: asStringArray(record.nodeIds) };
}

/** The terminal `error` frame (`{code, message, exception}`; `code` is always
 * `phase_failed`). Never a stringified object, whatever arrives. */
export interface CompileErrorPayload {
  code: string | null;
  message: string;
  exception: string | null;
}

export function readErrorPayload(data: unknown): CompileErrorPayload {
  const record = asRecord(data);
  if (record === null) {
    return { code: null, message: describePayload(data), exception: null };
  }
  return {
    code: asString(record.code),
    message: asString(record.message) ?? describePayload(data),
    exception: asString(record.exception),
  };
}

/** The success payload of a `done` frame -- and, identically, the snapshot's
 * `result` (docs/api.md: "the full success payload, identical to `result` in
 * the status snapshot"). `projectPath`/`runCommand` are the two fields that
 * make a result something a user can act on, so their absence means the
 * payload is not a compile result at all and `null` is returned instead of a
 * half-empty one. */
export interface CompileResult {
  projectPath: string;
  runCommand: string;
  /** The golden diagram Phase 2 captured (`diagram` on the wire -- the field
   * is *not* called `renderedDiagram`). */
  diagram: string | null;
  filledNodeIds: string[];
  attempts: number;
  /** The route the compile actually spent, with its source -- so the panel can
   * show which model ran instead of leaving it to the log tail alone. */
  model: ResolvedDefaultOut | null;
  /** Present only for a `target: "langgraph"` compile: the export produced by
   * phases 6-9 (`compile/langgraph`). */
  langgraph: LangGraphResult | null;
}

export interface LangGraphResult {
  projectPath: string;
  runCommand: string;
  diagram: string | null;
  convertedNodeIds: string[];
  attempts: number;
}

function parseLangGraphResult(value: unknown): LangGraphResult | null {
  const record = asRecord(value);
  if (record === null) return null;
  const projectPath = asString(record.projectPath);
  const runCommand = asString(record.runCommand);
  if (projectPath === null || runCommand === null) return null;
  return {
    projectPath,
    runCommand,
    diagram: asString(record.diagram),
    convertedNodeIds: asStringArray(record.convertedNodeIds),
    attempts: typeof record.attempts === 'number' ? record.attempts : 0,
  };
}

function parseCompileResultModel(value: unknown): ResolvedDefaultOut | null {
  const record = asRecord(value);
  if (record === null) return null;
  const provider = asString(record.provider);
  const model = asString(record.model);
  const source = asString(record.source);
  if (provider === null || model === null || source === null) return null;
  return { provider, model, source };
}

export function parseCompileResult(data: unknown): CompileResult | null {
  const record = asRecord(data);
  if (record === null) return null;
  const projectPath = asString(record.projectPath);
  const runCommand = asString(record.runCommand);
  if (projectPath === null || runCommand === null) return null;
  return {
    projectPath,
    runCommand,
    diagram: asString(record.diagram),
    filledNodeIds: asStringArray(record.filledNodeIds),
    attempts: typeof record.attempts === 'number' ? record.attempts : 0,
    model: parseCompileResultModel(record.model),
    langgraph: parseLangGraphResult(record.langgraph),
  };
}

// ---------------------------------------------------------------------------
// Resume decision
// ---------------------------------------------------------------------------

/** A snapshot's `latestEventId` as the cursor string a client hands back as
 * `Last-Event-ID`; `null` for a job with an empty log. */
export function resumeCursor(snapshot: CompileSnapshot): string | null {
  return snapshot.latestEventId === null ? null : String(snapshot.latestEventId);
}

/**
 * Whether a job's retained log still holds frames this client has not seen --
 * i.e. whether resuming the stream would be served anything at all.
 *
 * A terminal job (docs/api.md: "a job that already reached a terminal state
 * retains its *complete* log, so its whole response is a replay") sends frames
 * only if the cursor is older than its newest retained event. Resuming at the
 * cursor a client already consumed therefore yields an *empty* stream, which
 * this client would treat as a lost connection and retry -- five times, then
 * report a failure for a compile that actually succeeded. Asking this question
 * first is what keeps a post-finish reload from inventing an error.
 *
 * The ids compared here are the server's own monotonic integers (docs/api.md
 * "Framing": "the event's own monotonic integer"), which is the one place the
 * `Last-Event-ID` contract is numeric. A cursor that cannot be read as an
 * integer is treated as "subscribe anyway": the cost of being wrong that way
 * is an empty replay, never a missed event.
 */
export function hasUnseenEvents(snapshot: CompileSnapshot, cursor: string | null): boolean {
  const latest = snapshot.latestEventId;
  if (latest === null) return false;
  if (cursor === null) return true;
  const cursorId = Number.parseInt(cursor, 10);
  if (!Number.isInteger(cursorId)) return true;
  return cursorId < latest;
}
