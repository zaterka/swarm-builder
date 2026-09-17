import { useEffect, useRef, useState } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { useGraphStore } from './state/graphStore';
import { api, ApiError } from './api/client';
import Canvas from './canvas/Canvas';
import Palette from './panels/Palette';
import Inspector from './panels/Inspector';
import CompilePanel from './panels/CompilePanel';
import GraphPicker from './panels/GraphPicker';
import './App.css';

const AUTOSAVE_DEBOUNCE_MS = 800;

type Screen = { name: 'picker' } | { name: 'workspace' };

/**
 * Top-level shell (PLAN.md's "Frontend" section doesn't specify one --
 * GROUP6_PLAN.md decision 1): a graph picker first, then a three-pane
 * workspace. Component state, never a URL route, since `main.py`
 * mounts StaticFiles(html=True) with no SPA-fallback rewrite (review
 * finding A16).
 */
export function App() {
  const [screen, setScreen] = useState<Screen>({ name: 'picker' });
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

  if (screen.name === 'picker') {
    return <GraphPicker onOpen={openGraph} onCreateNew={createNew} />;
  }

  return (
    <div className="sb-app-shell">
      <div className="sb-toolbar">
        <button onClick={() => setScreen({ name: 'picker' })}>← Graphs</button>
        <span className="sb-graph-name">{graph?.name}</span>
        <span className="sb-save-status">
          {saveStatus === 'saving' && 'Saving…'}
          {saveStatus === 'saved' && 'Saved'}
          {saveStatus === 'error' && (
            <>
              Save failed: {saveError}{' '}
              <button onClick={() => void doSave()}>Retry</button>
            </>
          )}
        </span>
      </div>
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
        <CompilePanel />
      </div>
    </div>
  );
}

export default App;
