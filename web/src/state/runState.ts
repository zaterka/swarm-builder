// The run sub-state and every transition that touches it, as pure functions
// -- `compileState.ts`'s sibling for jobs of kind `run`.
//
// Same invariants: a snapshot never blanks the per-node trace, the log or
// the compile phases (it does not carry them; the replayed stream refills
// them), and every payload is narrowed through `api/runWire.ts` or
// `api/compileWire.ts` before it becomes state.
import type { CompileJobStatus, CompileSnapshot, RunRecord, SwarmEdge } from '../api/schema';
import type { CompileSseEvent } from '../api/compileWire';
import {
  formatLogLine,
  isLiveCompileStatus,
  readErrorPayload,
  readLogLine,
  resumeCursor,
} from '../api/compileWire';
import type { RunNodeStatus, RunResult, RunStage } from '../api/runWire';
import { parseRunResult, readRunNodePayload, readRunStagePayload } from '../api/runWire';
import type { CompilePhase } from './compileState';
import { foldPhaseEvent, MAX_LOG_LINES } from './compileState';

export interface RunNodeState {
  status: RunNodeStatus;
  inputs: unknown;
  output: unknown;
  stateDelta: Record<string, unknown> | null;
  error: string | null;
  traceback: string | null;
  durationMs: number | null;
  /** 1-based order in which the node first started, for a stable trace list. */
  order: number;
}

/** Where a run job is, as reported by its `run` frames plus the terminal
 * event. `idle` before the first frame; `finished` after `done`/`error`. */
export type RunProgress = 'idle' | RunStage | 'finished';

export interface RunState {
  runId: string | null;
  status: CompileJobStatus | 'idle';
  progress: RunProgress;
  /** Phase rows of a compile-if-stale that preceded the run, if any. */
  compilePhases: CompilePhase[];
  nodes: Record<string, RunNodeState>;
  logLines: string[];
  result: RunResult | null;
  error: string | null;
  model: string | null;
  input: unknown;
  latestEventId: string | null;
}

export function initialRunState(): RunState {
  return {
    runId: null,
    status: 'idle',
    progress: 'idle',
    compilePhases: [],
    nodes: {},
    logLines: [],
    result: null,
    error: null,
    model: null,
    input: undefined,
    latestEventId: null,
  };
}

/** Field-by-field merge, for the same reason `mergeCompilePatch` spells its
 * fields out: an explicit `undefined` must keep the existing value. */
export function mergeRunPatch(state: RunState, patch: Partial<RunState>): RunState {
  return {
    ...state,
    runId: patch.runId === undefined ? state.runId : patch.runId,
    status: patch.status ?? state.status,
    progress: patch.progress ?? state.progress,
    compilePhases: patch.compilePhases ?? state.compilePhases,
    nodes: patch.nodes ?? state.nodes,
    logLines: patch.logLines ?? state.logLines,
    result: patch.result === undefined ? state.result : patch.result,
    error: patch.error === undefined ? state.error : patch.error,
    model: patch.model === undefined ? state.model : patch.model,
    input: 'input' in patch ? patch.input : state.input,
    latestEventId: patch.latestEventId === undefined ? state.latestEventId : patch.latestEventId,
  };
}

export function applyRunSnapshot(state: RunState, snapshot: CompileSnapshot): RunState {
  const result = parseRunResult(snapshot.result);
  return {
    ...state,
    runId: snapshot.compileId,
    status: snapshot.status,
    result,
    error: snapshot.error,
    latestEventId: resumeCursor(snapshot),
    progress: isLiveCompileStatus(snapshot.status) ? state.progress : 'finished',
    model: result?.model ?? state.model,
    compilePhases: state.compilePhases,
    nodes: state.nodes,
    logLines: state.logLines,
  };
}

function appendLog(state: RunState, line: string): RunState {
  return { ...state, logLines: [...state.logLines, line].slice(-MAX_LOG_LINES) };
}

function applyStage(state: RunState, data: unknown): RunState {
  const payload = readRunStagePayload(data);
  if (payload === null) return state;
  return {
    ...state,
    progress: payload.status,
    model: payload.model ?? state.model,
    input: payload.input === undefined ? state.input : payload.input,
  };
}

function applyNode(state: RunState, data: unknown): RunState {
  const payload = readRunNodePayload(data);
  if (payload === null) return state;
  const existing = state.nodes[payload.nodeId];
  const order = existing?.order ?? Object.keys(state.nodes).length + 1;
  const next: RunNodeState = {
    status: payload.status,
    inputs: payload.inputs === undefined ? existing?.inputs : payload.inputs,
    output: payload.output === undefined ? existing?.output : payload.output,
    stateDelta: payload.stateDelta ?? existing?.stateDelta ?? null,
    error: payload.error ?? (payload.status === 'failed' ? existing?.error ?? null : null),
    traceback: payload.traceback ?? existing?.traceback ?? null,
    durationMs: payload.durationMs ?? existing?.durationMs ?? null,
    order,
  };
  return { ...state, nodes: { ...state.nodes, [payload.nodeId]: next } };
}

