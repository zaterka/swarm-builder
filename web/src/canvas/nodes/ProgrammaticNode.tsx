import { Handle, Position as FlowPosition, type NodeProps } from '@xyflow/react';
import { jitterDegForId } from '../jitter';
import type { SwarmNodeData } from './AgentNode';

export function ProgrammaticNode({ id, data }: NodeProps & { data: SwarmNodeData }) {
  const { node, isEntry, isExit } = data;
  return (
    <div
      className="sb-node-sketch sb-kind-programmatic"
      style={{ '--sb-jitter': `${jitterDegForId(id)}deg` } as React.CSSProperties}
      data-testid={`node-${id}`}
      data-node-kind="programmatic"
    >
      {isEntry && <span className="sb-node-corner-tag">START</span>}
      {!isEntry && isExit && <span className="sb-node-corner-tag">END</span>}
      <Handle type="target" position={FlowPosition.Left} />
      <div className="sb-node-badge">FN</div>
      <div className="sb-node-title">{node.title}</div>
      <div className="sb-node-intent-preview">{node.intent || '(no intent set)'}</div>
      <Handle type="source" position={FlowPosition.Right} />
    </div>
  );
}

export default ProgrammaticNode;
