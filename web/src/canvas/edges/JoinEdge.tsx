import { BaseEdge, getStraightPath, type EdgeProps } from '@xyflow/react';

export function JoinEdgeComponent({ id, sourceX, sourceY, targetX, targetY }: EdgeProps) {
  const [edgePath] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  return <BaseEdge id={id} path={edgePath} className="sb-edge-join" markerEnd="url(#sb-arrow)" />;
}

export default JoinEdgeComponent;