/** Fold one dispatched SSE frame into run state. */
export function applyRunEvent(state: RunState, event: CompileSseEvent): RunState {
  const current: RunState = state.status === 'queued' ? { ...state, status: 'running' } : state;
  switch (event.type) {
    case 'run':
      return applyStage(current, event.data);
    case 'node':
      return applyNode(current, event.data);
    case 'phase':
      return { ...current, compilePhases: foldPhaseEvent(current.compilePhases, event.data) };
    case 'log':
    case 'warning':
      return appendLog(current, formatLogLine(readLogLine(event.data)));
    case 'done': {
      const result = parseRunResult(event.data);
      if (result === null) {
        return appendLog(
          { ...current, status: 'succeeded', progress: 'finished' },
          formatLogLine(readLogLine(event.data)),
        );
      }
      return {
        ...current,
        status: 'succeeded',
        progress: 'finished',
        result,
        error: null,
        model: result.model ?? current.model,
      };
    }
    case 'error':
      return {
        ...current,
        status: 'failed',
        progress: 'finished',
        error: readErrorPayload(event.data).message,
      };
    default:
      return state;
  }
}

/** Transport-level stream failure: only a live run degrades to `failed`. */
export function applyRunStreamError(state: RunState, message: string): RunState {
  if (!isLiveCompileStatus(state.status)) return state;
  return { ...state, status: 'failed', progress: 'finished', error: message };
}

/** Rebuild run state from a persisted record (`GET /api/graphs/:id/runs/:runId`),
 * for browsing history. The record is complete, so this replaces everything. */
export function runStateFromRecord(record: RunRecord): RunState {
  const nodes: Record<string, RunNodeState> = {};
  let order = 0;
  for (const [nodeId, node] of Object.entries(record.nodes ?? {})) {
    order += 1;
    nodes[nodeId] = {
      status: (['started', 'succeeded', 'failed'] as const).includes(node.status as RunNodeStatus)
        ? (node.status as RunNodeStatus)
        : 'failed',
      inputs: node.inputs,
      output: node.output,
      stateDelta: (node.stateDelta as Record<string, unknown> | null | undefined) ?? null,
      error: node.error ?? null,
      traceback: null,
      durationMs: node.durationMs ?? null,
      order,
    };
  }
  const status = (['queued', 'running', 'succeeded', 'failed', 'cancelled'] as const).includes(
    record.status as CompileJobStatus,
  )
    ? (record.status as CompileJobStatus)
    : 'failed';
  const result: RunResult | null =
    record.status === 'succeeded'
      ? {
          output: record.output,
          state: (record.state as Record<string, unknown> | null | undefined) ?? {},
          durationMs: record.durationMs ?? null,
          model: record.model ?? null,
          compiled: record.compiled ?? false,
        }
      : null;
  return {
    runId: record.runId,
    status,
    progress: isLiveCompileStatus(status) ? 'started' : 'finished',
    compilePhases: [],
    nodes,
    logLines: [],
    result,
    error: record.error ?? null,
    model: record.model ?? null,
    input: record.input,
    latestEventId: null,
  };
}

// ---------------------------------------------------------------------------
// Canvas projection
// ---------------------------------------------------------------------------

/** What the canvas paints on a node. `idle` is "not part of this run (yet)". */
export type RunNodeDisplayStatus = 'idle' | 'running' | 'succeeded' | 'failed';

const STRUCTURAL_EDGE_KINDS: ReadonlySet<SwarmEdge['kind']> = new Set(['seq', 'fanout', 'join', 'branch']);

/**
 * Per-node display status for the whole graph.
 *
 * Step nodes (`agent`/`programmatic`) are traced directly by the tracer.
 * `decision` and `join` nodes are pydantic-graph builder constructs with no
 * step function to wrap, so their status is *derived*: a decision or join
 * has succeeded once any of its dispatch successors has started (or the run
 * finished successfully), and is running once any of its predecessors has
 * succeeded while the run is still live.
 */
export function deriveRunDisplayStatuses(
  state: RunState,
  nodes: readonly { id: string; kind: string }[],
  edges: readonly SwarmEdge[],
): Record<string, RunNodeDisplayStatus> {
  const result: Record<string, RunNodeDisplayStatus> = {};
  if (state.status === 'idle') {
    for (const node of nodes) result[node.id] = 'idle';
    return result;
  }
  const live = isLiveCompileStatus(state.status);
  const finishedOk = state.status === 'succeeded';

  const successors = new Map<string, string[]>();
  const predecessors = new Map<string, string[]>();
  for (const edge of edges) {
    if (!STRUCTURAL_EDGE_KINDS.has(edge.kind)) continue;
    successors.set(edge.source, [...(successors.get(edge.source) ?? []), edge.target]);
    predecessors.set(edge.target, [...(predecessors.get(edge.target) ?? []), edge.source]);
  }

  const traced = (id: string): RunNodeDisplayStatus => {
    const trace = state.nodes[id];
    if (trace === undefined) return 'idle';
    return trace.status === 'started' ? 'running' : trace.status;
  };

  for (const node of nodes) {
    if (node.kind !== 'decision' && node.kind !== 'join') {
      result[node.id] = traced(node.id);
    }
  }
  for (const node of nodes) {
    if (node.kind !== 'decision' && node.kind !== 'join') continue;
    const succ = successors.get(node.id) ?? [];
    const pred = predecessors.get(node.id) ?? [];
    const anySuccessorStarted = succ.some((id) => state.nodes[id] !== undefined);
    const anyPredecessorDone = pred.some((id) => state.nodes[id]?.status === 'succeeded');
    if (anySuccessorStarted || (finishedOk && anyPredecessorDone)) {
      result[node.id] = 'succeeded';
    } else if (live && anyPredecessorDone) {
      result[node.id] = 'running';
    } else {
      result[node.id] = 'idle';
    }
  }
  return result;
}

/** The traced nodes in the order they first started. */
export function runTraceRows(state: RunState): Array<RunNodeState & { nodeId: string }> {
  return Object.entries(state.nodes)
    .map(([nodeId, node]) => ({ nodeId, ...node }))
    .sort((a, b) => a.order - b.order);
}
