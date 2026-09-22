import type { CompileSnapshot, SwarmGraph } from '../src/api/schema';

// A review-clean fixture graph -- every edge endpoint refers to a real
// node id, entry/exit are set, and specs match their node kind. Kept
// deliberately separate from the render-only fixture below
// (GROUP6_PLAN.md review finding E3): reusing a render fixture (which
// may contain one-of-each-kind nodes with no coherent wiring) in a
// save/round-trip test would 422 for reasons unrelated to what that
// test is actually checking.
export function reviewCleanFixtureGraph(): SwarmGraph {
  return {
    version: 1,
    id: 'fixture-review-clean',
    name: 'Review-clean fixture',
    entryNodeId: 'search_the_web',
    exitNodeId: 'format_output',
    stateFields: [{ name: 'notes', type: 'str', default: '""', description: null }],
    nodes: [
      {
        id: 'search_the_web',
        kind: 'agent',
        title: 'Search the web',
        intent: 'Search the web for the latest news on this topic.',
        position: { x: 0, y: 0 },
        template: 'websearch',
        io: { inputType: 'str', outputType: 'str' },
        reads: [],
        writes: ['notes'],
        agent: { instructions: 'Search the web.', tools: ['web_search'], delegatesTo: [] },
        programmatic: null,
        decision: null,
        join: null,
      },
      {
        id: 'format_output',
        kind: 'programmatic',
        title: 'Format output',
        intent: 'Format the notes as a bullet list.',
        position: { x: 200, y: 0 },
        template: null,
        io: { inputType: 'str', outputType: 'str' },
        reads: ['notes'],
        writes: [],
        agent: null,
        programmatic: { needs: [], signatureHint: 'def run(notes: str) -> str' },
        decision: null,
        join: null,
      },
    ],
    edges: [
      { kind: 'seq', id: 'edge-1', source: 'search_the_web', target: 'format_output', label: null },
    ],
    model: null,
    updatedAt: '2024-01-01T00:00:00Z',
  };
}

// A render-only fixture: one node of each of the four kinds, and one
// edge of each of the five kinds, purely to exercise the canvas's
// node/edge type registrations. Not guaranteed to pass server review
// (e.g. the fanout arm here has no join partner) -- deliberately not
// used by any save/round-trip assertion (review finding E3).
export function renderOnlyFixtureGraph(): SwarmGraph {
  return {
    version: 1,
    id: 'fixture-render-only',
    name: 'Render-only fixture',
    entryNodeId: 'agent_node',
    exitNodeId: 'join_node',
    stateFields: [{ name: 'payload', type: 'str', default: null, description: null }],
    nodes: [
      {
        id: 'agent_node',
        kind: 'agent',
        title: 'Agent node',
        intent: 'Delegate to a sub-agent and coordinate the answer.',
        position: { x: 0, y: 0 },
        template: 'orchestrator',
        io: { inputType: 'str', outputType: 'str' },
        reads: [],
        writes: ['payload'],
        agent: { instructions: 'Coordinate.', tools: [], delegatesTo: ['programmatic_node'] },
        programmatic: null,
        decision: null,
        join: null,
      },
      {
        id: 'programmatic_node',
        kind: 'programmatic',
        title: 'Programmatic node',
        intent: 'Run some plain Python.',
        position: { x: 250, y: 0 },
        template: null,
        io: { inputType: 'str', outputType: 'str' },
        reads: ['payload'],
        writes: [],
        agent: null,
        programmatic: { needs: [], signatureHint: null },
        decision: null,
        join: null,
      },
      {
        id: 'decision_node',
        kind: 'decision',
        title: 'Decision node',
        intent: 'Classify the payload as big or small.',
        position: { x: 0, y: 150 },
        template: null,
        io: { inputType: 'str', outputType: 'str' },
        reads: [],
        writes: [],
        agent: null,
        programmatic: null,
        decision: { branches: [{ match: 'big', targetNodeId: 'programmatic_node' }], note: null },
        join: null,
      },
      {
        id: 'join_node',
        kind: 'join',
        title: 'Join node',
        intent: 'Join fan-out results.',
        position: { x: 250, y: 150 },
        template: null,
        io: { inputType: 'str', outputType: 'list[str]' },
        reads: [],
        writes: [],
        agent: null,
        programmatic: null,
        decision: null,
        join: { reducer: 'list_append', initialFactory: 'list' },
      },
    ],
    edges: [
      { kind: 'seq', id: 'e-seq', source: 'agent_node', target: 'programmatic_node', label: 'next' },
      { kind: 'branch', id: 'e-branch', source: 'decision_node', target: 'programmatic_node', match: 'big' },
      { kind: 'fanout', id: 'e-fanout', source: 'agent_node', target: 'join_node', joinNodeId: 'join_node' },
      { kind: 'join', id: 'e-join', source: 'programmatic_node', target: 'join_node' },
      { kind: 'delegate', id: 'e-delegate', source: 'agent_node', target: 'programmatic_node' },
    ],
    model: null,
    updatedAt: '2024-01-01T00:00:00Z',
  };
}

