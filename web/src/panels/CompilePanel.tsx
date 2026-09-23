import { useEffect, useRef, useState } from 'react';
import { useGraphStore } from '../state/graphStore';
import {
  api,
  ApiError,
  CompileConflictError,
  ReviewUnavailableError,
} from '../api/client';
import { modelSourceLabel } from './modelSource';
import type {
  CompileSnapshot,
  FindingOut,
  HealthResponse,
  ModelsResponse,
  StartCompileRequest,
} from '../api/schema';
import { hasUnseenEvents, isLiveCompileStatus, resumeCursor } from '../api/compileWire';
import { compilePhaseRows } from '../state/compileState';

const SESSION_KEY_PREFIX = 'swarm-builder:compile:';

/** The two compile targets `POST /api/compile` accepts (`StartCompileRequest.target`). */
type CompileTarget = NonNullable<StartCompileRequest['target']>;
const TARGET_LABELS: Record<CompileTarget, string> = {
  'pydantic-graph': 'PydanticAI (pydantic-graph)',
  langgraph: 'PydanticAI + LangGraph export',
};

/** A graph has at most one live compile (409 otherwise), so one sessionStorage
 * slot per graph is the whole persistence this panel needs: the compileId to
 * resume, and the last event id seen -- the cursor PLAN.md's "Browser reload
 * mid-compile" edge case replays from. */
interface StoredCompile {
  compileId: string;
  lastEventId?: string;
}

function sessionKey(graphId: string): string {
  return `${SESSION_KEY_PREFIX}${graphId}`;
}

function readStoredCompile(graphId: string): StoredCompile | null {
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
  const compileId = record.compileId;
  if (typeof compileId !== 'string') return null;
  const lastEventId = record.lastEventId;
  return { compileId, lastEventId: typeof lastEventId === 'string' ? lastEventId : undefined };
}

/**
 * Review findings -> model picker -> Compile -> streamed phases/log ->
 * result (PLAN.md "Frontend" -> "Compile panel").
 */
