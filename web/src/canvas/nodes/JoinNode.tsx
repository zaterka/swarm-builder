import { Handle, Position as FlowPosition, type NodeProps } from '@xyflow/react';
import { jitterDegForId } from '../jitter';
import type { SwarmNodeData } from './AgentNode';

export function JoinNode({ id, data }: NodeProps & { data: SwarmNodeData }) {
  const { node, isEntry, isExit, runStatus } = data;
  return (
    <div
      className="sb-node-sketch sb-kind-join"
      style={{ '--sb-jitter': `${jitterDegForId(id)}deg` } as React.CSSProperties}
      data-testid={`node-${id}`}
      data-run-status={runStatus ?? 'idle'}
      data-node-kind="join"
    >
      {isEntry && <span className="sb-node-corner-tag">START</span>}
      {!isEntry && isExit && <span className="sb-node-corner-tag">END</span>}
      <Handle type="target" position={FlowPosition.Left} />
      <div className="sb-node-badge">JOIN · {node.join?.reducer ?? 'list_append'}</div>
      <div className="sb-node-title">{node.title}</div>
      <div className="sb-node-intent-preview">{node.intent || '(no intent set)'}</div>
      <Handle type="source" position={FlowPosition.Right} />
    </div>
  );
}

export default JoinNode;
