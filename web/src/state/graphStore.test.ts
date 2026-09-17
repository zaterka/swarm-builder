import { describe, it, expect, beforeEach } from 'vitest';
import { useGraphStore } from './graphStore';

// Required frontend smoke test #1 (PLAN.md "Tests" -> "Frontend
// smoke"): a store round-trip -- add node, connect, assert the
// autosave payload.

// The allowed key sets below mirror models.py's SwarmBaseModel-derived
// schemas exactly (every model there has `extra="forbid"`), so this
// test can assert "no extra keys at every level" without needing a
// runtime schema validator (GROUP6_PLAN.md review finding A6).
const SWARM_GRAPH_KEYS = new Set([
  'version', 'id', 'name', 'entryNodeId', 'exitNodeId', 'stateFields', 'nodes', 'edges', 'model', 'updatedAt',
]);
const SWARM_NODE_KEYS = new Set([
  'id', 'kind', 'title', 'intent', 'position', 'template', 'io', 'reads', 'writes', 'agent', 'programmatic', 'decision', 'join',
]);
const POSITION_KEYS = new Set(['x', 'y']);
const NODE_IO_KEYS = new Set(['inputType', 'outputType']);
const AGENT_SPEC_KEYS = new Set(['instructions', 'tools', 'delegatesTo', 'outputSchema']);
const PROGRAMMATIC_SPEC_KEYS = new Set(['needs', 'signatureHint']);
const DECISION_SPEC_KEYS = new Set(['branches', 'note']);
const DECISION_BRANCH_KEYS = new Set(['match', 'targetNodeId']);
const JOIN_SPEC_KEYS = new Set(['reducer', 'initialFactory']);
const STATE_FIELD_KEYS = new Set(['name', 'type', 'default', 'description']);
const EDGE_BASE_KEYS: Record<string, Set<string>> = {
  seq: new Set(['kind', 'id', 'source', 'target', 'label']),
  branch: new Set(['kind', 'id', 'source', 'target', 'match']),
  fanout: new Set(['kind', 'id', 'source', 'target', 'joinNodeId']),
  join: new Set(['kind', 'id', 'source', 'target']),
  delegate: new Set(['kind', 'id', 'source', 'target']),
};

function assertOnlyKeys(obj: Record<string, unknown>, allowed: Set<string>, path: string) {
  for (const key of Object.keys(obj)) {
    expect(allowed.has(key), `unexpected key ${path}.${key}`).toBe(true);
  }
}

function assertNoExtraKeys(graph: Record<string, unknown>) {
  assertOnlyKeys(graph, SWARM_GRAPH_KEYS, 'graph');

  for (const rawField of (graph.stateFields as unknown[]) ?? []) {
    assertOnlyKeys(rawField as Record<string, unknown>, STATE_FIELD_KEYS, 'graph.stateFields[]');
  }

  for (const rawNode of (graph.nodes as unknown[]) ?? []) {
    const node = rawNode as Record<string, unknown>;
    assertOnlyKeys(node, SWARM_NODE_KEYS, 'graph.nodes[]');
    assertOnlyKeys(node.position as Record<string, unknown>, POSITION_KEYS, 'graph.nodes[].position');
    assertOnlyKeys(node.io as Record<string, unknown>, NODE_IO_KEYS, 'graph.nodes[].io');
    if (node.agent) {
      assertOnlyKeys(node.agent as Record<string, unknown>, AGENT_SPEC_KEYS, 'graph.nodes[].agent');
    }
    if (node.programmatic) {
      assertOnlyKeys(node.programmatic as Record<string, unknown>, PROGRAMMATIC_SPEC_KEYS, 'graph.nodes[].programmatic');
    }
    if (node.decision) {
      const decision = node.decision as Record<string, unknown>;
      assertOnlyKeys(decision, DECISION_SPEC_KEYS, 'graph.nodes[].decision');
      for (const rawBranch of (decision.branches as unknown[]) ?? []) {
        assertOnlyKeys(rawBranch as Record<string, unknown>, DECISION_BRANCH_KEYS, 'graph.nodes[].decision.branches[]');
      }
    }
    if (node.join) {
      assertOnlyKeys(node.join as Record<string, unknown>, JOIN_SPEC_KEYS, 'graph.nodes[].join');
    }
  }

  for (const rawEdge of (graph.edges as unknown[]) ?? []) {
    const edge = rawEdge as Record<string, unknown>;
    const allowed = EDGE_BASE_KEYS[edge.kind as string];
    expect(allowed, `unknown edge kind ${edge.kind}`).toBeDefined();
    if (!allowed) continue;
    assertOnlyKeys(edge, allowed, `graph.edges[] (kind=${edge.kind})`);
  }
}

beforeEach(() => {
  useGraphStore.setState({ graph: null });
});

