import { BaseEdge, EdgeLabelRenderer, getStraightPath, type EdgeProps } from '@xyflow/react';

// Always labeled with its match expression -- this is the match value,
// not decorative text (PLAN.md "Canvas": "branch labeled with its
// match expression").
export function BranchEdgeComponent({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  data,
}: EdgeProps & { data?: { match?: string } }) {
  const [edgePath, labelX, labelY] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return (
    <>
      <BaseEdge id={id} path={edgePath} className="sb-edge-branch" />
      <EdgeLabelRenderer>
        <div
          className="sb-edge-label"
          style={{ position: 'absolute', transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          data-testid={`edge-branch-label-${id}`}
        >
          {data?.match ?? ''}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

export default BranchEdgeComponent;
