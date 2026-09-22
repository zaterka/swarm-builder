import { useEffect, useRef, useState } from 'react';
import { useGraphStore } from '../state/graphStore';
import { api, ApiError, CompileConflictError } from '../api/client';
import type { HealthResponse, RunRecord } from '../api/schema';
import { hasUnseenEvents, isLiveCompileStatus, resumeCursor } from '../api/compileWire';
import { formatRunValue } from '../api/runWire';
import { compilePhaseRows } from '../state/compileState';
import { runTraceRows } from '../state/runState';
import { formatRelativeTime } from '../relativeTime';

const SESSION_KEY_PREFIX = 'swarm-builder:run:';

interface StoredRun {
  runId: string;
  lastEventId?: string;
}

function sessionKey(graphId: string): string {
  return `${SESSION_KEY_PREFIX}${graphId}`;
}

function readStoredRun(graphId: string): StoredRun | null {
  const raw = sessionStorage.getItem(sessionKey(graphId));
  if (raw === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    sessionStorage.removeItem(sessionKey(graphId));
    return null;
  }
  if (typeof parsed !== 'object' || parsed === null) return null;
  const record = parsed as Record<string, unknown>;
  if (typeof record.runId !== 'string') return null;
  return {
    runId: record.runId,
    lastEventId: typeof record.lastEventId === 'string' ? record.lastEventId : undefined,
  };
}

const INPUT_PLACEHOLDERS = {
  str: 'Type the workflow input…',
  json: '{"key": "value"}',
  'list[str]': '["first", "second"]',
} as const;

const PROGRESS_LABELS = {
  idle: '',
  compiling: 'Compiling the project first (it is missing or older than the graph)…',
  starting: 'Starting the workflow…',
  started: 'Running…',
  finished: '',
} as const;

/**
 * Run the compiled workflow with real credentials and watch it node by node
 * (PLAN-V2-FEATURES.md, Feature 1). Input box -> Run -> streamed trace ->
 * output/state -> history. Reload-resume mirrors `CompilePanel`: the run id
 * and last seen event id live in `sessionStorage`, snapshot first, then
 * resubscribe with `Last-Event-ID`.
 */
