import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { formatRelativeTime } from '../relativeTime';
import type { FindingOut, GraphListError, GraphSummary, HealthResponse, SwarmGraph } from '../api/schema';
import GeneratePanel from './GeneratePanel';

/**
 * The start screen (GROUP6_PLAN.md decision 1: a graph picker precedes
 * the workspace). Beyond opening/creating/deleting a graph it shows
 * `GraphSummary.updatedAt`, which the API has always returned, so the
 * list reads as "what I was last working on" rather than an unordered
 * dump of files.
 */
export function GraphPicker(props: {
  onOpen: (id: string) => void;
  onCreateNew: () => void;
  onGenerated: (graph: SwarmGraph, warnings: FindingOut[], attempts: number) => void;
}) {
  const [graphs, setGraphs] = useState<GraphSummary[]>([]);
  const [errors, setErrors] = useState<GraphListError[]>([]);
  const [loading, setLoading] = useState(true);
  // Environment readiness is otherwise only visible in the compile
  // drawer, i.e. after picking a graph. A missing/failed /api/health
  // just hides the strip rather than blocking the screen.
  const [health, setHealth] = useState<HealthResponse | null>(null);

  const refresh = () => {
    setLoading(true);
    api
      .listGraphs()
      .then((r) => {
        setGraphs(r.graphs);
        setErrors(r.errors);
      })
      .finally(() => setLoading(false));
  };

  useEffect(refresh, []);

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch(() => setHealth(null));
  }, []);

  const handleDelete = async (id: string, withProject: boolean) => {
    await api.deleteGraph(id, { project: withProject });
    refresh();
  };

  // Most recently edited first: the server lists whatever order the
  // directory yields, which is meaningless to the reader.
  const ordered = [...graphs].sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt));

  return (
    <div className="sb-start">
      <header className="sb-start-hero">
        <FlowMark />
        <h1>Swarm Builder</h1>
        <p className="sb-start-tagline">
          Sketch agents on a canvas, wire how work flows between them, then compile the graph into
          a runnable project.
        </p>
        <button className="sb-btn-primary sb-btn-lg" onClick={props.onCreateNew}>
          New graph
        </button>
      </header>

      <section className="sb-start-section sb-start-generate">
        <GeneratePanel onGenerated={props.onGenerated} />
      </section>

      <section className="sb-start-section">
        <div className="sb-start-section-head">
          <h2>Your graphs</h2>
          {!loading && ordered.length > 0 && <span className="sb-start-count">{ordered.length}</span>}
        </div>

        {loading && <GraphListSkeleton />}

        {!loading && ordered.length === 0 && (
          <div className="sb-start-empty">
            <FlowMark large />
            <div className="sb-start-empty-title">No graphs yet</div>
            <p className="sb-start-empty-body">
              A graph is one workflow: a few agent or function nodes, connected in the order they
              should run.
            </p>
            <button className="sb-btn-primary" onClick={props.onCreateNew}>
              Create your first graph
            </button>
          </div>
        )}

        {!loading && ordered.length > 0 && (
          <ul className="sb-graph-list">
            {ordered.map((g) => (
              <li key={g.id} className="sb-graph-row">
                <button className="sb-graph-open" onClick={() => props.onOpen(g.id)}>
                  <span className="sb-graph-open-name">{g.name}</span>
                  <span className="sb-graph-open-meta" title={new Date(g.updatedAt).toLocaleString()}>
                    {g.nodeCount} {g.nodeCount === 1 ? 'node' : 'nodes'}
                    <span className="sb-dot-sep" aria-hidden="true" />
                    {g.edgeCount} {g.edgeCount === 1 ? 'edge' : 'edges'}
                    <span className="sb-dot-sep" aria-hidden="true" />
                    edited {formatRelativeTime(g.updatedAt)}
                  </span>
                </button>
                <span className="sb-graph-row-actions">
                  <button
                    className="sb-btn-danger sb-btn-sm"
                    onClick={() => handleDelete(g.id, false)}
                    title="Delete the graph, keep any compiled project"
                  >
                    Delete
                  </button>
                  <button
                    className="sb-btn-danger sb-btn-sm"
                    onClick={() => handleDelete(g.id, true)}
                    title="Delete the graph and its compiled project directory"
                  >
                    Delete + project
                  </button>
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {errors.length > 0 && (
        <div className="sb-graph-list-errors">
          <h4>Some graphs could not be loaded</h4>
          <ul>
            {errors.map((e) => (
              <li key={e.id}>
                {e.id}: {e.detail}
              </li>
            ))}
          </ul>
        </div>
      )}

      {health && <EnvironmentStrip health={health} />}
    </div>
  );
}

/** What this install is set up to do, before a graph is even opened:
 * whether compiling can run at all, which model it would use, and where
 * graphs are being written. */
function EnvironmentStrip(props: { health: HealthResponse }) {
  const { health } = props;
  const model = health.resolvedModel;
  return (
    <div className="sb-env-strip">
      <div className="sb-env-line">
        <span
          className="sb-env-status"
          data-ready={health.compileReady ? 'yes' : 'no'}
        >
          {health.compileReady ? 'Ready to compile' : 'Compile blocked'}
        </span>
        {model && (
          <span className="sb-env-item">
            {model.provider} / {model.model}
          </span>
        )}
        <span className="sb-env-item" title={health.workspaceDir}>
          workspace <code>{shortenPath(health.workspaceDir)}</code>
        </span>
        <span className="sb-env-item sb-env-version">v{health.version}</span>
      </div>
      {health.blockers.length > 0 && (
        <ul className="sb-env-blockers">
          {health.blockers.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Absolute workspace paths are long and the part worth reading is the
 * tail, which is exactly the end a CSS ellipsis would cut. */
function shortenPath(path: string, keep = 2): string {
  const parts = path.split('/').filter(Boolean);
  if (parts.length <= keep) return path;
  return `…/${parts.slice(-keep).join('/')}`;
}

/** Two agents feeding a join, in the same per-kind colours the canvas
 * uses -- the mark is the product's own vocabulary, not a generic logo. */
function FlowMark(props: { large?: boolean }) {
  const size = props.large ? 40 : 30;
  return (
    <svg
      className="sb-flow-mark"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d="M10 16 L21.5 8.5 M10 16 L21.5 23.5"
        stroke="var(--sb-ink)"
        strokeWidth="1.6"
        strokeLinecap="round"
        opacity="0.55"
      />
      <circle cx="9.5" cy="16" r="4.2" fill="var(--sb-agent)" />
      <rect
        x="18.4"
        y="5"
        width="7.2"
        height="7.2"
        rx="1.6"
        fill="var(--sb-decision)"
        transform="rotate(-7 22 8.6)"
      />
      <circle cx="22.2" cy="23.6" r="3.6" fill="var(--sb-join)" />
    </svg>
  );
}

/** Placeholder rows sized like real ones, so the list doesn't jump when
 * the request resolves. */
function GraphListSkeleton() {
  return (
    <ul className="sb-graph-list" aria-hidden="true">
      {[0, 1].map((i) => (
        <li key={i} className="sb-graph-row sb-graph-row-skeleton">
          <span className="sb-skeleton-bar" style={{ width: i === 0 ? '38%' : '30%' }} />
          <span className="sb-skeleton-bar sb-skeleton-bar-sm" style={{ width: i === 0 ? '56%' : '48%' }} />
        </li>
      ))}
    </ul>
  );
}

export default GraphPicker;
