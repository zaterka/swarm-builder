import { useGraphStore, starterPatch } from '../state/graphStore';
import { isDatabaseKind, type NodeKind } from '../api/schema';

/** The one place a node kind becomes a human label. `Canvas.tsx` imports it
 * too, so a node dropped on the canvas and a node clicked in the palette are
 * titled identically (and therefore slugify to the same id prefix).
 *
 * Only the *kind* name lives here. A database entry's starter (its default
 * operation, example data and description) is shown from the server catalog at
 * render time -- never copied in, because a second copy would drift from the
 * file the generated project is actually rendered from. */
export const KIND_LABELS: Record<NodeKind, string> = {
  agent: 'Agent',
  programmatic: 'Programmatic',
  decision: 'Decision',
  join: 'Join',
  sql: 'SQL database',
  nosql: 'NoSQL database',
  vector: 'Vector store',
};

/**
 * Drag sources for the seven node kinds (PLAN.md repo layout:
 * "Palette.tsx — drag sources"). Drop coordinates are converted from
 * screen space to flow space via the React Flow instance's
 * `screenToFlowPosition` in Canvas's drop handler, never assumed to be
 * flow coordinates directly (GROUP6_PLAN.md review finding E7) --
 * simplest implementation here: a click-to-add fallback plus HTML5
 * drag-and-drop, both funneled through the same `addNode` action.
 *
 * The three database entries additionally depend on the starter catalog
 * (`GET /api/database-starters`, cached in the store): while it is
 * unavailable they render **disabled** with a hint and a Retry button, so the
 * palette can never create a database node whose operation nothing could fill
 * in. Their starter spec *and* io are copied into the new node, which is what
 * makes a freshly added node compile and dry-run with no further input.
 */
export function Palette() {
  const addNode = useGraphStore((s) => s.addNode);
  const starters = useGraphStore((s) => s.databaseStarters);
  const startersStatus = useGraphStore((s) => s.databaseStartersStatus);
  const startersError = useGraphStore((s) => s.databaseStartersError);
  const retryDatabaseStarters = useGraphStore((s) => s.retryDatabaseStarters);

  const startersReady = startersStatus === 'ready' && starters !== null;

  const createNode = (kind: NodeKind) => {
    if (!isDatabaseKind(kind)) {
      addNode(kind, KIND_LABELS[kind], { x: 40, y: 40 });
      return;
    }
    // No catalog, no node: `starterPatch` has no hand-written fallback, so a
    // database node can only ever be created from the server's own starter.
    const patch = starterPatch(kind, starters);
    if (!patch) return;
    addNode(kind, KIND_LABELS[kind], { x: 40, y: 40 }, patch);
  };

  const onDragStart = (event: React.DragEvent, kind: NodeKind) => {
    if (isDatabaseKind(kind) && !startersReady) {
      event.preventDefault();
      return;
    }
    event.dataTransfer.setData('application/swarm-builder-node-kind', kind);
    event.dataTransfer.effectAllowed = 'move';
  };

  return (
    <div className="sb-palette">
      <div className="sb-palette-title">Add node</div>
      {(Object.keys(KIND_LABELS) as NodeKind[]).map((kind) => {
        const database = isDatabaseKind(kind);
        const disabled = database && !startersReady;
        const starter = database ? starters?.[kind] : undefined;
        return (
          <div
            key={kind}
            className={`sb-palette-item sb-kind-${kind}${disabled ? ' sb-palette-item-disabled' : ''}`}
            draggable={!disabled}
            aria-disabled={disabled || undefined}
            onDragStart={(e) => onDragStart(e, kind)}
            onClick={() => {
              if (disabled) return;
              createNode(kind);
            }}
            role="button"
            tabIndex={disabled ? -1 : 0}
            title={
              disabled
                ? 'The database starter catalog is unavailable, so this node cannot be created yet.'
                : starter?.description
            }
          >
            <span className="sb-palette-item-label">{KIND_LABELS[kind]}</span>
            {starter && <span className="sb-palette-item-starter">{starter.label}</span>}
          </div>
        );
      })}
      {startersStatus === 'unavailable' && (
        <div className="sb-palette-hint" role="status">
          Database starters are unavailable, so the three database entries are disabled.
          {startersError ? ` (${startersError})` : ''}{' '}
          <button type="button" className="sb-btn-sm" onClick={() => void retryDatabaseStarters()}>
            Retry
          </button>
        </div>
      )}
    </div>
  );
}

export default Palette;