export function RunPanel() {
  const graph = useGraphStore((s) => s.graph);
  const run = useGraphStore((s) => s.run);
  const setRunState = useGraphStore((s) => s.setRunState);
  const resetRunState = useGraphStore((s) => s.resetRunState);
  const applyRunEvent = useGraphStore((s) => s.applyRunEvent);
  const applyRunSnapshot = useGraphStore((s) => s.applyRunSnapshot);
  const markRunStreamError = useGraphStore((s) => s.markRunStreamError);
  const loadRunRecord = useGraphStore((s) => s.loadRunRecord);
  const selectNode = useGraphStore((s) => s.selectNode);

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [input, setInput] = useState('');
  const [history, setHistory] = useState<RunRecord[]>([]);
  const [startError, setStartError] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);

  const unsubscribeRef = useRef<(() => void) | null>(null);

  const refreshHealth = () => {
    api.health().then(setHealth).catch(() => setHealth(null));
  };

  const refreshHistory = (graphId: string) => {
    api
      .listRuns(graphId)
      .then((response) => setHistory(response.runs))
      .catch(() => setHistory([]));
  };

  useEffect(() => {
    refreshHealth();
  }, []);

  useEffect(() => {
    if (graph) refreshHistory(graph.id);
  }, [graph?.id]);

  // A terminal event means the record on disk is complete: refresh the list.
  useEffect(() => {
    if (graph && (run.status === 'succeeded' || run.status === 'failed' || run.status === 'cancelled')) {
      refreshHistory(graph.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.status]);

  // Resume a persisted run on mount: snapshot, then subscribe if the stream
  // still has frames this client has not seen.
  useEffect(() => {
    if (!graph) return;
    const graphId = graph.id;
    const stored = readStoredRun(graphId);
    if (!stored) return;
    const { runId, lastEventId } = stored;
    const storedCursor = lastEventId ?? null;
    setRunState({ runId, status: 'queued' });

    let cancelled = false;
    api
      .getJobSnapshot(runId)
      .then((snapshot) => {
        if (cancelled) return;
        applyRunSnapshot(snapshot);
        const cursor = storedCursor ?? resumeCursor(snapshot);
        if (isLiveCompileStatus(snapshot.status) || hasUnseenEvents(snapshot, cursor)) {
          subscribe(runId, cursor ?? undefined);
        } else {
          sessionStorage.removeItem(sessionKey(graphId));
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          sessionStorage.removeItem(sessionKey(graphId));
          setRunState({
            runId: null,
            status: 'idle',
            error: 'The previous run is no longer available on the server.',
          });
          return;
        }
        subscribe(runId, storedCursor ?? undefined);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph?.id]);

  useEffect(() => () => unsubscribeRef.current?.(), []);

  function subscribe(runId: string, lastEventId?: string) {
    unsubscribeRef.current?.();
    const close = api.subscribeJobEvents(runId, {
      lastEventId,
      onLastEventId: (id) => {
        if (graph) {
          sessionStorage.setItem(sessionKey(graph.id), JSON.stringify({ runId, lastEventId: id }));
        }
      },
      onEvent: (evt) => {
        applyRunEvent(evt);
        if (evt.type === 'done' || evt.type === 'error') {
          if (graph) sessionStorage.removeItem(sessionKey(graph.id));
        }
      },
      onError: (err) => {
        markRunStreamError(err.message);
      },
    });
    unsubscribeRef.current = close;
  }

  if (!graph) {
    return <div className="sb-compile-panel-empty">No graph loaded.</div>;
  }

  const entry = graph.nodes.find((n) => n.id === graph.entryNodeId);
  const inputType = entry?.io.inputType ?? 'str';
  const runReady = health?.runReady ?? false;
  const runBlockers = health?.runBlockers ?? [];
  const inFlight = isLiveCompileStatus(run.status);
  const traceRows = runTraceRows(run);
  const phaseRows = run.compilePhases.length > 0 ? compilePhaseRows(run.compilePhases) : [];
  const progressLabel = PROGRESS_LABELS[run.progress];

  const handleRun = async () => {
    setStartError(null);
    resetRunState();
    setRunState({ status: 'queued', input });
    try {
      const { runId } = await api.startRun(graph.id, input, true);
      setRunState({ runId });
      sessionStorage.setItem(sessionKey(graph.id), JSON.stringify({ runId }));
      subscribe(runId);
    } catch (err) {
      if (err instanceof CompileConflictError) {
        setRunState({ status: 'idle' });
        setStartError('A compile or run is already in progress for this graph.');
      } else if (err instanceof ApiError && err.status === 422) {
        setRunState({ status: 'idle' });
        setStartError(`Input rejected: ${String(err.detail)}`);
      } else {
        setRunState({ status: 'failed', error: err instanceof Error ? err.message : String(err) });
      }
    }
  };

  const handleCancel = async () => {
    const runId = run.runId;
    if (runId === null) return;
    try {
      applyRunSnapshot(await api.cancelJob(runId));
    } catch {
      setRunState({ status: 'cancelled', progress: 'finished' });
    }
    unsubscribeRef.current?.();
    sessionStorage.removeItem(sessionKey(graph.id));
  };

  const handleLoadRecord = async (record: RunRecord) => {
    if (inFlight) return;
    try {
      loadRunRecord(await api.getRun(graph.id, record.runId));
    } catch {
      loadRunRecord(record);
    }
  };

  return (
    <div className="sb-compile-panel sb-run-panel">
      <h3>Run</h3>

      <section>
        {!runReady && health && (
          <div className="sb-compile-blocked">
            <strong>Run disabled:</strong>
            <ul>
              {runBlockers.map((b, i) => (
                <li key={i}>{b}</li>
              ))}
            </ul>
          </div>
        )}
        {runReady && !acknowledged && (
          <div className="sb-hint sb-run-notice">
            Running executes the generated project on this machine with the credentials configured
            for the resolved model. Generated step bodies are model-written code.{' '}
            <button type="button" className="sb-btn-sm" onClick={() => setAcknowledged(true)}>
              Understood
            </button>
          </div>
        )}
        <label>
          Input for <code>{entry?.title ?? graph.entryNodeId}</code> ({inputType})
          <textarea
            className="sb-run-input"
            value={input}
            placeholder={INPUT_PLACEHOLDERS[inputType]}
            onChange={(e) => setInput(e.target.value)}
            disabled={inFlight}
            rows={inputType === 'str' ? 3 : 4}
          />
        </label>
        <div className="sb-action-row">
          <button
            className="sb-btn-primary"
            disabled={!runReady || inFlight || input.trim() === ''}
            onClick={handleRun}
          >
            Run
          </button>
          {inFlight && <button onClick={handleCancel}>Cancel</button>}
          {run.model && <span className="sb-hint">model: {run.model}</span>}
        </div>
        {startError && <div className="sb-finding sb-finding-error">{startError}</div>}
      </section>

      {run.status !== 'idle' && (
        <section>
          <h4>Trace</h4>
          {progressLabel && <div className="sb-hint">{progressLabel}</div>}
          {phaseRows.length > 0 && (
            <ol className="sb-phase-list">
              {phaseRows
                .filter((row) => row.status !== null)
                .map((row) => (
                  <li key={row.name} className={`sb-phase sb-phase-${row.status ?? 'pending'}`}>
                    {row.label}: {row.statusLabel}
                  </li>
                ))}
            </ol>
          )}
          <ol className="sb-run-trace">
            {traceRows.map((row) => {
              const title = graph.nodes.find((n) => n.id === row.nodeId)?.title ?? row.nodeId;
              return (
                <li key={row.nodeId} className={`sb-run-trace-row sb-run-${row.status}`}>
                  <button type="button" className="sb-run-trace-node" onClick={() => selectNode(row.nodeId)}>
                    {title}
                  </button>
                  <span className="sb-run-trace-status">{row.status === 'started' ? 'running' : row.status}</span>
                  {row.durationMs !== null && <span className="sb-run-trace-ms">{row.durationMs} ms</span>}
                  {row.error && <div className="sb-run-trace-error">{row.error}</div>}
                </li>
              );
            })}
          </ol>
          {traceRows.length === 0 && run.progress !== 'compiling' && (
            <div className="sb-hint">No steps have reported yet.</div>
          )}
          {run.logLines.length > 0 && (
            <details className="sb-run-log">
              <summary>Log ({run.logLines.length})</summary>
              <pre className="sb-log-tail" aria-live="polite" aria-label="Run log">
                {run.logLines.join('\n')}
              </pre>
            </details>
          )}
        </section>
      )}

      {run.result && (
        <section>
          <h4>Output</h4>
          <pre className="sb-run-output">{formatRunValue(run.result.output)}</pre>
          {Object.keys(run.result.state).length > 0 && (
            <>
              <h4>Final state</h4>
              <table className="sb-run-state">
                <tbody>
                  {Object.entries(run.result.state).map(([name, value]) => (
                    <tr key={name}>
                      <th>{name}</th>
                      <td>
                        <pre>{formatRunValue(value)}</pre>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          <div className="sb-hint">
            {run.result.durationMs !== null ? `${run.result.durationMs} ms` : ''}
            {run.result.model ? ` — ${run.result.model}` : ''}
            {run.result.compiled ? ' — recompiled before running' : ''}
          </div>
        </section>
      )}

      {run.error && <div className="sb-finding sb-finding-error">{run.error}</div>}

      {history.length > 0 && (
        <section>
          <h4>History</h4>
          <ul className="sb-run-history">
            {history.map((record) => (
              <li key={record.runId}>
                <button
                  type="button"
                  className={`sb-run-history-item sb-run-${record.status}${
                    record.runId === run.runId ? ' sb-run-history-current' : ''
                  }`}
                  disabled={inFlight}
                  onClick={() => void handleLoadRecord(record)}
                >
                  <span className="sb-run-history-status">{record.status}</span>
                  <span className="sb-run-history-time">{formatRelativeTime(record.createdAt)}</span>
                  <span className="sb-run-history-input">{formatRunValue(record.input).slice(0, 60)}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

export default RunPanel;
