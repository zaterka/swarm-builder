import { describe, it, expect, afterEach } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { ReactFlowProvider } from '@xyflow/react';
import DatabaseNode, { DATABASE_BADGES, databaseOperationSummary } from './DatabaseNode';
import type { SwarmNode } from '../../api/schema';
import type { SwarmNodeData } from './AgentNode';

// One component serves all three database kinds, so the tests are keyed on what
// actually differs: the badge (`SQL` / `DOC` / `VEC`), the run-status attribute
// the Run view paints, the entry/exit corner tags, and the one-line summary
// taken from the node's own spec.

afterEach(cleanup);

function databaseNode(overrides: Partial<SwarmNode> = {}): SwarmNode {
  return {
    id: 'tickets',
    kind: 'nosql',
    title: 'Support tickets',
    intent: 'Find the tickets for the incoming status.',
    position: { x: 0, y: 0 },
    template: null,
    io: { inputType: 'str', outputType: 'list[json]' },
    reads: [],
    writes: [],
    agent: null,
    programmatic: null,
    decision: null,
    join: null,
    sql: null,
    nosql: {
      collection: 'tickets',
      operation: 'find',
      filter: { status: '$input' },
      limit: 20,
      seed: [],
      note: null,
    },
    vector: null,
    ...overrides,
  };
}

/** The card reads only `id` and `data`; the rest of `NodeProps` is React
 * Flow's own plumbing, so the test renders the component through a narrower
 * signature rather than inventing internals. */
const Card = DatabaseNode as unknown as (props: {
  id: string;
  data: SwarmNodeData;
}) => React.ReactElement;

function renderNode(node: SwarmNode, props: { isEntry?: boolean; isExit?: boolean; runStatus?: 'running' } = {}) {
  return render(
    // `<Handle>` reads React Flow's store, exactly as it does inside Canvas.
    <ReactFlowProvider>
      <Card
        id={node.id}
        data={{
          node,
          isEntry: props.isEntry ?? false,
          isExit: props.isExit ?? false,
          runStatus: props.runStatus ?? 'idle',
        }}
      />
    </ReactFlowProvider>,
  );
}

describe('DatabaseNode', () => {
  it('badges each database kind distinctly and tags its node kind', () => {
    const cases: [SwarmNode, string][] = [
      [databaseNode({ kind: 'sql', sql: { query: 'SELECT 1', seedSql: 's', write: false, note: null }, nosql: null }), 'SQL'],
      [databaseNode(), 'DOC'],
      [
        databaseNode({
          kind: 'vector',
          nosql: null,
          vector: { collection: 'docs', topK: 4, minScore: 0, seed: [], note: null },
        }),
        'VEC',
      ],
    ];

    for (const [node, badge] of cases) {
      const { unmount } = renderNode(node);
      expect(screen.getByText(badge)).toBeTruthy();
      expect(DATABASE_BADGES[node.kind as 'sql' | 'nosql' | 'vector']).toBe(badge);
      const card = screen.getByTestId(`node-${node.id}`);
      expect(card.getAttribute('data-node-kind')).toBe(node.kind);
      expect(card.className).toContain(`sb-kind-${node.kind}`);
      expect(screen.getByText(node.title).className).toBe('sb-node-title');
      unmount();
    }
  });

  it('carries the same hooks the other node components use', () => {
    renderNode(databaseNode({ intent: '' }), { isEntry: true, runStatus: 'running' });

    const card = screen.getByTestId('node-tickets');
    expect(card.getAttribute('data-run-status')).toBe('running');
    expect(screen.getByText('START').className).toBe('sb-node-corner-tag');
    expect(screen.getByText('(no intent set)').className).toBe('sb-node-intent-preview');
    expect(screen.getByText('tickets · find').className).toBe('sb-node-db-op');
  });

  it('shows EXIT only when it is the exit and not the entry', () => {
    const { unmount } = renderNode(databaseNode(), { isExit: true });
    expect(screen.getByText('END')).toBeTruthy();
    unmount();
    renderNode(databaseNode(), { isEntry: true, isExit: true });
    expect(screen.getByText('START')).toBeTruthy();
    expect(screen.queryByText('END')).toBeNull();
  });

  it('summarizes the node window from its own spec, including the empty case', () => {
    expect(
      databaseOperationSummary(
        databaseNode({ kind: 'sql', nosql: null, sql: { query: '', seedSql: '', write: false, note: null } }),
      ),
    ).toBe('no query yet');
    expect(
      databaseOperationSummary(
        databaseNode({
          kind: 'sql',
          nosql: null,
          sql: { query: 'SELECT 1\nFROM t', seedSql: 's', write: true, note: null },
        }),
      ),
    ).toBe('write · SELECT 1');
    expect(
      databaseOperationSummary(
        databaseNode({
          kind: 'nosql',
          nosql: { collection: 'notes', operation: 'insert_one', filter: {}, limit: 1, seed: [], note: null },
        }),
      ),
    ).toBe('notes · insert_one (write)');
    expect(
      databaseOperationSummary(
        databaseNode({
          kind: 'vector',
          nosql: null,
          vector: { collection: '', topK: 4, minScore: 0, seed: [], note: null },
        }),
      ),
    ).toBe('no collection yet');
  });
});