// ---------------------------------------------------------------------------
// Compile fixtures
//
// Every payload below is copied from `docs/api.md` -> "Compile endpoints" and
// "SSE event stream", which document what the pipeline really sends. They are
// deliberately *not* round-tripped through any frontend type first, so a test
// using them proves the frontend reads the server's shape rather than its own
// idea of it:
//
//   * the status snapshot is exactly {compileId, graphId, status, createdAt,
//     startedAt, finishedAt, latestEventId, result, error} -- there is no
//     `phases`, `logTail` or `warnings` field on it;
//   * a phase's own status is `started` | `succeeded` | `failed`;
//   * a `log` frame is `{message}`, and a `warning` frame is
//     `{code, message, nodeIds}`.
// ---------------------------------------------------------------------------

/** The compileId from docs/api.md's own examples. */
export const FIXTURE_COMPILE_ID = '5783640a1b0440a8bad3ebda0f6725f7';

/** The `done` payload, byte-for-byte the shape docs/api.md gives in the
 * snapshot's `result` and in the `done` frame ("identical to `result` in the
 * status snapshot"). */
export function compileDonePayloadFixture(): Record<string, unknown> {
  return {
    projectPath: '/tmp/swarm-workspace/projects/linear-chat',
    runCommand:
      'cd /tmp/swarm-workspace/projects/linear-chat && UV_CACHE_DIR=/tmp/uv-cache uv sync && UV_CACHE_DIR=/tmp/uv-cache uv run python validate/dry_run.py',
    diagram: 'stateDiagram-v2\n  intake\n  chat_step\n  summarize\n\n  [*] --> intake\n  intake --> chat_step\n  chat_step --> summarize\n  summarize --> [*]',
    filledNodeIds: ['intake', 'summarize'],
    attempts: 1,
    model: { provider: 'deepseek-official', model: 'deepseek-flash', source: 'settings-default' },
  };
}

/** A status snapshot mid-compile: still running, nothing terminal yet. */
export function compileRunningSnapshotFixture(overrides: Partial<CompileSnapshot> = {}): CompileSnapshot {
  return {
    compileId: FIXTURE_COMPILE_ID,
    graphId: 'fixture-review-clean',
    kind: 'compile',
    status: 'running',
    createdAt: '2026-09-16T23:36:50.614212+00:00',
    startedAt: '2026-09-16T23:36:50.626227+00:00',
    finishedAt: null,
    latestEventId: 9,
    result: null,
    error: null,
    ...overrides,
  };
}

/** A finished, successful status snapshot. */
export function compileSucceededSnapshotFixture(overrides: Partial<CompileSnapshot> = {}): CompileSnapshot {
  return {
    compileId: FIXTURE_COMPILE_ID,
    graphId: 'fixture-review-clean',
    kind: 'compile',
    status: 'succeeded',
    createdAt: '2026-09-16T23:36:50.614212+00:00',
    startedAt: '2026-09-16T23:36:50.626227+00:00',
    finishedAt: '2026-09-16T23:36:53.138777+00:00',
    latestEventId: 9,
    result: compileDonePayloadFixture(),
    error: null,
    ...overrides,
  };
}

/** Raw SSE frames (sse-starlette's real CRLF framing) for events 5-9 of a
 * compiled graph, as docs/api.md renders them. Every payload is the server's
 * own shape, `eventId`/`createdAt` included. */
export function compileSseFramesFixture(): string[] {
  return [
    'id: 5\r\nevent: log\r\ndata: {"message":"model: deepseek-official:deepseek-flash (source=settings-default)","eventId":5,"createdAt":"2026-09-16T23:40:20.053687+00:00"}\r\n\r\n',
    'id: 6\r\nevent: phase\r\ndata: {"name":"scaffold","index":2,"total":5,"status":"started","eventId":6,"createdAt":"2026-09-16T23:40:20.113401+00:00"}\r\n\r\n',
    'id: 7\r\nevent: warning\r\ndata: {"code":"state_field_unused","message":"state field \'notes\' is declared but never read","nodeIds":["search_the_web"],"eventId":7,"createdAt":"2026-09-16T23:40:20.121004+00:00"}\r\n\r\n',
    'id: 8\r\nevent: log\r\ndata: {"message":"scaffolded 7 files","eventId":8,"createdAt":"2026-09-16T23:40:20.190552+00:00"}\r\n\r\n',
    'id: 9\r\nevent: done\r\ndata: {"projectPath":"/tmp/swarm-workspace/projects/linear-chat","runCommand":"cd /tmp/swarm-workspace/projects/linear-chat && UV_CACHE_DIR=/tmp/uv-cache uv sync && UV_CACHE_DIR=/tmp/uv-cache uv run python validate/dry_run.py","diagram":"stateDiagram-v2\\n  intake\\n  chat_step\\n  summarize\\n\\n  [*] --> intake\\n  intake --> chat_step\\n  chat_step --> summarize\\n  summarize --> [*]","filledNodeIds":["intake","summarize"],"attempts":1,"model":{"provider":"deepseek-official","model":"deepseek-flash","source":"settings-default"},"validationSteps":["uv_sync","keyless_import","dry_run"],"eventId":9,"createdAt":"2026-09-16T23:40:22.457911+00:00"}\r\n\r\n',
  ];
}
