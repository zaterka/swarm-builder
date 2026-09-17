import { BaseEdge, EdgeLabelRenderer, getStraightPath, type EdgeProps } from '@xyflow/react';

// Drawn toward the join node via a shared stroke color and a
// "-> join:<id>" label rather than a custom curved path that reaches
// into the join node's own coordinates -- the curved-path approach is
// a deferred stretch goal (GROUP6_PLAN.md review finding E5): it would
// need either prop-drilling coordinates or a store read inside the
// edge renderer, both adding re-render cost with no functional
// requirement beyond "drawn toward", which this label already
// satisfies.
export function FanoutEdgeComponent({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  data,
}: EdgeProps & { data?: { joinNodeId?: string } }) {
  const [edgePath, labelX, labelY] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return (
    <>
      <BaseEdge id={id} path={edgePath} className="sb-edge-fanout" />
      <EdgeLabelRenderer>
        <div
          className="sb-edge-label"
          style={{ position: 'absolute', transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
        >
          → join:{data?.joinNodeId ?? '?'}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

export default FanoutEdgeComponent;
