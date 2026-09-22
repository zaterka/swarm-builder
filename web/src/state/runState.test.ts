import { describe, expect, it } from 'vitest';
import type { SwarmEdge } from '../api/schema';
import type { RunState } from './runState';
import {
  applyRunEvent,
  applyRunSnapshot,
  applyRunStreamError,
  deriveRunDisplayStatuses,
  initialRunState,
  mergeRunPatch,
  runStateFromRecord,
  runTraceRows,
} from './runState';
import { compileSucceededSnapshotFixture } from '../../test/fixtures';

const frame = (type: string, data: unknown, id = '1') => ({ id, type, data });

describe('applyRunEvent', () => {
  it('walks compiling -> starting -> started and records the model', () => {
    let state: RunState = { ...initialRunState(), status: 'queued' };
    state = applyRunEvent(state, frame('run', { status: 'compiling' }));
    expect(state.status).toBe('running');
    expect(state.progress).toBe('compiling');
    state = applyRunEvent(state, frame('phase', { name: 'review', index: 1, total: 5, status: 'started' }));
    state = applyRunEvent(state, frame('phase', { name: 'review', index: 1, total: 5, status: 'succeeded' }));
    expect(state.compilePhases).toEqual([{ name: 'review', status: 'succeeded', attempt: 1 }]);
    state = applyRunEvent(state, frame('run', { status: 'starting', compiled: true }));
    state = applyRunEvent(state, frame('run', { status: 'started', model: 'test-model', input: 'hi' }));
    expect(state.progress).toBe('started');
    expect(state.model).toBe('test-model');
    expect(state.input).toBe('hi');
  });

  it('merges node frames per node and keeps first-start order', () => {
    let state = initialRunState();
    state = applyRunEvent(state, frame('node', { nodeId: 'a', status: 'started', inputs: 'x' }));
    state = applyRunEvent(state, frame('node', { nodeId: 'b', status: 'started', inputs: 'x' }));
    state = applyRunEvent(
      state,
      frame('node', { nodeId: 'a', status: 'succeeded', output: 'y', stateDelta: { topic: 'x' }, durationMs: 7 }),
    );
    expect(state.nodes.a).toMatchObject({
      status: 'succeeded',
      inputs: 'x',
      output: 'y',
      stateDelta: { topic: 'x' },
      durationMs: 7,
      order: 1,
    });
    expect(state.nodes.b?.status).toBe('started');
    expect(runTraceRows(state).map((row) => row.nodeId)).toEqual(['a', 'b']);
  });

  it('keeps a failed node error and finishes on error', () => {
    let state = initialRunState();
    state = applyRunEvent(state, frame('node', { nodeId: 'a', status: 'started' }));
    state = applyRunEvent(
      state,
      frame('node', { nodeId: 'a', status: 'failed', error: 'ValueError: nope', traceback: 'Traceback…' }),
    );
    state = applyRunEvent(state, frame('error', { code: 'run_failed', message: 'ValueError: nope' }));
    expect(state.nodes.a).toMatchObject({ status: 'failed', error: 'ValueError: nope' });
    expect(state.status).toBe('failed');
    expect(state.progress).toBe('finished');
    expect(state.error).toBe('ValueError: nope');
  });

  it('parses the done payload into a result', () => {
    let state = initialRunState();
    state = applyRunEvent(
      state,
      frame('done', { output: 'SUMMARY: x', state: { topic: 'x' }, durationMs: 42, model: 'm', compiled: false }),
    );
    expect(state.status).toBe('succeeded');
    expect(state.result).toEqual({
      output: 'SUMMARY: x',
      state: { topic: 'x' },
      durationMs: 42,
      model: 'm',
      compiled: false,
    });
  });

  it('turns log and warning frames into log lines, never [object Object]', () => {
    let state = initialRunState();
    state = applyRunEvent(state, frame('log', { message: 'hello', stream: 'stderr' }));
    state = applyRunEvent(state, frame('warning', { code: 'w', message: 'careful', nodeIds: ['a'] }));
    expect(state.logLines).toEqual(['hello', 'w: careful (nodes: a)']);
  });

  it('ignores unknown frame types', () => {
    const state = initialRunState();
    expect(applyRunEvent(state, frame('mystery', { x: 1 }))).toBe(state);
  });
});

