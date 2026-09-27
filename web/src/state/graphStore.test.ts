import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { useGraphStore, starterPatch } from './graphStore';
import { api } from '../api/client';
import { buildDatabaseStarterMap } from '../api/schema';
import { databaseStartersFixture } from '../../test/fixtures';

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
  'sql', 'nosql', 'vector',
]);
const POSITION_KEYS = new Set(['x', 'y']);
const NODE_IO_KEYS = new Set(['inputType', 'outputType']);
const AGENT_SPEC_KEYS = new Set(['instructions', 'tools', 'delegatesTo', 'outputSchema']);
const PROGRAMMATIC_SPEC_KEYS = new Set(['needs', 'signatureHint']);
const DECISION_SPEC_KEYS = new Set(['branches', 'note']);
const DECISION_BRANCH_KEYS = new Set(['match', 'targetNodeId']);
const JOIN_SPEC_KEYS = new Set(['reducer', 'initialFactory']);
const SQL_SPEC_KEYS = new Set(['query', 'seedSql', 'write', 'note']);
const NOSQL_SPEC_KEYS = new Set(['collection', 'operation', 'filter', 'limit', 'seed', 'note']);
const VECTOR_SPEC_KEYS = new Set(['collection', 'topK', 'minScore', 'seed', 'note']);
const VECTOR_DOCUMENT_KEYS = new Set(['id', 'text', 'metadata']);
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
    if (node.sql) {
      assertOnlyKeys(node.sql as Record<string, unknown>, SQL_SPEC_KEYS, 'graph.nodes[].sql');
    }
    if (node.nosql) {
      assertOnlyKeys(node.nosql as Record<string, unknown>, NOSQL_SPEC_KEYS, 'graph.nodes[].nosql');
    }
    if (node.vector) {
      const vector = node.vector as Record<string, unknown>;
      assertOnlyKeys(vector, VECTOR_SPEC_KEYS, 'graph.nodes[].vector');
      for (const rawDocument of (vector.seed as unknown[]) ?? []) {
        assertOnlyKeys(rawDocument as Record<string, unknown>, VECTOR_DOCUMENT_KEYS, 'graph.nodes[].vector.seed[]');
      }
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
  useGraphStore.setState({
    graph: null,
    databaseStarters: null,
    databaseStartersStatus: 'idle',
    databaseStartersError: null,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
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

describe('reconnectEdge', () => {
  it('retargets a seq edge and keeps its kind', () => {
    const store = useGraphStore.getState();
    store.newGraph('reconnect-seq', 'Seq reconnect');
    const a = store.addNode('agent', 'A', { x: 0, y: 0 });
    const b = store.addNode('programmatic', 'B', { x: 100, y: 0 });
    const c = store.addNode('programmatic', 'C', { x: 200, y: 0 });
    const id = store.addEdge('seq', a, b, { kind: 'seq' });

    expect(store.reconnectEdge(id, a, c)).toBe(true);

    const edge = useGraphStore.getState().graph!.edges.find((e) => e.id === id)!;
    expect(edge).toMatchObject({ kind: 'seq', source: a, target: c });
  });

  it('moves a branch edge target and retargets the matching DecisionSpec entry', () => {
    const store = useGraphStore.getState();
    store.newGraph('reconnect-branch-target', 'Branch target reconnect');
    const decision = store.addNode('decision', 'Route', { x: 0, y: 0 });
    const a = store.addNode('agent', 'A', { x: 100, y: 0 });
    const b = store.addNode('agent', 'B', { x: 200, y: 0 });
    store.addDecisionBranch(decision, 'yes', a);
    const id = useGraphStore.getState().graph!.edges.find((e) => e.kind === 'branch')!.id;

    expect(store.reconnectEdge(id, decision, b)).toBe(true);

    const graph = useGraphStore.getState().graph!;
    const decisionNode = graph.nodes.find((n) => n.id === decision)!;
    expect(decisionNode.decision!.branches).toEqual([{ match: 'yes', targetNodeId: b }]);
    expect(graph.edges.find((e) => e.id === id)).toMatchObject({ source: decision, target: b });
  });

  it('moves a branch edge to another decision and relocates the spec entry', () => {
    const store = useGraphStore.getState();
    store.newGraph('reconnect-branch-source', 'Branch source reconnect');
    const d1 = store.addNode('decision', 'D1', { x: 0, y: 0 });
    const d2 = store.addNode('decision', 'D2', { x: 200, y: 0 });
    const target = store.addNode('agent', 'Target', { x: 100, y: 100 });
    store.addDecisionBranch(d1, 'yes', target);
    const id = useGraphStore.getState().graph!.edges.find((e) => e.kind === 'branch')!.id;

    expect(store.reconnectEdge(id, d2, target)).toBe(true);

    const graph = useGraphStore.getState().graph!;
    const oldDecision = graph.nodes.find((n) => n.id === d1)!;
    const newDecision = graph.nodes.find((n) => n.id === d2)!;
    expect(oldDecision.decision!.branches).toEqual([]);
    expect(newDecision.decision!.branches).toEqual([{ match: 'yes', targetNodeId: target }]);
  });

  it('refuses to move a branch edge onto a non-decision source', () => {
    const store = useGraphStore.getState();
    store.newGraph('reconnect-branch-refuse', 'Branch refuse');
    const decision = store.addNode('decision', 'Route', { x: 0, y: 0 });
    const target = store.addNode('agent', 'Target', { x: 100, y: 0 });
    const notDecision = store.addNode('agent', 'Not a decision', { x: 200, y: 0 });
    store.addDecisionBranch(decision, 'yes', target);
    const id = useGraphStore.getState().graph!.edges.find((e) => e.kind === 'branch')!.id;
    const before = useGraphStore.getState().graph!;

    expect(store.reconnectEdge(id, notDecision, target)).toBe(false);
    expect(useGraphStore.getState().graph).toBe(before);
  });

  it('moves a delegate edge to another agent and syncs delegatesTo on both', () => {
    const store = useGraphStore.getState();
    store.newGraph('reconnect-delegate', 'Delegate reconnect');
    const a1 = store.addNode('agent', 'Orchestrator', { x: 0, y: 0 });
    const a2 = store.addNode('agent', 'Other orchestrator', { x: 200, y: 0 });
    const target = store.addNode('agent', 'Child', { x: 100, y: 100 });
    store.setDelegatesTo(a1, [target]);
    const id = useGraphStore.getState().graph!.edges.find((e) => e.kind === 'delegate')!.id;

    expect(store.reconnectEdge(id, a2, target)).toBe(true);

    const graph = useGraphStore.getState().graph!;
    const first = graph.nodes.find((n) => n.id === a1)!;
    const second = graph.nodes.find((n) => n.id === a2)!;
    expect(first.agent!.delegatesTo).toEqual([]);
    expect(second.agent!.delegatesTo).toEqual([target]);
  });

  it('keeps joinNodeId when a fanout edge is retargeted', () => {
    const store = useGraphStore.getState();
    store.newGraph('reconnect-fanout', 'Fanout reconnect');
    const source = store.addNode('agent', 'Source', { x: 0, y: 0 });
    const arm = store.addNode('agent', 'Arm', { x: 100, y: 0 });
    const join = store.addNode('join', 'Join', { x: 100, y: 100 });
    store.addEdge('fanout', source, arm, { kind: 'fanout', joinNodeId: join });
    const id = useGraphStore.getState().graph!.edges.find((e) => e.kind === 'fanout')!.id;
    const newArm = store.addNode('agent', 'New arm', { x: 200, y: 0 });

    expect(store.reconnectEdge(id, source, newArm)).toBe(true);
    expect(useGraphStore.getState().graph!.edges.find((e) => e.id === id)).toMatchObject({
      kind: 'fanout',
      source,
      target: newArm,
      joinNodeId: join,
    });
  });

  it('refuses self-loops and duplicate edges', () => {
    const store = useGraphStore.getState();
    store.newGraph('reconnect-guards', 'Guards');
    const a = store.addNode('agent', 'A', { x: 0, y: 0 });
    const b = store.addNode('agent', 'B', { x: 100, y: 0 });
    const id = store.addEdge('seq', a, b, { kind: 'seq' });

    expect(store.reconnectEdge(id, a, a)).toBe(false);
    expect(store.reconnectEdge(id, a, b)).toBe(false); // unchanged = no-op
  });
});

// ---------------------------------------------------------------------------
// Database kinds: the starter catalog, and what a new node is created from
//
// The catalog is fetched once per session and cached, so the palette can offer
// a database kind only when it has the starter that makes the node
// materializable. `api` is spied rather than fetch-stubbed: the store's
// contract is "ask the client and cache the answer", and the 503-to-
// `StartersUnavailableError` translation is the client's own test.
// ---------------------------------------------------------------------------

function readyStarters() {
  return buildDatabaseStarterMap(databaseStartersFixture())!;
}

describe('database starter catalog', () => {
  it('fetches once, caches, and does not refetch on a second load', async () => {
    const spy = vi.spyOn(api, 'listDatabaseStarters').mockResolvedValue(databaseStartersFixture());

    await useGraphStore.getState().loadDatabaseStarters();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(useGraphStore.getState().databaseStartersStatus).toBe('ready');
    expect(useGraphStore.getState().databaseStarters!.vector!.spec.collection).toBe('fixture_docs');

    // A second app-start call (React StrictMode's double mount, or a second
    // mount of the shell) is a no-op -- the catalog is session-scoped.
    await useGraphStore.getState().loadDatabaseStarters();
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('records an explicit unavailable state and is retryable', async () => {
    const spy = vi
      .spyOn(api, 'listDatabaseStarters')
      .mockRejectedValueOnce(new Error('database starters are not available: 503'))
      .mockResolvedValueOnce(databaseStartersFixture());

    await useGraphStore.getState().loadDatabaseStarters();
    expect(useGraphStore.getState().databaseStarters).toBeNull();
    expect(useGraphStore.getState().databaseStartersStatus).toBe('unavailable');
    expect(useGraphStore.getState().databaseStartersError).toContain('503');

    await useGraphStore.getState().retryDatabaseStarters();
    expect(spy).toHaveBeenCalledTimes(2);
    expect(useGraphStore.getState().databaseStartersStatus).toBe('ready');
    expect(useGraphStore.getState().databaseStartersError).toBeNull();
  });

  it('treats an incomplete catalog as unavailable rather than offering one of the three', async () => {
    vi.spyOn(api, 'listDatabaseStarters').mockResolvedValue(databaseStartersFixture().slice(0, 2));

    await useGraphStore.getState().loadDatabaseStarters();

    expect(useGraphStore.getState().databaseStarters).toBeNull();
    expect(useGraphStore.getState().databaseStartersStatus).toBe('unavailable');
  });

  it('is not disturbed by loading a graph', async () => {
    vi.spyOn(api, 'listDatabaseStarters').mockResolvedValue(databaseStartersFixture());
    await useGraphStore.getState().loadDatabaseStarters();

    useGraphStore.getState().newGraph('fixture-db-graph', 'Database fixture');

    expect(useGraphStore.getState().databaseStartersStatus).toBe('ready');
    expect(starterPatch('sql', useGraphStore.getState().databaseStarters)).toBeDefined();
  });
});

describe('addNode for database kinds', () => {
  it('applies the starter spec AND io for each of the three kinds', () => {
    const store = useGraphStore.getState();
    store.newGraph('fixture-db-graph-2', 'Database node fixture');
    const starters = readyStarters();

    for (const kind of ['sql', 'nosql', 'vector'] as const) {
      const starter = starters[kind]!;
      const patch = starterPatch(kind, starters);
      expect(patch).toBeDefined();

      const id = store.addNode(kind, `A ${kind} node`, { x: 0, y: 0 }, patch);
      const node = useGraphStore.getState().graph!.nodes.find((n) => n.id === id)!;

      expect(node.kind).toBe(kind);
      // The I/O pair is the starter's, which is what makes a fresh node
      // compilable with no further input.
      expect(node.io).toEqual({ inputType: 'str', outputType: 'list[json]' });
      expect(node.template).toBeNull();
      // Exactly one spec field is set, and it is this kind's.
      const specs = { sql: node.sql, nosql: node.nosql, vector: node.vector };
      expect(specs[kind]).toEqual(starter.spec);
      for (const other of ['sql', 'nosql', 'vector'] as const) {
        if (other !== kind) expect(specs[other]).toBeNull();
      }
    }
  });

  it('keeps the mandatory io pair and empty operation fields when called with no patch', () => {
    const store = useGraphStore.getState();
    store.newGraph('fixture-db-graph-3', 'Unmaterialized database node');
    const id = store.addNode('sql', 'Empty SQL node', { x: 0, y: 0 });
    const node = useGraphStore.getState().graph!.nodes.find((n) => n.id === id)!;

    // Empty, never a hand-written default: Phase 1 reports
    // `db_empty_operation` until the user declares an operation.
    expect(node.sql).toEqual({ query: '', seedSql: '', write: false, note: null });
    // ...but the ports are the kind's, so the only thing missing is the
    // operation the user has to type.
    expect(node.io).toEqual({ inputType: 'str', outputType: 'list[json]' });
  });

  it('refuses a patch for another kind rather than applying it', () => {
    const store = useGraphStore.getState();
    store.newGraph('fixture-db-graph-4', 'Mismatched patch');
    const patch = starterPatch('sql', readyStarters())!;

    expect(() => store.addNode('nosql', 'Mismatched', { x: 0, y: 0 }, patch)).toThrow(/sql starter/);
  });

  it('leaves the four agent-side kinds exactly as they were', () => {
    const store = useGraphStore.getState();
    store.newGraph('fixture-db-graph-5', 'Agent-side kinds');
    const id = store.addNode('agent', 'Plain agent', { x: 0, y: 0 });
    const node = useGraphStore.getState().graph!.nodes.find((n) => n.id === id)!;

    expect(node.io).toEqual({ inputType: 'str', outputType: 'str' });
    expect(node.sql).toBeNull();
    expect(node.nosql).toBeNull();
    expect(node.vector).toBeNull();
  });

  it('produces a payload with no extra keys at any level for a database node', () => {
    const store = useGraphStore.getState();
    store.newGraph('fixture-db-graph-6', 'Database payload');
    const sqlId = store.addNode('sql', 'Lookup', { x: 0, y: 0 }, starterPatch('sql', readyStarters()));
    store.setEntryNodeId(sqlId);
    store.setExitNodeId(sqlId);
    const starterId = useGraphStore.getState().graph!.nodes.find((n) => n.id !== sqlId)!.id;
    store.removeNode(starterId);

    const payload = useGraphStore.getState().toSavePayload()!;
    expect(payload.nodes).toHaveLength(1);
    assertNoExtraKeys(payload as unknown as Record<string, unknown>);
  });
});

describe('starterPatch', () => {
  it('is undefined for every kind while the catalog is unavailable', () => {
    expect(starterPatch('sql', null)).toBeUndefined();
    expect(starterPatch('nosql', null)).toBeUndefined();
    expect(starterPatch('vector', null)).toBeUndefined();
  });
});
