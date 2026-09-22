// The run stream's wire vocabulary, narrowed once -- the sibling of
// `compileWire.ts` for jobs of kind `run` (`compile/run.py`).
//
// A run job reuses the compile job's transport (same registry, same SSE
// framing, same `Last-Event-ID` replay) and adds two frame types: `run`
// (`{status: "compiling" | "starting" | "started", model?, input?,
// compiled?}`) and `node` (`{nodeId, status: "started" | "succeeded" |
// "failed", inputs?, output?, stateDelta?, error?, traceback?,
// durationMs?}`). Its terminal `done` payload is `{output, state,
// durationMs, model, compiled}` -- a different shape from a compile's, so it
// gets its own parser. `phase`/`log`/`warning`/`error` frames are the
// compile's and are read with `compileWire.ts`'s readers.

export const RUN_EVENT_TYPES = ['run', 'node'] as const;
export type RunSseEventType = (typeof RUN_EVENT_TYPES)[number];

/** `run` frame statuses, in emission order (`compile/run.py`
 * `RUN_STATUS_*`). */
export const RUN_STAGES = ['compiling', 'starting', 'started'] as const;
export type RunStage = (typeof RUN_STAGES)[number];

export function isRunStage(value: unknown): value is RunStage {
  return typeof value === 'string' && (RUN_STAGES as readonly string[]).includes(value);
}

/** `node` frame statuses (`compile/run.py` `NODE_STATUS_*`). */
export const RUN_NODE_STATUSES = ['started', 'succeeded', 'failed'] as const;
export type RunNodeStatus = (typeof RUN_NODE_STATUSES)[number];

export function isRunNodeStatus(value: unknown): value is RunNodeStatus {
  return typeof value === 'string' && (RUN_NODE_STATUSES as readonly string[]).includes(value);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function asInt(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export interface RunStagePayload {
  status: RunStage;
  model: string | null;
  input: unknown;
  compiled: boolean;
}

export function readRunStagePayload(data: unknown): RunStagePayload | null {
  const record = asRecord(data);
  if (record === null || !isRunStage(record.status)) return null;
  return {
    status: record.status,
    model: asString(record.model),
    input: 'input' in record ? record.input : undefined,
    compiled: record.compiled === true,
  };
}

export interface RunNodePayload {
  nodeId: string;
  status: RunNodeStatus;
  /** `undefined` when the frame did not carry the field (a `started` frame
   * has inputs but no output; a `succeeded` frame the reverse). */
  inputs: unknown;
  output: unknown;
  stateDelta: Record<string, unknown> | null;
  error: string | null;
  traceback: string | null;
  durationMs: number | null;
}

export function readRunNodePayload(data: unknown): RunNodePayload | null {
  const record = asRecord(data);
  if (record === null) return null;
  const nodeId = asString(record.nodeId);
  if (nodeId === null || !isRunNodeStatus(record.status)) return null;
  return {
    nodeId,
    status: record.status,
    inputs: 'inputs' in record ? record.inputs : undefined,
    output: 'output' in record ? record.output : undefined,
    stateDelta: asRecord(record.stateDelta),
    error: asString(record.error),
    traceback: asString(record.traceback),
    durationMs: asInt(record.durationMs),
  };
}

/** The run's terminal `done` payload, and identically the snapshot `result`
 * of a finished run job. `state` is the final `State` dataclass as a dict. */
export interface RunResult {
  output: unknown;
  state: Record<string, unknown>;
  durationMs: number | null;
  model: string | null;
  compiled: boolean;
}

export function parseRunResult(data: unknown): RunResult | null {
  const record = asRecord(data);
  if (record === null || !('output' in record) || asRecord(record.state) === null) return null;
  return {
    output: record.output,
    state: asRecord(record.state) ?? {},
    durationMs: asInt(record.durationMs),
    model: asString(record.model),
    compiled: record.compiled === true,
  };
}

/** The tracer truncates long values to `{__preview__, __truncated__}`
 * (`scaffold.py`'s `_render`). Render that -- and anything else -- as text
 * without ever producing "[object Object]". */
export function formatRunValue(value: unknown): string {
  if (value === undefined) return '';
  if (typeof value === 'string') return value;
  const record = asRecord(value);
  if (record !== null && typeof record.__preview__ === 'string') {
    return record.__truncated__ === true ? `${record.__preview__}\n… (truncated)` : record.__preview__;
  }
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}
