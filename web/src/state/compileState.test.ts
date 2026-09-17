import { describe, it, expect, beforeEach } from 'vitest';
import type { CompileSseEvent } from '../api/compileWire';
import {
  applyCompileEvent,
  applyCompileSnapshot,
  applyCompileStreamError,
  compilePhaseRows,
  initialCompileState,
  MAX_LOG_LINES,
  type CompileState,
} from './compileState';
import { useGraphStore } from './graphStore';
import {
  FIXTURE_COMPILE_ID,
  compileDonePayloadFixture,
  compileRunningSnapshotFixture,
  compileSucceededSnapshotFixture,
} from '../../test/fixtures';

// The three defects this suite closes, against the server's REAL payload
// shapes (docs/api.md; every payload lives in `test/fixtures.ts` so it is
// written down once, as the wire contract states it):
//
//   S1  the status snapshot carries no `phases`/`logTail`/`warnings`, so
//       applying one must leave those arrays intact -- never `undefined`,
//       which is what made the next `[...logLines, line]` / `.join('\n')`
//       throw after a mid-compile reload;
//   S2  a `log` frame is `{message}` (or a Phase-1 finding's
//       `{message, code, nodeIds, severity}`) and a `warning` frame is
//       `{code, message, nodeIds}` -- so the streamed text is the server's
//       own message, never `[object Object]`;
//   S3  a phase's status is `started` | `succeeded` | `failed`, and a second
//       `started` for the same phase is a retry, not a new phase.

function event(type: string, data: unknown, id = '1'): CompileSseEvent {
  return { id, type, data };
}

function phaseEvent(name: string, status: string, id = '1'): CompileSseEvent {
  return event('phase', { name, index: 1, total: 5, status, eventId: Number(id) }, id);
}

beforeEach(() => {
  useGraphStore.getState().resetCompileState();
});

describe('S1: a status snapshot rebuilds only what it really carries', () => {
  it('has no phases/logTail/warnings field at all (the shape the panel used to trust)', () => {
    // Guard against the fixture itself drifting into the old fiction.
    expect(Object.keys(compileRunningSnapshotFixture()).sort()).toEqual([
      'compileId',
      'createdAt',
      'error',
      'finishedAt',
      'graphId',
      'latestEventId',
      'result',
      'startedAt',
      'status',
    ]);
  });

  it('leaves the streamed phases/log/warnings intact and never goes undefined', () => {
    const store = useGraphStore.getState();
    store.setCompileState({ compileId: FIXTURE_COMPILE_ID, status: 'queued' });

    // What the live stream had already delivered before the reload.
    store.applyCompileEvent(
      event('log', { message: 'model: deepseek-official:deepseek-flash (source=settings-default)' }),
    );
    store.applyCompileEvent(phaseEvent('review', 'started'));
    store.applyCompileEvent(
      event('warning', { code: 'state_field_unused', message: 'never read', nodeIds: ['search_the_web'] }),
    );

    // The reloaded page's snapshot: status, result, error, latestEventId.
    store.applyCompileSnapshot(compileRunningSnapshotFixture());

    const compile = useGraphStore.getState().compile;
    expect(compile.status).toBe('running');
    expect(compile.latestEventId).toBe('9');
    expect(Array.isArray(compile.phases)).toBe(true);
    expect(Array.isArray(compile.logLines)).toBe(true);
    expect(Array.isArray(compile.warnings)).toBe(true);
    expect(compile.phases).toEqual([{ name: 'review', status: 'started', attempt: 1 }]);
    expect(compile.logLines).toEqual(['model: deepseek-official:deepseek-flash (source=settings-default)']);
    expect(compile.warnings).toHaveLength(1);

    // The two operations that used to throw on the very next event.
    expect(() => [...compile.logLines, 'next line']).not.toThrow();
    expect(() => compile.logLines.join('\n')).not.toThrow();
    expect(() => compile.phases.map((phase) => phase.name)).not.toThrow();
  });

  it('treats an explicitly undefined patch field as "leave as is"', () => {
    const store = useGraphStore.getState();
    store.applyCompileEvent(event('log', { message: 'kept' }));

    // Legal under this tsconfig (`Partial<CompileState>` without
    // `exactOptionalPropertyTypes`), and therefore worth refusing.
    store.setCompileState({ logLines: undefined, phases: undefined, warnings: undefined });

    const compile = useGraphStore.getState().compile;
    expect(compile.logLines).toEqual(['kept']);
    expect(compile.phases).toEqual([]);
    expect(compile.warnings).toEqual([]);
  });

  it('reads a terminal snapshot\'s result into the panel-facing shape', () => {
    const next = applyCompileSnapshot(initialCompileState(), compileSucceededSnapshotFixture());
    expect(next.status).toBe('succeeded');
    expect(next.error).toBeNull();
    expect(next.latestEventId).toBe('9');
    expect(next.result).toEqual({
      projectPath: '/tmp/swarm-workspace/projects/linear-chat',
      runCommand: compileDonePayloadFixture().runCommand,
      diagram: compileDonePayloadFixture().diagram,
      filledNodeIds: ['intake', 'summarize'],
      attempts: 1,
      model: { provider: 'deepseek-official', model: 'deepseek-flash', source: 'settings-default' },
    });
    // `diagram` is the wire name; `renderedDiagram` never existed.
    expect(next.result && 'renderedDiagram' in next.result).toBe(false);
  });

  it('ignores a result payload that is not a compile result', () => {
    const next = applyCompileSnapshot(
      initialCompileState(),
      compileSucceededSnapshotFixture({ result: { unexpected: true } }),
    );
    expect(next.result).toBeNull();
    expect(next.status).toBe('succeeded');
  });
});