export function CompilePanel({ settingsRevision = 0 }: { settingsRevision?: number } = {}) {
  const graph = useGraphStore((s) => s.graph);
  const dirty = useGraphStore((s) => s.dirty);
  const saveStatus = useGraphStore((s) => s.saveStatus);
  const compile = useGraphStore((s) => s.compile);
  const setCompileState = useGraphStore((s) => s.setCompileState);
  const resetCompileState = useGraphStore((s) => s.resetCompileState);
  const applyCompileSnapshot = useGraphStore((s) => s.applyCompileSnapshot);
  const applyCompileEvent = useGraphStore((s) => s.applyCompileEvent);
  const markCompileStreamError = useGraphStore((s) => s.markCompileStreamError);
  const selectNodes = useGraphStore((s) => s.selectNodes);

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [models, setModels] = useState<ModelsResponse | null>(null);
  const [review, setReview] = useState<{
    ok: boolean;
    errors: FindingOut[];
    warnings: FindingOut[];
  } | null>(null);
  const [reviewNotice, setReviewNotice] = useState<string | null>(null);
  const [modelOverrideProvider, setModelOverrideProvider] = useState('');
  const [modelOverrideModel, setModelOverrideModel] = useState('');
  const [exportInfo, setExportInfo] = useState<{ projectPath: string; runCommand: string } | null>(null);
  const [target, setTarget] = useState<CompileTarget>('pydantic-graph');

  const unsubscribeRef = useRef<(() => void) | null>(null);
  const prevSaveStatus = useRef(saveStatus);

  const refreshHealth = () => {
    api.health().then(setHealth).catch(() => setHealth(null));
  };

  useEffect(() => {
    refreshHealth();
    api.getModels().then(setModels).catch(() => setModels(null));
    // Re-read after a settings save: every server read is fresh, so bumping
    // the revision is all it takes for a new provider to show up with no
    // page reload.
  }, [settingsRevision]);

  // Review is never fired on a bare timer against the in-memory
  // document (review finding A5): it only runs after an autosave PUT
  // resolves successfully (tracked via the saveStatus transition
  // 'saving' -> 'saved'), or when the panel is opened with dirty ===
  // false.
  useEffect(() => {
    if (!graph) return;
    const wasSaving = prevSaveStatus.current === 'saving';
    prevSaveStatus.current = saveStatus;
    if (saveStatus === 'saved' && wasSaving) {
      runReview(graph.id);
    }
  }, [saveStatus, graph]);

  useEffect(() => {
    if (graph && !dirty && review === null) {
      runReview(graph.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph?.id]);

  function runReview(graphId: string) {
    setReviewNotice(null);
    api
      .reviewGraph(graphId)
      .then((r) => setReview(r))
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) {
          setReviewNotice('Save the graph before reviewing.');
        } else if (err instanceof ReviewUnavailableError) {
          setReviewNotice('Review is temporarily unavailable on the server.');
        } else {
          setReviewNotice(`Review failed: ${err instanceof Error ? err.message : String(err)}`);
        }
        setReview(null);
      });
  }

  useEffect(() => {
    if (!graph) return;
    api
      .exportGraph(graph.id)
      .then((r) => setExportInfo({ projectPath: r.projectPath, runCommand: r.runCommand }))
      .catch(() => setExportInfo(null));
  }, [graph?.id, compile.status]);

  // On mount, resume a persisted compile if one exists for this graph (review
  // finding B6): snapshot-then-subscribe.
  //
  // The snapshot is all a reloaded page can know from the server's
  // point-in-time view: `status`, `result`, `error` and `latestEventId` (the
  // cursor a fresh client passes back as `Last-Event-ID`). It carries no phase
  // list and no log -- those live in the retained ring buffer, and the
  // resubscribed stream is what refills them. Writing the snapshot into the
  // store therefore never touches `phases`/`logLines`/`warnings`, so the next
  // `[...logLines, line]` cannot see an `undefined` array (PLAN.md edge case:
  // "Browser reload mid-compile -> SSE reconnect replays strictly after the
  // client's Last-Event-ID").
  useEffect(() => {
    if (!graph) return;
    const graphId = graph.id;
    const stored = readStoredCompile(graphId);
    if (!stored) return;
    const { compileId, lastEventId } = stored;
    const storedCursor = lastEventId ?? null;

    // Optimistic until the snapshot answers; the server's own vocabulary, not
    // an invented one.
    setCompileState({ compileId, status: 'queued' });

    let cancelled = false;
    api
      .getCompileSnapshot(compileId)
      .then((snapshot) => {
        if (cancelled) return;
        applyCompileSnapshot(snapshot);

        // Resume only when the stream can still send something. A live job
        // always can; a terminal one only if its retained log holds frames
        // this client has not seen. Subscribing at a cursor already consumed
        // gets an empty response, which the client would read as a lost
        // connection and retry -- ending in a reported failure for a compile
        // that actually succeeded.
        const cursor = storedCursor ?? resumeCursor(snapshot);
        if (isLiveCompileStatus(snapshot.status) || hasUnseenEvents(snapshot, cursor)) {
          subscribe(compileId, cursor ?? undefined);
        } else {
          sessionStorage.removeItem(sessionKey(graphId));
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          // Never existed, or evicted (finished jobs are retained up to 20,
          // dropped oldest-first). There is nothing to resume, so the honest
          // state is "start a fresh compile".
          sessionStorage.removeItem(sessionKey(graphId));
          setCompileState({
            compileId: null,
            status: 'idle',
            error: 'The previous compile is no longer available on the server. Compile again.',
          });
          return;
        }
        // Any other failure (a network blip, a 503): the stream may still
        // work, so subscribe with the cursor we have.
        subscribe(compileId, storedCursor ?? undefined);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph?.id]);

  useEffect(() => () => unsubscribeRef.current?.(), []);

  function subscribe(compileId: string, lastEventId?: string) {
    unsubscribeRef.current?.();
    const close = api.subscribeCompileEvents(compileId, {
      lastEventId,
      onLastEventId: (id) => {
        if (graph) {
          sessionStorage.setItem(sessionKey(graph.id), JSON.stringify({ compileId, lastEventId: id }));
        }
      },
      onEvent: (evt) => {
        // Every frame goes through the store's reducer, which narrows the
        // payload (`api/compileWire.ts`) before it becomes state -- a `log`
        // frame is `{message}`, not a stringified object.
        applyCompileEvent(evt);
        if (evt.type === 'done' || evt.type === 'error') {
          if (graph) sessionStorage.removeItem(sessionKey(graph.id));
        }
      },
      onError: (err) => {
        markCompileStreamError(err.message);
      },
    });
    unsubscribeRef.current = close;
  }

  if (!graph) {
    return <div className="sb-compile-panel-empty">No graph loaded.</div>;
  }

  const blockers = health?.blockers ?? [];
  const compileReady = health?.compileReady ?? false;

  // Model picker: corrected four-state semantics (GROUP6_PLAN.md
  // decision 7 / review finding A13). The server's `resolvedDefault`
  // never reports source "graph-override" (resolve_effective_model is
  // called with no graph_override argument in health.py/llm_routes.py)
  // -- the frontend computes that fourth state itself from whether this
  // graph's own `model` field is set.
  const graphOverride = graph.model;
  const resolvedDefault = models?.resolvedDefault;

  const largeGraphWarning =
    graph.nodes.length > 40
      ? `This graph has ${graph.nodes.length} nodes (>40) — one fill run may exceed a comfortable context.`
      : null;

  const compileInFlight = isLiveCompileStatus(compile.status);
  const phaseRows = compilePhaseRows(compile.phases);

  const handleCompile = async () => {
    resetCompileState();
    // `POST /api/compile` returns as soon as the job is registered and its
    // task scheduled, which is the server's `queued` status -- not `running`.
    setCompileState({ status: 'queued' });
    try {
      const { compileId } = await api.startCompile(graph.id, target);
      setCompileState({ compileId });
      sessionStorage.setItem(sessionKey(graph.id), JSON.stringify({ compileId }));
      subscribe(compileId);
    } catch (err) {
      if (err instanceof CompileConflictError) {
        setCompileState({ status: 'idle', error: 'A compile is already running for this graph.' });
      } else {
        setCompileState({ status: 'failed', error: err instanceof Error ? err.message : String(err) });
      }
    }
  };

  const handleCancel = async () => {
    const compileId = compile.compileId;
    if (compileId === null) return;
    try {
      // DELETE answers with the same snapshot GET would, carrying
      // `status: "cancelled"` -- no second request needed for the outcome.
      applyCompileSnapshot(await api.cancelCompile(compileId));
    } catch {
      setCompileState({ status: 'cancelled' });
    }
    unsubscribeRef.current?.();
    sessionStorage.removeItem(sessionKey(graph.id));
  };

  const handleCopyRunCommand = () => {
    const result = useGraphStore.getState().compile.result;
    if (result === null) return;
    const clipboard = navigator.clipboard;
    if (!clipboard) return;
    clipboard.writeText(result.runCommand).catch(() => {});
  };

  return (
    <div className="sb-compile-panel">
      <h3>Compile</h3>

      <section>
        <h4>Review</h4>
        {dirty && <div className="sb-review-stale">Review is stale — unsaved changes.</div>}
        {reviewNotice && <div className="sb-review-notice">{reviewNotice}</div>}
        {!dirty && review && (
          <>
            {review.errors.map((f, i) => (
              <div key={i} className="sb-finding sb-finding-error" onClick={() => selectNodes(f.nodeIds)}>
                <strong>{f.code}</strong>: {f.message}
              </div>
            ))}
            {review.warnings.map((f, i) => (
              <div key={i} className="sb-finding sb-finding-warning" onClick={() => selectNodes(f.nodeIds)}>
                <strong>{f.code}</strong>: {f.message}
              </div>
            ))}
            {largeGraphWarning && <div className="sb-finding sb-finding-warning">{largeGraphWarning}</div>}
            {review.ok && review.warnings.length === 0 && !largeGraphWarning && <div>No findings.</div>}
          </>
        )}
      </section>

      <section>
        <h4>Model</h4>
        {resolvedDefault && (
          <div className="sb-hint">
            {graphOverride
              ? (
                <>
                  Overridden by this graph: <strong>{graphOverride.provider} / {graphOverride.model}</strong>
                  {' '}— would otherwise use: {resolvedDefault.provider} / {resolvedDefault.model} (
                  {modelSourceLabel(resolvedDefault.source)})
                </>
              )
              : (
                <>
                  Using: <strong>{resolvedDefault.provider} / {resolvedDefault.model}</strong> —{' '}
                  {modelSourceLabel(resolvedDefault.source)}
                </>
              )}
          </div>
        )}
        {health?.dryRun && (
          <div className="sb-hint">
            Dry run: node bodies come from the built-in stub, not a model.
            {!health.modelConfigured && (
              <>
                {' '}
                The project records the offline default model id, which nothing here chose — set a
                model to change what the export targets.
              </>
            )}
          </div>
        )}
        <div className="sb-field-row">
          <button className="sb-btn-ghost sb-btn-sm" onClick={refreshHealth} title="Re-read the server's model state">
            Refresh
          </button>
          <select value={modelOverrideProvider} onChange={(e) => setModelOverrideProvider(e.target.value)}>
            <option value="">(use the configured model)</option>
            {models?.appRoute && (
              <option value={models.appRoute.key} disabled={models.appRoute.emission === 'unmappable'}>
                {models.appRoute.key} — configured here
              </option>
            )}
            {models?.routes.map((route) => (
              <option key={route.key} value={route.key} disabled={route.emission === 'unmappable'}>
                {route.key}
                {route.emission === 'unmappable' ? ` — unmappable: ${route.unmappableReason}` : ''}
              </option>
            ))}
          </select>
          {modelOverrideProvider && (
            <input
              placeholder="model id (free text for catalog-only routes)"
              value={modelOverrideModel}
              onChange={(e) => setModelOverrideModel(e.target.value)}
            />
          )}
          {modelOverrideProvider && (
            <button
              disabled={!modelOverrideModel.trim()}
              onClick={() =>
                useGraphStore.getState().setModelOverride({
                  provider: modelOverrideProvider,
                  model: modelOverrideModel.trim(),
                  reasoningEffort: null,
                })
              }
            >
              Set override
            </button>
          )}
          {graphOverride && (
            <button onClick={() => useGraphStore.getState().setModelOverride(null)}>Clear override</button>
          )}
        </div>
      </section>

      <section>
        {!compileReady && (
          <div className="sb-compile-blocked">
            <strong>Compile disabled:</strong>
            <ul>
              {blockers.map((b, i) => (
                <li key={i}>{b}</li>
              ))}
            </ul>
          </div>
        )}
        {exportInfo && <div className="sb-hint">Recompiling regenerates the whole project; v1 does not merge prior edits.</div>}
        <fieldset className="sb-target-picker">
          <legend>Target</legend>
          {(Object.keys(TARGET_LABELS) as CompileTarget[]).map((option) => (
            <label key={option}>
              <input
                type="radio"
                name="compile-target"
                value={option}
                checked={target === option}
                disabled={compileInFlight}
                onChange={() => setTarget(option)}
              />
              {TARGET_LABELS[option]}
            </label>
          ))}
          {target === 'langgraph' && (
            <div className="sb-hint">
              Runs the five phases, then converts the validated project into a LangGraph export
              (four more phases, one extra model call) under <code>workspace/projects-langgraph/</code>.
            </div>
          )}
        </fieldset>
        <div className="sb-action-row">
          <button
            className="sb-btn-primary"
            disabled={!compileReady || (review !== null && !review.ok) || compileInFlight}
            onClick={handleCompile}
          >
            Compile
          </button>
          {compileInFlight && <button onClick={handleCancel}>Cancel</button>}
        </div>
      </section>

      {compile.status !== 'idle' && (
        <section>
          <h4>Progress</h4>
          <ol className="sb-phase-list">
            {phaseRows.map((row) => (
              <li key={row.name} className={`sb-phase sb-phase-${row.status ?? 'pending'}`}>
                {row.label}: {row.statusLabel}
                {row.status === 'started' && row.attempt > 1 && (
                  <span className="sb-phase-retry"> — retrying (attempt {row.attempt})</span>
                )}
              </li>
            ))}
          </ol>
          {compile.phases.length === 0 && (
            <div className="sb-hint">No phase events yet — waiting for the stream.</div>
          )}
          <pre className="sb-log-tail" aria-live="polite" aria-label="Compile log">
            {compile.logLines.join('\n')}
          </pre>
          {compile.logLines.length === 0 && <div className="sb-hint">No log output yet.</div>}
          {compile.warnings.map((warning, i) => (
            <button
              key={`${warning.code}-${i}`}
              type="button"
              className="sb-finding sb-finding-warning"
              onClick={() => selectNodes(warning.nodeIds)}
            >
              <strong>{warning.code}</strong>: {warning.message}
            </button>
          ))}
        </section>
      )}

      {compile.result && (
        <section>
          <h4>Result</h4>
          <div className="sb-result-row">
            Project path: <code>{compile.result.projectPath}</code>
          </div>
          <div className="sb-result-row">
            <code>{compile.result.runCommand}</code>
            <button type="button" className="sb-btn-sm" onClick={handleCopyRunCommand}>
              Copy
            </button>
          </div>
          {compile.result.model && (
            <div className="sb-hint">
              Compiled with {compile.result.model.provider} / {compile.result.model.model} (via{' '}
              {compile.result.model.source})
              {compile.result.attempts > 1 ? ` — ${compile.result.attempts} fill attempts` : ''}
            </div>
          )}
          {compile.result.filledNodeIds.length > 0 && (
            <div className="sb-hint">Filled nodes: {compile.result.filledNodeIds.join(', ')}</div>
          )}
          {compile.result.diagram && <pre className="sb-diagram">{compile.result.diagram}</pre>}
          {compile.result.langgraph && (
            <div className="sb-langgraph-result">
              <h4>LangGraph export</h4>
              <div className="sb-result-row">
                Project path: <code>{compile.result.langgraph.projectPath}</code>
              </div>
              <div className="sb-result-row">
                <code>{compile.result.langgraph.runCommand}</code>
                <button
                  type="button"
                  className="sb-btn-sm"
                  onClick={() => {
                    const command = compile.result?.langgraph?.runCommand;
                    if (command && navigator.clipboard) navigator.clipboard.writeText(command).catch(() => {});
                  }}
                >
                  Copy
                </button>
              </div>
              {compile.result.langgraph.convertedNodeIds.length > 0 && (
                <div className="sb-hint">
                  Converted nodes: {compile.result.langgraph.convertedNodeIds.join(', ')}
                  {compile.result.langgraph.attempts > 1 ? ` — ${compile.result.langgraph.attempts} attempts` : ''}
                </div>
              )}
              {compile.result.langgraph.diagram && (
                <pre className="sb-diagram">{compile.result.langgraph.diagram}</pre>
              )}
            </div>
          )}
        </section>
      )}

      {compile.error && <div className="sb-finding sb-finding-error">{compile.error}</div>}
    </div>
  );
}

export default CompilePanel;
