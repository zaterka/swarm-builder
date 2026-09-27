import { describe, it, expect } from 'vitest';
import {
  DATABASE_INPUT_TYPES,
  DATABASE_KINDS,
  DATABASE_OUTPUT_TYPES,
  NODE_KINDS,
  NOSQL_OPERATIONS,
  PORT_TYPES,
  REDUCER_IDS,
  TEMPLATE_IDS,
  buildDatabaseStarterMap,
  isDatabaseKind,
  isWriteDatabaseNode,
  normalizeGraph,
} from './schema';
import type {
  DatabaseKind,
  NodeKind,
  NosqlOperation,
  PortType,
  ReducerId,
  SwarmGraph,
  SwarmNode,
  TemplateId,
} from './schema';
import { databaseStartersFixture } from '../../test/fixtures';

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
          sql: null,
          nosql: null,
          vector: null,
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
          sql: null,
          nosql: null,
          vector: null,
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

// ---------------------------------------------------------------------------
// Parity between the hand-maintained const lists and the generated unions
//
// `types.ts` is generated from the server's OpenAPI schema; the `*_IDS` /
// `NODE_KINDS` / `PORT_TYPES` lists are hand-maintained (they have to be --
// a type is not a value). The coverage maps below are what keeps the two in
// step: each is a `Record<Union, true>`, so `tsc -b` fails when the generated
// union gains or loses a member, and the assertions tie the runtime list to
// that map. A stale `types.ts` therefore fails the build rather than silently
// disabling an option in a select.
// ---------------------------------------------------------------------------

const NODE_KIND_COVERAGE: Record<NodeKind, true> = {
  agent: true,
  programmatic: true,
  decision: true,
  join: true,
  sql: true,
  nosql: true,
  vector: true,
};
const PORT_TYPE_COVERAGE: Record<PortType, true> = {
  str: true,
  json: true,
  'list[str]': true,
  'list[json]': true,
};
const TEMPLATE_ID_COVERAGE: Record<TemplateId, true> = {
  chat: true,
  orchestrator: true,
  websearch: true,
};
const REDUCER_ID_COVERAGE: Record<ReducerId, true> = {
  list_append: true,
  list_extend: true,
  dict_update: true,
  sum: true,
};
const DATABASE_KIND_COVERAGE: Record<DatabaseKind, true> = {
  sql: true,
  nosql: true,
  vector: true,
};
const NOSQL_OPERATION_COVERAGE: Record<NosqlOperation, true> = {
  find: true,
  find_one: true,
  count: true,
  insert_one: true,
};

describe('hand-maintained consts match the generated unions', () => {
  it('NODE_KINDS is exactly the generated NodeKind members', () => {
    expect([...NODE_KINDS].sort()).toEqual(Object.keys(NODE_KIND_COVERAGE).sort());
  });

  it('PORT_TYPES is exactly the generated PortType members', () => {
    expect([...PORT_TYPES].sort()).toEqual(Object.keys(PORT_TYPE_COVERAGE).sort());
  });

  it('TEMPLATE_IDS is exactly the generated template-id members', () => {
    expect([...TEMPLATE_IDS].sort()).toEqual(Object.keys(TEMPLATE_ID_COVERAGE).sort());
  });

  it('REDUCER_IDS is exactly the generated ReducerId members', () => {
    expect([...REDUCER_IDS].sort()).toEqual(Object.keys(REDUCER_ID_COVERAGE).sort());
  });

  it('DATABASE_KINDS is exactly the three database NodeKind members', () => {
    expect([...DATABASE_KINDS].sort()).toEqual(Object.keys(DATABASE_KIND_COVERAGE).sort());
    for (const kind of DATABASE_KINDS) {
      expect(NODE_KINDS).toContain(kind);
    }
  });

  it('NOSQL_OPERATIONS is exactly the generated NosqlSpec operation members', () => {
    expect([...NOSQL_OPERATIONS].sort()).toEqual(Object.keys(NOSQL_OPERATION_COVERAGE).sort());
  });
});

describe('database kind ports', () => {
  it('offers each kind exactly the input types review.py can bind', () => {
    // Mirrors `_BINDABLE_INPUT_TYPES`: a similarity search's parameter is the
    // query text, so `json` is not a pair the vector kind can bind.
    expect(DATABASE_INPUT_TYPES.sql).toEqual(['str', 'json', 'list[str]']);
    expect(DATABASE_INPUT_TYPES.nosql).toEqual(['str', 'json', 'list[str]']);
    expect(DATABASE_INPUT_TYPES.vector).toEqual(['str']);
    // ...and `list[json]` is never offered as an input, for any of the three:
    // a database step's input is the *parameter* its operation binds, never rows.
    expect(DATABASE_OUTPUT_TYPES).toEqual(['list[json]']);
    for (const kind of DATABASE_KINDS) {
      for (const type of DATABASE_INPUT_TYPES[kind]) {
        expect(PORT_TYPES).toContain(type);
      }
      // Rows are never a database step's *parameter* -- that is `list[json]`'s
      // only role -- but `list[str]` is a real one for the two kinds whose
      // operation takes a bindable sequence (see the exact tuples above).
      expect(DATABASE_INPUT_TYPES[kind]).not.toContain('list[json]');
      if (kind === 'vector') {
        expect(DATABASE_INPUT_TYPES[kind]).not.toContain('list[str]');
      }
    }
  });
});

