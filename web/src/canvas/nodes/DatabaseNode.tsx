import { Handle, Position as FlowPosition, type NodeProps } from '@xyflow/react';
import { jitterDegForId } from '../jitter';
import type { DatabaseKind, SwarmNode } from '../../api/schema';
import type { SwarmNodeData } from './AgentNode';

/** The three database kinds share one badge vocabulary: the engine, kept to
 * three characters so a node reads at a glance on a zoomed-out canvas. */
export const DATABASE_BADGES: Record<DatabaseKind, string> = {
  sql: 'SQL',
  nosql: 'DOC',
  vector: 'VEC',
};

/**
 * One line naming what this node will actually run, taken from the node's own
 * spec -- never from a starter copy: the Inspector is the only thing that
 * changes these fields, so what is drawn here is what the emitted step runs.
 * A node whose operation is still empty says so, which is the same state
 * Phase 1 reports as `db_empty_operation`.
 */
export function databaseOperationSummary(node: SwarmNode): string {
  switch (node.kind) {
    case 'sql': {
      const query = (node.sql?.query ?? '').trim();
      if (!query) return 'no query yet';
      const firstLine = query.split('\n', 1)[0] ?? query;
      return `${node.sql?.write ? 'write' : 'read'} · ${firstLine}`;
    }
    case 'nosql': {
      const collection = (node.nosql?.collection ?? '').trim();
      const operation = node.nosql?.operation ?? 'find';
      if (!collection) return 'no collection yet';
      return `${collection} · ${operation}${operation === 'insert_one' ? ' (write)' : ''}`;
    }
    case 'vector': {
      const collection = (node.vector?.collection ?? '').trim();
      if (!collection) return 'no collection yet';
      return `${collection} · top ${node.vector?.topK ?? 4}`;
    }
    default:
      return '';
  }
}

/**
 * The canvas card for `sql` / `nosql` / `vector` nodes -- deliberately **one**
 * component for all three: they differ only in badge and in the one-line
 * summary above, and three near-identical files would be three places to keep
 * the same hooks (`data-testid`, `data-node-kind`, the run-status attribute,
 * the corner tags) in step.
 */
export function DatabaseNode({ id, data }: NodeProps & { data: SwarmNodeData }) {
  const { node, isEntry, isExit, runStatus } = data;
  // Canvas routes exactly the database kinds here, so this is a narrowing of
  // what React Flow already guarantees, not a fallback.
  const kind = node.kind as DatabaseKind;

  return (
    <div
      className={`sb-node-sketch sb-kind-${kind}`}
      style={{ '--sb-jitter': `${jitterDegForId(id)}deg` } as React.CSSProperties}
      data-testid={`node-${id}`}
      data-run-status={runStatus ?? 'idle'}
      data-node-kind={kind}
    >
      {isEntry && <span className="sb-node-corner-tag">START</span>}
      {!isEntry && isExit && <span className="sb-node-corner-tag">END</span>}
      <Handle type="target" position={FlowPosition.Left} />
      <div className="sb-node-badge">{DATABASE_BADGES[kind]}</div>
      <div className="sb-node-title">{node.title}</div>
      <div className="sb-node-intent-preview">{node.intent || '(no intent set)'}</div>
      <div className="sb-node-db-op">{databaseOperationSummary(node)}</div>
      <Handle type="source" position={FlowPosition.Right} />
    </div>
  );
}

export default DatabaseNode;
