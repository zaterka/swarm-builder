// The compile sub-state and every transition that touches it, as pure
// functions.
//
// Kept out of `graphStore.ts` for one reason: these are the transitions a
// reload, a reconnect and a live stream all go through, and a pure function is
// the only version of them that can be tested against the server's real
// payloads without mounting a component or a store (see
// `compileState.test.ts`).
//
// The invariants this module exists to hold:
//
//   * the snapshot (`GET /api/compile/{compileId}`) carries `status`,
//     `result`, `error` and `latestEventId` -- it does **not** carry `phases`,
//     `logTail` or `warnings`. A snapshot therefore never *overwrites* the
//     phase list, the log or the warnings; the SSE replay (with
//     `Last-Event-ID`) is what refills them. Nothing here can set any of those
//     three to `undefined`, which is what used to make the next
//     `[...logLines, line]` or `logLines.join('\n')` throw after a reload.
//   * every payload is narrowed through `api/compileWire.ts`, so a streamed
//     log entry is the server's own message and never `[object Object]`.
import type { CompileJobStatus, CompileSnapshot, FindingOut } from '../api/schema';
import type { CompilePhaseStatus, CompileResult, CompileSseEvent } from '../api/compileWire';
import {
  COMPILE_PHASE_NAMES,
  formatLogLine,
  isCompileEventType,
  isCompilePhaseName,
  isLiveCompileStatus,
  parseCompileResult,
  phaseLabel,
  phaseStatusLabel,
  readErrorPayload,
  readLogLine,
  readPhasePayload,
  readWarningPayload,
  resumeCursor,
} from '../api/compileWire';

/** How many streamed log lines the panel keeps. The server's ring buffer holds
 * 2000 events; the panel is a tail, not an archive. */
export const MAX_LOG_LINES = 500;

/** One phase as the stream reported it. `attempt` counts `started` events:
 * 1 on the first pass, 2+ when the pipeline retried the phase (docs/api.md: "A
 * retried Phase 3 shows up as a second `started` for `fill`, which is how a UI
 * renders 'retrying'"). */
export interface CompilePhase {
  name: string;
  status: CompilePhaseStatus;
  attempt: number;
}

export interface CompileState {
  compileId: string | null;
  /** The *job* status (`queued`/`running`/`succeeded`/`failed`/`cancelled`),
   * plus the client-side `idle` for "no compile has been started in this
   * session". Distinct from a phase's `CompilePhaseStatus`. */
  status: CompileJobStatus | 'idle';
  phases: CompilePhase[];
  logLines: string[];
  warnings: FindingOut[];
  result: CompileResult | null;
  error: string | null;
  /** The server's `latestEventId`, as the string this client hands back as
   * `Last-Event-ID`. `null` for a job with an empty log. */
  latestEventId: string | null;
}

export function initialCompileState(): CompileState {
  return {
    compileId: null,
    status: 'idle',
    phases: [],
    logLines: [],
    warnings: [],
    result: null,
    error: null,
    latestEventId: null,
  };
}

/**
 * Merge a partial update into compile state.
 *
 * Spelled out field by field rather than with `{ ...state, ...patch }` for the
 * reason the old code needed it: under this project's tsconfig an explicit
 * `{ logLines: undefined }` is a legal `Partial<CompileState>`, and spreading
 * it would hand the next reader an `undefined` where it expects an array --
 * which is exactly how a mid-compile reload used to turn the log tail into a
 * crash. Here an absent (or explicitly `undefined`) field always keeps the
 * value the state already had.
 */
export function mergeCompilePatch(state: CompileState, patch: Partial<CompileState>): CompileState {
  return {
    ...state,
    compileId: patch.compileId === undefined ? state.compileId : patch.compileId,
    status: patch.status ?? state.status,
    phases: patch.phases ?? state.phases,
    logLines: patch.logLines ?? state.logLines,
    warnings: patch.warnings ?? state.warnings,
    result: patch.result === undefined ? state.result : patch.result,
    error: patch.error === undefined ? state.error : patch.error,
    latestEventId: patch.latestEventId === undefined ? state.latestEventId : patch.latestEventId,
  };
}

/**
 * Rebuild what a reloaded page can know from the status snapshot alone.
 *
 * `phases`, `logLines` and `warnings` are deliberately left untouched: the
 * snapshot does not carry them (the server's `Job.snapshot()` returns
 * `compileId`, `graphId`, `status`, the timestamps, `latestEventId`, `result`
 * and `error`), and the resubscribed stream refills them from the retained
 * ring buffer. `latestEventId` becomes the reconnect cursor.
 */
export function applyCompileSnapshot(state: CompileState, snapshot: CompileSnapshot): CompileState {
  return {
    ...state,
    compileId: snapshot.compileId,
    status: snapshot.status,
    result: parseCompileResult(snapshot.result),
    error: snapshot.error,
    latestEventId: resumeCursor(snapshot),
    // Named explicitly rather than left to the spread above: these three are
    // the fields a snapshot must never blank, so leaving them implicit is what
    // let a reload write `undefined` over them in the first place.
    phases: state.phases,
    logLines: state.logLines,
    warnings: state.warnings,
  };
}

