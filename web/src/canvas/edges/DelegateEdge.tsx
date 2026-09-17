import { BaseEdge, EdgeLabelRenderer, getStraightPath, type EdgeProps } from '@xyflow/react';

// Dashed to signal "called as a tool, not a step" (I1) -- this
// distinction is semantic, not decorative (PLAN.md "Canvas").
export function DelegateEdgeComponent({ id, sourceX, sourceY, targetX, targetY }: EdgeProps) {
  const [edgePath, labelX, labelY] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return (
    <>
      <BaseEdge id={id} path={edgePath} className="sb-edge-delegate" />
      <EdgeLabelRenderer>
        <div
          className="sb-edge-label"
          style={{ position: 'absolute', transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
        >
          🔧 tool call
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

export default DelegateEdgeComponent;