describe('snapshot, patch and stream errors', () => {
  it('a snapshot never blanks nodes or log lines', () => {
    let state = initialRunState();
    state = applyRunEvent(state, frame('node', { nodeId: 'a', status: 'started' }));
    state = applyRunEvent(state, frame('log', { message: 'l' }));
    const snapshot = compileSucceededSnapshotFixture({
      kind: 'run',
      result: { output: 'o', state: {}, durationMs: 1, model: 'm', compiled: true },
      latestEventId: 5,
    });
    state = applyRunSnapshot(state, snapshot);
    expect(state.nodes.a).toBeDefined();
    expect(state.logLines).toEqual(['l']);
    expect(state.status).toBe('succeeded');
    expect(state.result?.output).toBe('o');
    expect(state.latestEventId).toBe('5');
    expect(state.progress).toBe('finished');
  });

  it('an explicit undefined in a patch keeps the existing value', () => {
    const state = { ...initialRunState(), logLines: ['x'] };
    expect(mergeRunPatch(state, { logLines: undefined }).logLines).toEqual(['x']);
    expect(mergeRunPatch(state, { input: 'hi' }).input).toBe('hi');
  });

  it('a stream error only degrades a live run', () => {
    const live: RunState = { ...initialRunState(), status: 'running' };
    expect(applyRunStreamError(live, 'lost').status).toBe('failed');
    const done: RunState = { ...initialRunState(), status: 'succeeded' };
    expect(applyRunStreamError(done, 'lost').status).toBe('succeeded');
  });
});

describe('runStateFromRecord', () => {
  it('rebuilds nodes and result from a persisted record', () => {
    const state = runStateFromRecord({
      runId: 'r1',
      graphId: 'g',
      status: 'succeeded',
      createdAt: '2026-09-21T10:00:00+00:00',
      finishedAt: '2026-09-21T10:00:05+00:00',
      input: 'hi',
      output: 'out',
      state: { topic: 'hi' },
      error: null,
      model: 'm',
      durationMs: 5000,
      compiled: true,
      nodes: { a: { status: 'succeeded', inputs: 'hi', output: 'out', stateDelta: null, error: null, durationMs: 3 } },
    });
    expect(state.runId).toBe('r1');
    expect(state.status).toBe('succeeded');
    expect(state.nodes.a?.output).toBe('out');
    expect(state.result?.compiled).toBe(true);
    expect(state.input).toBe('hi');
  });
});

describe('deriveRunDisplayStatuses', () => {
  const nodes = [
    { id: 'classify', kind: 'programmatic' },
    { id: 'route', kind: 'decision' },
    { id: 'big', kind: 'agent' },
    { id: 'small', kind: 'agent' },
  ];
  const edges: SwarmEdge[] = [
    { kind: 'seq', id: 'e1', source: 'classify', target: 'route' },
    { kind: 'branch', id: 'e2', source: 'route', target: 'big', match: 'big' },
    { kind: 'branch', id: 'e3', source: 'route', target: 'small', match: 'small' },
  ];

  it('is idle everywhere before a run', () => {
    const statuses = deriveRunDisplayStatuses(initialRunState(), nodes, edges);
    expect(Object.values(statuses).every((s) => s === 'idle')).toBe(true);
  });

  it('marks a decision running once its predecessor finished and succeeded once a branch started', () => {
    let state: RunState = { ...initialRunState(), status: 'running' };
    state = applyRunEvent(state, frame('node', { nodeId: 'classify', status: 'started' }));
    expect(deriveRunDisplayStatuses(state, nodes, edges)).toMatchObject({
      classify: 'running',
      route: 'idle',
    });
    state = applyRunEvent(state, frame('node', { nodeId: 'classify', status: 'succeeded', output: 'big' }));
    expect(deriveRunDisplayStatuses(state, nodes, edges).route).toBe('running');
    state = applyRunEvent(state, frame('node', { nodeId: 'big', status: 'started' }));
    expect(deriveRunDisplayStatuses(state, nodes, edges)).toMatchObject({
      classify: 'succeeded',
      route: 'succeeded',
      big: 'running',
      small: 'idle',
    });
  });

  it('paints a failed step red', () => {
    let state: RunState = { ...initialRunState(), status: 'failed' };
    state = applyRunEvent(state, frame('node', { nodeId: 'classify', status: 'failed', error: 'x' }));
    expect(deriveRunDisplayStatuses(state, nodes, edges).classify).toBe('failed');
  });
});