function appendLog(state: CompileState, line: string): CompileState {
  return { ...state, logLines: [...state.logLines, line].slice(-MAX_LOG_LINES) };
}

function applyPhaseEvent(state: CompileState, data: unknown): CompileState {
  const payload = readPhasePayload(data);
  if (payload === null) return state;

  const existing = state.phases.find((phase) => phase.name === payload.name);
  if (existing === undefined) {
    // First sighting of this phase. A phase streamed for the first time during
    // a replay arrives here exactly like a live one.
    return { ...state, phases: [...state.phases, { name: payload.name, status: payload.status, attempt: 1 }] };
  }

  // A repeated `started` for a phase that already exists is a *retry*, not a
  // second phase: keep one row, count the attempt, and let the row show that
  // it is running again.
  const attempt = payload.status === 'started' ? existing.attempt + 1 : existing.attempt;
  const updated: CompilePhase = { name: payload.name, status: payload.status, attempt };
  return {
    ...state,
    phases: state.phases.map((phase) => (phase.name === payload.name ? updated : phase)),
  };
}

function applyWarningEvent(state: CompileState, data: unknown): CompileState {
  const warning = readWarningPayload(data);
  if (warning !== null) {
    return { ...state, warnings: [...state.warnings, warning] };
  }
  // A `warning` frame that is not the documented {code, message, nodeIds}
  // shape is still shown -- as a log line, which is where free-form text
  // belongs -- rather than dropped or rendered as "[object Object]".
  return appendLog(state, formatLogLine(readLogLine(data)));
}

function applyDoneEvent(state: CompileState, data: unknown): CompileState {
  const result = parseCompileResult(data);
  if (result === null) {
    // Succeeded, but with a payload this build cannot read: say so, and keep
    // the raw payload visible instead of showing an empty result pane.
    return appendLog({ ...state, status: 'succeeded' }, formatLogLine(readLogLine(data)));
  }
  return { ...state, status: 'succeeded', result, error: null };
}

/**
 * Fold one dispatched SSE frame into compile state.
 *
 * The only place a streamed payload becomes state, so it is also the only
 * place the `[object Object]` class of bug could live: every branch goes
 * through a reader in `api/compileWire.ts` first.
 */
export function applyCompileEvent(state: CompileState, event: CompileSseEvent): CompileState {
  if (!isCompileEventType(event.type)) {
    // An event name this build does not know. Ignoring it is safer than
    // guessing: the frame is not part of the documented vocabulary.
    return state;
  }

  // Any documented event proves the job is past `queued` -- the client wrote
  // that status optimistically when `POST /api/compile` returned, and the
  // stream is the first real evidence about the job's progress.
  const current: CompileState = state.status === 'queued' ? { ...state, status: 'running' } : state;

  switch (event.type) {
    case 'phase':
      return applyPhaseEvent(current, event.data);
    case 'log':
      return appendLog(current, formatLogLine(readLogLine(event.data)));
    case 'warning':
      return applyWarningEvent(current, event.data);
    case 'done':
      return applyDoneEvent(current, event.data);
    case 'error':
      return { ...current, status: 'failed', error: readErrorPayload(event.data).message };
  }
}

/**
 * Record a transport-level stream failure.
 *
 * Only a *live* job degrades to `failed`. A job the snapshot or the stream
 * already declared terminal stays terminal: the server closes the stream on
 * the terminal frame (docs/api.md: "The response is deliberately finite"), so
 * a reader that ends -- or a reconnect that never gets another frame after
 * that -- is the expected end of a finished compile, not a failed one.
 */
export function applyCompileStreamError(state: CompileState, message: string): CompileState {
  if (!isLiveCompileStatus(state.status)) return state;
  return { ...state, status: 'failed', error: message };
}

/** One rendered phase row: the five known phases in pipeline order (so the
 * list reads as the pipeline, not as "whatever has been reported so far"),
 * then any unknown slug a future server sends. */
export interface CompilePhaseRow {
  name: string;
  label: string;
  status: CompilePhaseStatus | null;
  statusLabel: string;
  attempt: number;
  /** 1-based position within the rendered list, for a stable React key. */
  position: number;
}

export function compilePhaseRows(phases: CompilePhase[]): CompilePhaseRow[] {
  const reported = new Map(phases.map((phase) => [phase.name, phase]));

  function toRow(name: string, phase: CompilePhase | undefined, position: number): CompilePhaseRow {
    const status = phase?.status ?? null;
    return {
      name,
      label: phaseLabel(name),
      status,
      statusLabel: phaseStatusLabel(status),
      attempt: phase?.attempt ?? 0,
      position,
    };
  }

  const known = COMPILE_PHASE_NAMES.map((name, index) => toRow(name, reported.get(name), index + 1));
  const unknown = phases
    .filter((phase) => !isCompilePhaseName(phase.name))
    .map((phase, index) => toRow(phase.name, phase, known.length + index + 1));

  return [...known, ...unknown];
}
