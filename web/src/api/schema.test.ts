import { describe, it, expect } from 'vitest';
import { normalizeGraph } from './schema';
import type { SwarmGraph } from './schema';

// The narrow-once boundary this file tests (PLAN.md's frontend
// conventions arbitration + the task's normalization requirement):
// `normalizeGraph` is the one place a wire-shaped `SwarmGraph` (whose
// `nodes`/`edges`/`stateFields` and nested spec arrays are optional,
// per the generated `types.ts`) is converted into a `NormalizedSwarmGraph`
// where those fields are guaranteed-present arrays. Every other module
// in this package (graphStore.ts above all) assumes that invariant
// already holds and never re-checks it -- these tests are what make
// that assumption trustworthy.
//
// Deliberate choice: normalize (default missing fields to empty
// arrays), not reject. The server's own `models.py` declares
// `Field(default_factory=list)` for these fields, so a document that
// omits them on the wire is not malformed -- it is exactly what a
// spec-compliant client is allowed to send. Rejecting it would be
// stricter than the schema itself.

describe('normalizeGraph', () => {
  it('normalizes a payload missing nodes/edges/stateFields to empty arrays', () => {
    // Cast through `unknown` deliberately: this constructs exactly the
    // shape a real HTTP response can carry (fields omitted, matching
    // the optional `nodes?`/`edges?`/`stateFields?` in the generated
    // `types.ts`), which the normal `SwarmGraph` TS type (imported from
    // that same generated file) already allows without an assertion.
    const sparse: SwarmGraph = {
      version: 1,
      id: 'sparse-graph',
      name: 'Sparse',
      entryNodeId: 'n1',
      exitNodeId: 'n1',
      model: null,
      updatedAt: '2024-01-01T00:00:00Z',
      // nodes, edges, stateFields all omitted
    };

    const normalized = normalizeGraph(sparse);

    expect(normalized.nodes).toEqual([]);
    expect(normalized.edges).toEqual([]);
    expect(normalized.stateFields).toEqual([]);
    // Every other field passes through untouched.
    expect(normalized.id).toBe('sparse-graph');
    expect(normalized.entryNodeId).toBe('n1');
  });

  it('normalizes nested optional spec arrays (agent/programmatic/decision/join) to empty arrays or null defaults', () => {
    const sparse: SwarmGraph = {
      version: 1,
      id: 'sparse-nodes',
      name: 'Sparse nodes',
      entryNodeId: 'agent1',
      exitNodeId: 'agent1',
      model: null,
      updatedAt: '2024-01-01T00:00:00Z',
      nodes: [
        {
          id: 'agent1',
          kind: 'agent',
          title: 'Agent',
          intent: 'do something',
          position: { x: 0, y: 0 },
          io: { inputType: 'str', outputType: 'str' },
          // reads/writes omitted
          agent: {
            instructions: 'go',
            // tools/delegatesTo omitted
          },
        },
        {
          id: 'prog1',
          kind: 'programmatic',
          title: 'Prog',
          intent: 'compute',
          position: { x: 1, y: 1 },
          io: { inputType: 'str', outputType: 'str' },
          programmatic: {
            // needs/signatureHint omitted
          },
        },
        {
          id: 'dec1',
          kind: 'decision',
          title: 'Decision',
          intent: 'branch',
          position: { x: 2, y: 2 },
          io: { inputType: 'str', outputType: 'str' },
          decision: {
            // branches/note omitted
          },
        },
        {
          id: 'join1',
          kind: 'join',
          title: 'Join',
          intent: 'reduce',
          position: { x: 3, y: 3 },
          io: { inputType: 'str', outputType: 'str' },
          join: {
            reducer: 'list_append',
            // initialFactory omitted
          },
        },
      ],
    };

    const normalized = normalizeGraph(sparse);
    expect(normalized.nodes).toHaveLength(4);

    const [agentNode, progNode, decisionNode, joinNode] = normalized.nodes;
    expect(agentNode!.reads).toEqual([]);
    expect(agentNode!.writes).toEqual([]);
    expect(agentNode!.agent!.tools).toEqual([]);
    expect(agentNode!.agent!.delegatesTo).toEqual([]);

    expect(progNode!.programmatic!.needs).toEqual([]);
    expect(progNode!.programmatic!.signatureHint).toBeNull();

    expect(decisionNode!.decision!.branches).toEqual([]);
    expect(decisionNode!.decision!.note).toBeNull();

    expect(joinNode!.join!.initialFactory).toBeNull();
  });

  it('round-trips a well-formed payload unchanged', () => {
    const complete: SwarmGraph = {
      version: 1,
      id: 'complete-graph',
      name: 'Complete',
      entryNodeId: 'a',
      exitNodeId: 'b',
      stateFields: [{ name: 'notes', type: 'str', default: '""', description: null }],
      nodes: [
        {
          id: 'a',
          kind: 'agent',
          title: 'A',
          intent: 'search',
          position: { x: 0, y: 0 },
          template: 'websearch',
          io: { inputType: 'str', outputType: 'str' },
          reads: [],
          writes: ['notes'],
          agent: { instructions: 'search', tools: ['web_search'], delegatesTo: [] },
          programmatic: null,
          decision: null,
          join: null,
        },
        {
          id: 'b',
          kind: 'programmatic',
          title: 'B',
          intent: 'format',
          position: { x: 200, y: 0 },
          template: null,
          io: { inputType: 'str', outputType: 'str' },
          reads: ['notes'],
          writes: [],
          agent: null,
          programmatic: { needs: [], signatureHint: 'def run(x: str) -> str' },
          decision: null,
          join: null,
        },
      ],
      edges: [{ kind: 'seq', id: 'e1', source: 'a', target: 'b', label: null }],
      model: null,
      updatedAt: '2024-01-01T00:00:00Z',
    };

    const normalized = normalizeGraph(complete);

    // Structurally identical -- normalizing an already-complete
    // document changes nothing observable.
    expect(normalized).toEqual(complete);
  });
});
