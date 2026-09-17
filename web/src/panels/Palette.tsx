import { useGraphStore } from '../state/graphStore';
import type { NodeKind } from '../api/schema';

const KIND_LABELS: Record<NodeKind, string> = {
  agent: 'Agent',
  programmatic: 'Programmatic',
  decision: 'Decision',
  join: 'Join',
};

/**
 * Drag sources for the four node kinds (PLAN.md repo layout:
 * "Palette.tsx — drag sources"). Drop coordinates are converted from
 * screen space to flow space via the React Flow instance's
 * `screenToFlowPosition` in Canvas's drop handler, never assumed to be
 * flow coordinates directly (GROUP6_PLAN.md review finding E7) --
 * simplest implementation here: a click-to-add fallback plus HTML5
 * drag-and-drop, both funneled through the same `addNode` action.
 */
export function Palette() {
  const addNode = useGraphStore((s) => s.addNode);

  const onDragStart = (event: React.DragEvent, kind: NodeKind) => {
    event.dataTransfer.setData('application/swarm-builder-node-kind', kind);
    event.dataTransfer.effectAllowed = 'move';
  };

  const onClickAdd = (kind: NodeKind) => {
    addNode(kind, KIND_LABELS[kind], { x: 40, y: 40 });
  };

  return (
    <div className="sb-palette">
      <div className="sb-palette-title">Add node</div>
      {(Object.keys(KIND_LABELS) as NodeKind[]).map((kind) => (
        <div
          key={kind}
          className={`sb-palette-item sb-kind-${kind}`}
          draggable
          onDragStart={(e) => onDragStart(e, kind)}
          onClick={() => onClickAdd(kind)}
          role="button"
          tabIndex={0}
        >
          {KIND_LABELS[kind]}
        </div>
      ))}
    </div>
  );
}

export default Palette;
