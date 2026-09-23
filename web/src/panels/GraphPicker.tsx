import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { formatRelativeTime } from '../relativeTime';
import type { FindingOut, GraphListError, GraphSummary, HealthResponse, SwarmGraph } from '../api/schema';
import GeneratePanel from './GeneratePanel';
import { modelState, needsAttention } from './modelState';

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
  onGenerated: (
    graph: SwarmGraph,
    warnings: FindingOut[],
    attempts: number,
    dryRun?: boolean,
  ) => void;
  /** Opens the model settings screen. */
  onOpenSettings?: () => void;
  /** Bumped by the shell after a settings save, to re-read health. */
  settingsRevision?: number;
  /** Read by the shell, which also feeds the top bar from it: one value, so the
   *  bar and this page's notice can never disagree about the model. A `null`
   *  means "not known yet" (or a failed read) and hides the strip, as before. */
  health: HealthResponse | null;
}) {
  const [graphs, setGraphs] = useState<GraphSummary[]>([]);
  const [errors, setErrors] = useState<GraphListError[]>([]);
  const [loading, setLoading] = useState(true);
  const health = props.health;

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

      {/* Only when something needs doing: the steady state is quiet, because the
          top bar already says which model this page will spend. */}
      {needsAttention(modelState(health)) && (
        <ModelNotice health={health} onOpenSettings={props.onOpenSettings} />
      )}

      <section className="sb-start-section sb-start-generate">
        <GeneratePanel
          onGenerated={props.onGenerated}
          settingsRevision={props.settingsRevision}
        />
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

/**
 * The one thing on this page that appears only when the user has to act:
 * nothing configured, a missing API key, or a settings file that cannot be
 * read. Everything steady-state is in the top bar, which is why this is a
 * notice rather than a permanent row — a read-out between "New graph" and
 * "Describe your workflow" reads as a step in the flow.
 *
 * It carries the *reason and the remedy* in this application's own short words.
 * The server's longer sentence for the same condition stays in the environment
 * strip's blocker list, so no sentence is printed twice.
 *
 * No `aria-live`: this is ordinary page content present at load, and a live
 * region that mounts already-populated is announced inconsistently across
 * assistive technologies.
 */
const NOTICE_ACTIONS: Record<string, string> = {
  config: 'Fix in Model settings',
  warn: 'Set up a model',
  cred: 'Add a key',
};

export function ModelNotice(props: {
  health: HealthResponse | null;
  onOpenSettings?: () => void;
}) {
  const state = modelState(props.health);
  const action = NOTICE_ACTIONS[state];
  if (!action) return null;

  return (
    <section className="sb-model-notice" data-state={state} data-testid="model-notice">
      <div className="sb-model-notice-main">
        {state === 'warn' && (
          <>
            <strong>No model is configured yet.</strong>{' '}
            <span className="sb-hint">
              Choose a provider and add your API key to describe, compile and run workflows — or turn
              on Dry run mode to build and run offline.
            </span>
          </>
        )}
        {state === 'cred' && (
          <span className="sb-hint">
            No API key for this model yet — describing and compiling need one.
          </span>
        )}
        {state === 'config' && (
          <span className="sb-hint">
            Swarm Builder&apos;s model settings could not be read — open Model settings to fix them.
          </span>
        )}
      </div>
      {props.onOpenSettings && (
        <button className="sb-btn-primary" onClick={props.onOpenSettings}>
          {action}
        </button>
      )}
    </section>
  );
}

/** What this install is set up to do, before a graph is even opened: whether
 * compiling can run at all, where graphs are being written, and which version
 * this is. The *model* is the top bar's statement, not this one's.
 *
 * The blockers it lists are the server's own, written in this application's
 * terms ("Model settings", "Dry run mode"); nothing here asks a new user to
 * know about a file or variable belonging to something else. */
function EnvironmentStrip(props: { health: HealthResponse }) {
  const { health } = props;
  return (
    <div className="sb-env-strip">
      <div className="sb-env-line">
        <span
          className="sb-env-status"
          data-ready={health.compileReady ? 'yes' : 'no'}
        >
          {health.compileReady ? 'Ready to compile' : 'Compile blocked'}
        </span>
        {/* No model line here: the top bar says which model this install will
            spend, always and more prominently. This strip keeps what is
            genuinely about the environment. */}
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
