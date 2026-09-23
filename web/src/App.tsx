import { useEffect, useRef, useState } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { useGraphStore } from './state/graphStore';
import { api, ApiError } from './api/client';
import Canvas from './canvas/Canvas';
import Palette from './panels/Palette';
import Inspector from './panels/Inspector';
import CompilePanel from './panels/CompilePanel';
import RunPanel from './panels/RunPanel';
import GeneratePanel from './panels/GeneratePanel';
import type { FindingOut, HealthResponse, SwarmGraph } from './api/schema';
import GraphPicker from './panels/GraphPicker';
import ModelSettings from './panels/ModelSettings';
import DryRunBadge from './panels/DryRunBadge';
import StartTopBar from './panels/StartTopBar';
import './App.css';

const AUTOSAVE_DEBOUNCE_MS = 800;

type Screen = { name: 'picker' } | { name: 'workspace' };
type DrawerTab = 'compile' | 'run';

/**
 * Top-level shell (PLAN.md's "Frontend" section doesn't specify one --
 * GROUP6_PLAN.md decision 1): a graph picker first, then a three-pane
 * workspace. Component state, never a URL route, since `main.py`
 * mounts StaticFiles(html=True) with no SPA-fallback rewrite (review
 * finding A16).
 */
export function App() {
  const [screen, setScreen] = useState<Screen>({ name: 'picker' });
  const [drawerTab, setDrawerTab] = useState<DrawerTab>('compile');
  // Whether the model-settings screen is open, and a counter the panels use
  // to re-read `/api/health` after a save (every read is fresh server-side, so
  // a bump is all it takes for a new provider to take effect with no reload).
  const [showSettings, setShowSettings] = useState(false);
  const [settingsRevision, setSettingsRevision] = useState(0);
  // The shell owns the start screen's health read because two surfaces render
  // from it -- the top bar (chrome, outside the centered start column) and the
  // picker's notice -- and two independent reads could disagree about the model.
  // A failed read is `null`, which the bar reports as "checking" and the picker
  // treats as "no strip", exactly as before.
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [showRegenerate, setShowRegenerate] = useState(false);
  const [generatedNotice, setGeneratedNotice] = useState<{
    warnings: FindingOut[];
    attempts: number;
    dryRun: boolean;
  } | null>(null);
  const selectNodes = useGraphStore((s) => s.selectNodes);
  const graph = useGraphStore((s) => s.graph);
  const dirty = useGraphStore((s) => s.dirty);
  const saveStatus = useGraphStore((s) => s.saveStatus);
  const saveError = useGraphStore((s) => s.saveError);
  const loadGraph = useGraphStore((s) => s.loadGraph);
  const newGraph = useGraphStore((s) => s.newGraph);
  const markSaving = useGraphStore((s) => s.markSaving);
  const applySaved = useGraphStore((s) => s.applySaved);
  const markSaveError = useGraphStore((s) => s.markSaveError);
  const toSavePayload = useGraphStore((s) => s.toSavePayload);

  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retryCountRef = useRef(0);

  // Autosave: debounces a PUT at 800ms after the last mutation
  // (PLAN.md "Inspector": "Editing marks the graph dirty; autosave
  // debounces a PUT at 800 ms"). Wired here, not inside the store
  // itself, so the store stays free of timers/side effects
  // (GROUP6_PLAN.md "state/graphStore.ts" design note) and therefore
  // trivially unit-testable.
  useEffect(() => {
    if (!dirty || !graph) return;
    if (debounceTimer.current) clearTimeout(debounceTimer.current);
    debounceTimer.current = setTimeout(() => {
      void doSave();
    }, AUTOSAVE_DEBOUNCE_MS);
    return () => {
      if (debounceTimer.current) clearTimeout(debounceTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty, graph]);

  async function doSave(attempt = 0): Promise<void> {
    const payload = toSavePayload();
    if (!payload) return;
    const forRevision = markSaving();
    try {
      const saved = await api.putGraph(payload.id, payload);
      applySaved(saved.updatedAt, forRevision);
      retryCountRef.current = 0;
    } catch (err) {
      const detail = err instanceof ApiError ? String(err.detail) : String(err);
      if (attempt < 2) {
        // Bounded retry with backoff before surfacing a failure
        // (review finding A9).
        retryCountRef.current = attempt + 1;
        setTimeout(() => void doSave(attempt + 1), 500 * 2 ** attempt);
      } else {
        markSaveError(detail);
      }
    }
  }

  // Flush any pending debounced save on visibilitychange/beforeunload
  // (review finding A9) so "reload restores the graph exactly" holds
  // even for an edit inside the 800ms window.
  useEffect(() => {
    const flush = () => {
      if (dirty && graph) {
        const payload = toSavePayload();
        if (payload) {
          void fetch(`/api/graphs/${encodeURIComponent(payload.id)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            keepalive: true,
          });
        }
      }
    };
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') flush();
    });
    window.addEventListener('beforeunload', flush);
    return () => {
      window.removeEventListener('beforeunload', flush);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty, graph]);

  useEffect(() => {
    let cancelled = false;
    api
      .health()
      .then((value) => {
        if (!cancelled) setHealth(value);
      })
      .catch(() => {
        if (!cancelled) setHealth(null);
      });
    return () => {
      cancelled = true;
    };
  }, [settingsRevision]);

  const openGraph = async (id: string) => {
    const g = await api.getGraph(id);
    loadGraph(g);
    setScreen({ name: 'workspace' });
  };

  const createNew = async () => {
    const id = crypto.randomUUID();
    const g = newGraph(id, 'Untitled graph');
    await api.putGraph(id, g).catch(() => {});
    setScreen({ name: 'workspace' });
  };

  // A generated graph is already saved by the server; load it as an ordinary
  // document, select every node so the review is visible at a glance, and
  // keep a dismissible notice with the reviewer's warnings.
  const onGenerated = (
    g: SwarmGraph,
    warnings: FindingOut[],
    attempts: number,
    dryRun = false,
  ) => {
    loadGraph(g);
    selectNodes((g.nodes ?? []).map((n) => n.id));
    setGeneratedNotice({ warnings, attempts, dryRun });
    setShowRegenerate(false);
    setScreen({ name: 'workspace' });
  };

  const modelSettings = showSettings ? (
    <ModelSettings
      onClose={() => setShowSettings(false)}
      onSaved={() => setSettingsRevision((n) => n + 1)}
    />
  ) : null;

  if (screen.name === 'picker') {
    return (
      <>
        {/* Chrome, so it sits outside the centered start column -- the same bar
            the workspace has, in the same place. */}
        <StartTopBar health={health} onOpenSettings={() => setShowSettings(true)} />
        <GraphPicker
          onOpen={openGraph}
          onCreateNew={createNew}
          onGenerated={onGenerated}
          settingsRevision={settingsRevision}
          onOpenSettings={() => setShowSettings(true)}
          health={health}
        />
        {modelSettings}
      </>
    );
  }

  return (
    <div className="sb-app-shell">
      <div className="sb-toolbar">
        <button className="sb-btn-ghost" onClick={() => setScreen({ name: 'picker' })}>
          ← Graphs
        </button>
        <button
          className="sb-btn-ghost sb-btn-sm"
          onClick={() => setShowSettings(true)}
          title="Choose the provider and API key used for describing, compiling and running"
        >
          Model settings
        </button>
        <DryRunBadge revision={settingsRevision} />
        <span className="sb-graph-name">{graph?.name}</span>
        <button
          className="sb-btn-ghost sb-btn-sm"
          onClick={() => setShowRegenerate((v) => !v)}
          title="Describe the workflow in prose and replace this canvas with a generated graph"
        >
          {showRegenerate ? 'Close' : 'Describe…'}
        </button>
        <span className="sb-save-status" data-status={saveStatus}>
          {saveStatus === 'saving' && 'Saving…'}
          {saveStatus === 'saved' && 'Saved'}
          {saveStatus === 'error' && (
            <>
              Save failed: {saveError}{' '}
              <button className="sb-btn-sm" onClick={() => void doSave()}>
                Retry
              </button>
            </>
          )}
        </span>
      </div>
      {showRegenerate && graph && (
        <div className="sb-generate-popover">
          <GeneratePanel
            compact
            replaceGraphId={graph.id}
            keepName={graph.name}
            onGenerated={onGenerated}
            onCancel={() => setShowRegenerate(false)}
          />
        </div>
      )}
      {generatedNotice && (
        <div className="sb-generated-notice" role="status">
          <span>
            {generatedNotice.dryRun
              ? 'Drafting was done in Dry run mode: this graph came from the built-in stub, not a model. '
              : 'Generated from your description'}
            {generatedNotice.attempts > 1 ? ` (${generatedNotice.attempts} drafts)` : ''} — review the
            nodes, then Compile or Run.
          </span>
          {generatedNotice.warnings.length > 0 && (
            <ul>
              {generatedNotice.warnings.map((w, i) => (
                <li key={`${w.code}-${i}`}>
                  <strong>{w.code}</strong>: {w.message}
                </li>
              ))}
            </ul>
          )}
          <button className="sb-btn-sm" onClick={() => setGeneratedNotice(null)}>
            Dismiss
          </button>
        </div>
      )}
      <div className="sb-workspace">
        <div className="sb-palette-pane">
          <Palette />
        </div>
        <div className="sb-canvas-pane-wrapper">
          <ReactFlowProvider>
            <Canvas />
          </ReactFlowProvider>
        </div>
        <div className="sb-inspector-pane">
          <Inspector />
        </div>
      </div>
      <div className="sb-compile-drawer">
        <div className="sb-drawer-tabs" role="tablist">
          <button
            role="tab"
            aria-selected={drawerTab === 'compile'}
            className={drawerTab === 'compile' ? 'sb-tab-active' : ''}
            onClick={() => setDrawerTab('compile')}
          >
            Compile
          </button>
          <button
            role="tab"
            aria-selected={drawerTab === 'run'}
            className={drawerTab === 'run' ? 'sb-tab-active' : ''}
            onClick={() => setDrawerTab('run')}
          >
            Run
          </button>
        </div>
        {/* Both panels stay mounted so a live SSE subscription survives a tab
            switch; the inactive one is only hidden. */}
        <div hidden={drawerTab !== 'compile'}>
          <CompilePanel settingsRevision={settingsRevision} />
        </div>
        <div hidden={drawerTab !== 'run'}>
          <RunPanel settingsRevision={settingsRevision} />
        </div>
      </div>
      {modelSettings}
    </div>
  );
}

export default App;
