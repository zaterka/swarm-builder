import { Handle, Position as FlowPosition, type NodeProps } from '@xyflow/react';
import { jitterDegForId } from '../jitter';
import type { SwarmNodeData } from './AgentNode';

export function DecisionNode({ id, data }: NodeProps & { data: SwarmNodeData }) {
  const { node, isEntry, isExit } = data;
  const branchCount = node.decision?.branches?.length ?? 0;
  return (
    <div
      className="sb-node-sketch sb-kind-decision"
      style={{ '--sb-jitter': `${jitterDegForId(id)}deg` } as React.CSSProperties}
      data-testid={`node-${id}`}
      data-node-kind="decision"
    >
      {isEntry && <span className="sb-node-corner-tag">START</span>}
      {!isEntry && isExit && <span className="sb-node-corner-tag">END</span>}
      <Handle type="target" position={FlowPosition.Left} />
      <div className="sb-node-badge">IF · {branchCount} branch{branchCount === 1 ? '' : 'es'}</div>
      <div className="sb-node-title">{node.title}</div>
      <div className="sb-node-intent-preview">{node.intent || '(no intent set)'}</div>
      <Handle type="source" position={FlowPosition.Right} />
    </div>
  );
}

export default DecisionNode;