describe('database kind helpers', () => {
  it('isDatabaseKind separates the three database kinds from the agent-side kinds', () => {
    for (const kind of DATABASE_KINDS) expect(isDatabaseKind(kind)).toBe(true);
    for (const kind of ['agent', 'programmatic', 'decision', 'join'] as NodeKind[]) {
      expect(isDatabaseKind(kind)).toBe(false);
    }
  });

  it('isWriteDatabaseNode matches review.py: sql.write and nosql.insert_one only', () => {
    expect(isWriteDatabaseNode({ kind: 'sql', sql: { query: 'q', seedSql: 's', write: true, note: null } })).toBe(true);
    expect(isWriteDatabaseNode({ kind: 'sql', sql: { query: 'q', seedSql: 's', write: false, note: null } })).toBe(false);
    expect(
      isWriteDatabaseNode({
        kind: 'nosql',
        nosql: { collection: 'c', operation: 'insert_one', filter: {}, limit: 20, seed: [], note: null },
      }),
    ).toBe(true);
    expect(
      isWriteDatabaseNode({
        kind: 'nosql',
        nosql: { collection: 'c', operation: 'find', filter: {}, limit: 20, seed: [], note: null },
      }),
    ).toBe(false);
    // A vector node has no write operation at all.
    expect(
      isWriteDatabaseNode({
        kind: 'vector',
        vector: { collection: 'c', topK: 4, minScore: 0, seed: [], note: null },
      }),
    ).toBe(false);
    // A database node whose spec never materialized is not a write node.
    expect(isWriteDatabaseNode({ kind: 'sql', sql: null })).toBe(false);
  });
});

describe('buildDatabaseStarterMap', () => {
  it('maps a complete catalog to one entry per kind, with each spec normalized', () => {
    const starters = buildDatabaseStarterMap(databaseStartersFixture());
    expect(starters).not.toBeNull();
    expect(starters!.sql?.spec.query).toBe('SELECT id FROM widgets WHERE name = :input');
    expect(starters!.sql?.io).toEqual({ inputType: 'str', outputType: 'list[json]' });
    expect(starters!.nosql?.spec.collection).toBe('fixture_notes');
    expect(starters!.vector?.spec.topK).toBe(2);
    // A starter whose kind is known but whose spec omits a defaulted field is
    // normalized exactly like a document's spec is.
    const [sql] = databaseStartersFixture();
    const sparse = buildDatabaseStarterMap([
      { ...sql!, spec: sparseSqlSpec },
      ...databaseStartersFixture().slice(1),
    ]);
    expect(sparse!.sql?.spec).toEqual({
      query: sparseSqlSpec.query,
      seedSql: sparseSqlSpec.seedSql,
      write: false,
      note: null,
    });
  });

  it('returns null when a kind is missing, so the palette disables all three', () => {
    expect(buildDatabaseStarterMap(databaseStartersFixture().slice(0, 2))).toBeNull();
    expect(buildDatabaseStarterMap([])).toBeNull();
  });

  it('drops an entry whose kind and spec disagree', () => {
    const [sql, nosql, vector] = databaseStartersFixture();
    // A vector spec served under kind 'sql' must not become a SQL node's spec.
    const mismatched = buildDatabaseStarterMap([{ ...sql!, spec: vector!.spec }, nosql!, vector!]);
    expect(mismatched).toBeNull();
  });
});

// Two payloads the server *accepts* but the generated type marks as required:
// pydantic lists a defaulted scalar (`write`, `operation`, `limit`) in the
// JSON schema's `required`, so `types.ts` has no way to say "a client may omit
// this". An older or hand-written document is exactly this shape, and
// `normalizeGraph` is what makes the rest of the app never see the difference.
const sparseSqlSpec = { query: 'SELECT 1', seedSql: '' } as unknown as NonNullable<SwarmNode['sql']>;
const sparseNosqlSpec = { collection: 'notes' } as unknown as NonNullable<SwarmNode['nosql']>;

describe('normalizeGraph on database specs', () => {
  it('fills the defaulted fields of a sparse database spec and leaves the others null', () => {
    const graph: SwarmGraph = {
      version: 1,
      id: 'sparse-db',
      name: 'Sparse database document',
      entryNodeId: 'sql1',
      exitNodeId: 'sql1',
      model: null,
      updatedAt: '2024-01-01T00:00:00Z',
      nodes: [
        {
          id: 'sql1',
          kind: 'sql',
          title: 'Lookup',
          intent: '',
          position: { x: 0, y: 0 },
          io: { inputType: 'str', outputType: 'list[json]' },
          sql: sparseSqlSpec,
        },
        {
          id: 'nosql1',
          kind: 'nosql',
          title: 'Find',
          intent: '',
          position: { x: 1, y: 1 },
          io: { inputType: 'str', outputType: 'list[json]' },
          nosql: sparseNosqlSpec,
        },
      ],
    };

    const normalized = normalizeGraph(graph);
    const [sqlNode, nosqlNode] = normalized.nodes;

    expect(sqlNode!.sql).toEqual({ query: 'SELECT 1', seedSql: '', write: false, note: null });
    expect(sqlNode!.nosql).toBeNull();
    expect(sqlNode!.vector).toBeNull();
    expect(nosqlNode!.nosql).toEqual({
      collection: 'notes',
      operation: 'find',
      filter: {},
      limit: 20,
      seed: [],
      note: null,
    });
    expect(nosqlNode!.sql).toBeNull();
  });
});