describe('S2: streamed log lines are the server\'s message', () => {
  it('renders a {message} log frame as that message', () => {
    const next = applyCompileEvent(initialCompileState(), event('log', { message: 'scaffolded 7 files' }));
    expect(next.logLines).toEqual(['scaffolded 7 files']);
    expect(next.logLines.join('\n')).not.toContain('[object Object]');
  });

  it('keeps a Phase-1 error finding\'s code and nodes when it rides a log frame', () => {
    const next = applyCompileEvent(
      initialCompileState(),
      event('log', {
        message: 'node "fan_out" is missing a join',
        code: 'multi_successor_without_join',
        nodeIds: ['fan_out'],
        severity: 'error',
      }),
    );
    expect(next.logLines).toEqual(['multi_successor_without_join: node "fan_out" is missing a join (nodes: fan_out)']);
  });

  it('falls back to readable text for a payload it cannot parse', () => {
    const plain = applyCompileEvent(initialCompileState(), event('log', 'uv sync: 12 packages resolved'));
    expect(plain.logLines).toEqual(['uv sync: 12 packages resolved']);

    const odd = applyCompileEvent(initialCompileState(), event('log', { unexpected: { nested: true } }));
    expect(odd.logLines).toEqual(['{"unexpected":{"nested":true}}']);
    expect(odd.logLines.join('\n')).not.toContain('[object Object]');
  });

  it('caps the tail instead of growing without bound', () => {
    let state: CompileState = initialCompileState();
    for (let i = 0; i < MAX_LOG_LINES + 25; i += 1) {
      state = applyCompileEvent(state, event('log', { message: `line ${i}` }));
    }
    expect(state.logLines).toHaveLength(MAX_LOG_LINES);
    expect(state.logLines[0]).toBe('line 25');
  });

  it('stores a warning frame structurally, like a review finding', () => {
    const next = applyCompileEvent(
      initialCompileState(),
      event('warning', {
        code: 'state_field_unused',
        message: "state field 'notes' is declared but never read",
        nodeIds: ['search_the_web'],
      }),
    );
    expect(next.warnings).toEqual([
      {
        code: 'state_field_unused',
        message: "state field 'notes' is declared but never read",
        nodeIds: ['search_the_web'],
      },
    ]);
    expect(next.logLines).toEqual([]);
  });

  it('shows a malformed warning frame as a log line rather than dropping it', () => {
    const next = applyCompileEvent(initialCompileState(), event('warning', 'legacy free-form warning'));
    expect(next.warnings).toEqual([]);
    expect(next.logLines).toEqual(['legacy free-form warning']);
  });

  it('reads the terminal error frame\'s message, not the whole object', () => {
    const next = applyCompileEvent(
      initialCompileState(),
      event('error', { code: 'phase_failed', message: "Phase 5 failed: 'uv' is not on PATH", exception: 'PhaseFailureError' }),
    );
    expect(next.status).toBe('failed');
    expect(next.error).toBe("Phase 5 failed: 'uv' is not on PATH");
  });
});