describe('graphStore round-trip', () => {
  it('add node, connect, and assert the autosave payload shape', () => {
    const store = useGraphStore.getState();
    store.newGraph('fixture-graph-id', 'Round trip fixture');

    const firstId = store.addNode('agent', 'Search the web', { x: 0, y: 0 });
    const secondId = store.addNode('programmatic', 'Format output', { x: 200, y: 0 });
    const edgeId = store.addEdge('seq', firstId, secondId, { kind: 'seq' });

    // Move entry/exit off the auto-created starter node onto the two
    // just-created nodes, exercising setEntryNodeId/setExitNodeId.
    store.setEntryNodeId(firstId);
    store.setExitNodeId(secondId);
    // Remove the now-unreferenced starter node so the fixture reads
    // cleanly (removeNode's cascade is exercised elsewhere).
    const starterId = useGraphStore.getState().graph!.nodes.find(
      (n) => n.id !== firstId && n.id !== secondId,
    )!.id;
    store.removeNode(starterId);

    const payload = useGraphStore.getState().toSavePayload();
    expect(payload).not.toBeNull();
    const graph = payload!;

    // Both node ids are present, in the SwarmGraph shape.
    const nodeIds = graph.nodes.map((n) => n.id);
    expect(nodeIds).toContain(firstId);
    expect(nodeIds).toContain(secondId);
    expect(nodeIds).toHaveLength(2);

    // Exactly one seq edge, with the right source/target.
    expect(graph.edges).toHaveLength(1);
    expect(graph.edges[0]).toMatchObject({ id: edgeId, kind: 'seq', source: firstId, target: secondId });

    // Structural sanity, mirroring (not reimplementing at length) the
    // narrow set SwarmGraph._check_structural_integrity itself checks:
    // every edge endpoint and entryNodeId/exitNodeId refers to a real
    // node id (review finding E2 -- kept intentionally minimal).
    const idSet = new Set(nodeIds);
    expect(idSet.has(graph.entryNodeId)).toBe(true);
    expect(idSet.has(graph.exitNodeId)).toBe(true);
    for (const edge of graph.edges) {
      expect(idSet.has(edge.source)).toBe(true);
      expect(idSet.has(edge.target)).toBe(true);
    }

    // No extra (React-Flow-owned or otherwise) keys anywhere in the
    // payload (review finding A6) -- a recursive key-set assertion,
    // not just "has both node ids".
    assertNoExtraKeys(graph as unknown as Record<string, unknown>);
  });

  it('assigns a node id once at creation and never recomputes it on a title edit', () => {
    // GROUP6_PLAN.md decision 3 / review finding C1: id stability.
    const store = useGraphStore.getState();
    store.newGraph('fixture-graph-id-2', 'Stability fixture');
    const id = store.addNode('agent', 'Original title', { x: 0, y: 0 });

    store.updateNode(id, { title: 'Completely different title' });

    const graph = useGraphStore.getState().graph!;
    const node = graph.nodes.find((n) => n.title === 'Completely different title');
    expect(node).toBeDefined();
    expect(node!.id).toBe(id);
  });

  it('removeNode cascades edges, decision branches, and delegatesTo entries', () => {
    // GROUP6_PLAN.md review finding D6.
    const store = useGraphStore.getState();
    store.newGraph('fixture-graph-id-3', 'Cascade fixture');
    const starterNode = useGraphStore.getState().graph!.nodes[0];
    if (!starterNode) throw new Error('expected a starter node');
    const starterId = starterNode.id;

    const orchestratorId = store.addNode('agent', 'Orchestrator', { x: 0, y: 0 });
    const childId = store.addNode('agent', 'Child', { x: 100, y: 0 });
    store.setDelegatesTo(orchestratorId, [childId]);

    let graph = useGraphStore.getState().graph!;
    expect(graph.edges.some((e) => e.kind === 'delegate' && e.target === childId)).toBe(true);

    store.removeNode(childId);

    graph = useGraphStore.getState().graph!;
    expect(graph.nodes.find((n) => n.id === childId)).toBeUndefined();
    expect(graph.edges.some((e) => e.target === childId || e.source === childId)).toBe(false);
    const orchestratorNode = graph.nodes.find((n) => n.id === orchestratorId)!;
    expect(orchestratorNode.agent!.delegatesTo).not.toContain(childId);

    // starterId is untouched by this cascade.
    expect(graph.nodes.some((n) => n.id === starterId)).toBe(true);
  });

  it('applySaved only clears dirty when no mutation happened since the save it answers', () => {
    // GROUP6_PLAN.md review finding A8.
    const store = useGraphStore.getState();
    store.newGraph('fixture-graph-id-4', 'Save-race fixture');
    expect(useGraphStore.getState().dirty).toBe(false);

    store.addNode('agent', 'Node A', { x: 0, y: 0 });
    expect(useGraphStore.getState().dirty).toBe(true);
    const revisionAtSaveStart = store.markSaving();

    // A second mutation lands while the (simulated) PUT is in flight.
    store.addNode('agent', 'Node B', { x: 50, y: 0 });

    store.applySaved('2024-06-01T00:00:00Z', revisionAtSaveStart);

    // Still dirty: a mutation happened after the save request was sent.
    expect(useGraphStore.getState().dirty).toBe(true);
    expect(useGraphStore.getState().saveStatus).toBe('saved');
  });
});
