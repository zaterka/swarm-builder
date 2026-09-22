import { Handle, Position as FlowPosition, type NodeProps } from '@xyflow/react';
import { jitterDegForId } from '../jitter';
import type { SwarmNode } from '../../api/schema';
import type { RunNodeDisplayStatus } from '../../state/runState';

export interface SwarmNodeData extends Record<string, unknown> {
  node: SwarmNode;
  isEntry: boolean;
  isExit: boolean;
  /** What the last/current run says about this node (`state/runState.ts`
   * `deriveRunDisplayStatuses`); painted via `data-run-status`. */
  runStatus?: RunNodeDisplayStatus;
}

export function AgentNode({ id, data }: NodeProps & { data: SwarmNodeData }) {
  const { node, isEntry, isExit, runStatus } = data;
  return (
    <div
      className="sb-node-sketch sb-kind-agent"
      style={{ '--sb-jitter': `${jitterDegForId(id)}deg` } as React.CSSProperties}
      data-testid={`node-${id}`}
      data-run-status={runStatus ?? 'idle'}
      data-node-kind="agent"
    >
      {isEntry && <span className="sb-node-corner-tag">START</span>}
      {!isEntry && isExit && <span className="sb-node-corner-tag">END</span>}
      <Handle type="target" position={FlowPosition.Left} />
      <div className="sb-node-badge">AGENT{node.template ? ` · ${node.template}` : ''}</div>
      <div className="sb-node-title">{node.title}</div>
      <div className="sb-node-intent-preview">{node.intent || '(no intent set)'}</div>
      <Handle type="source" position={FlowPosition.Right} />
    </div>
  );
}

export default AgentNode;