describe('S3: the phase vocabulary is the server\'s', () => {
  it('records started/succeeded, and ignores a status outside the vocabulary', () => {
    // What the panel has after POST /api/compile returns: the server accepted
    // the job, the stream has not said anything yet.
    const queued: CompileState = { ...initialCompileState(), status: 'queued' };

    let state = applyCompileEvent(queued, phaseEvent('review', 'started'));
    expect(state.phases).toEqual([{ name: 'review', status: 'started', attempt: 1 }]);
    expect(state.status).toBe('running');

    state = applyCompileEvent(state, phaseEvent('review', 'succeeded'));
    expect(state.phases).toEqual([{ name: 'review', status: 'succeeded', attempt: 1 }]);

    // The frontend's old invented vocabulary must not create a phase row.
    state = applyCompileEvent(state, phaseEvent('review', 'done'));
    expect(state.phases).toEqual([{ name: 'review', status: 'succeeded', attempt: 1 }]);
    state = applyCompileEvent(state, phaseEvent('review', 'running'));
    expect(state.phases).toEqual([{ name: 'review', status: 'succeeded', attempt: 1 }]);
  });

  it('reads a second `started` for one phase as a retry, not a second phase', () => {
    let state = applyCompileEvent(initialCompileState(), phaseEvent('fill', 'started'));
    state = applyCompileEvent(state, phaseEvent('fill', 'started'));
    expect(state.phases).toEqual([{ name: 'fill', status: 'started', attempt: 2 }]);

    state = applyCompileEvent(state, phaseEvent('fill', 'succeeded'));
    expect(state.phases).toEqual([{ name: 'fill', status: 'succeeded', attempt: 2 }]);
  });

  it('promotes a queued job to running on its first event', () => {
    const queued: CompileState = { ...initialCompileState(), status: 'queued' };
    expect(applyCompileEvent(queued, phaseEvent('review', 'started')).status).toBe('running');
    // An event the vocabulary does not know is not evidence of progress.
    expect(applyCompileEvent(queued, event('message', { any: 'thing' })).status).toBe('queued');
  });

  it('labels every phase the pipeline emits, in pipeline order', () => {
    let state = applyCompileEvent(initialCompileState(), phaseEvent('review', 'succeeded'));
    state = applyCompileEvent(state, phaseEvent('scaffold', 'failed'));

    const rows = compilePhaseRows(state.phases);
    expect(rows.map((row) => [row.label, row.statusLabel])).toEqual([
      ['Review', 'done'],
      ['Scaffold', 'failed'],
      ['Fill', 'pending'],
      ['Boundary check', 'pending'],
      ['Validate', 'pending'],
    ]);
  });

  it('shows a phase slug this build does not know, under its own name', () => {
    const state = applyCompileEvent(initialCompileState(), phaseEvent('teleport', 'started'));
    const rows = compilePhaseRows(state.phases);
    expect(rows).toHaveLength(6);
    expect(rows[5]).toMatchObject({ name: 'teleport', label: 'teleport', statusLabel: 'running' });
  });

  it('marks a retry in the row it renders', () => {
    let state = applyCompileEvent(initialCompileState(), phaseEvent('fill', 'started'));
    state = applyCompileEvent(state, phaseEvent('fill', 'succeeded'));
    state = applyCompileEvent(state, phaseEvent('fill', 'started'));

    const fill = compilePhaseRows(state.phases).find((row) => row.name === 'fill');
    expect(fill).toMatchObject({ status: 'started', statusLabel: 'running', attempt: 2 });
  });
});

describe('stream failures never overwrite a terminal outcome', () => {
  it('marks a live job failed', () => {
    const live: CompileState = { ...initialCompileState(), status: 'running' };
    const next = applyCompileStreamError(live, 'compile events stream lost after 5 reconnect attempts');
    expect(next.status).toBe('failed');
    expect(next.error).toBe('compile events stream lost after 5 reconnect attempts');
  });

  it('leaves a compile the server already finished alone', () => {
    // The stream is finite and closes on its terminal frame, so a reader that
    // ends afterwards is expected -- not a reason to report a failure for a
    // compile that succeeded.
    const done = applyCompileSnapshot(initialCompileState(), compileSucceededSnapshotFixture());
    const next = applyCompileStreamError(done, 'stream lost');
    expect(next.status).toBe('succeeded');
    expect(next.error).toBeNull();
    expect(next.result).not.toBeNull();
  });

  it('a `done` frame followed by a stream error still reports success', () => {
    let state = applyCompileEvent(initialCompileState(), event('done', compileDonePayloadFixture()));
    state = applyCompileStreamError(state, 'stream lost');
    expect(state.status).toBe('succeeded');
    expect(state.result?.projectPath).toBe('/tmp/swarm-workspace/projects/linear-chat');
  });
});
