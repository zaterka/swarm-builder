import { describe, it, expect } from 'vitest';
import {
  COMPILE_PHASE_NAMES,
  COMPILE_PHASE_STATUSES,
  hasUnseenEvents,
  isCompileEventType,
  parseCompileResult,
  phaseLabel,
  phaseStatusLabel,
  readErrorPayload,
  readLogLine,
  readPhasePayload,
  readWarningPayload,
  resumeCursor,
} from './compileWire';
import { compileDonePayloadFixture, compileSucceededSnapshotFixture, compileRunningSnapshotFixture } from '../../test/fixtures';

// The wire vocabulary this file pins down is the one in `docs/api.md`, not the
// one the frontend used to assume:
//
//   * a phase's status is `started` | `succeeded` | `failed` (never
//     `pending`/`running`/`done`);
//   * a `log` frame's payload is the parsed object `{message}`, so rendering it
//     is `message`, never `String(object)`;
//   * the snapshot's `result` is an open dict on the wire and is narrowed once,
//     here, into the panel-facing `CompileResult`.

describe('compile wire vocabulary', () => {
  it('lists the server\'s phase statuses, not the frontend\'s old ones', () => {
    expect([...COMPILE_PHASE_STATUSES]).toEqual(['started', 'succeeded', 'failed']);
    expect([...COMPILE_PHASE_NAMES]).toEqual(['review', 'scaffold', 'fill', 'boundary', 'validate']);
    expect(isCompileEventType('phase')).toBe(true);
    expect(isCompileEventType('progress')).toBe(false);
  });

  it('labels the real values, and keeps an unknown slug as itself', () => {
    expect(phaseStatusLabel('started')).toBe('running');
    expect(phaseStatusLabel('succeeded')).toBe('done');
    expect(phaseStatusLabel('failed')).toBe('failed');
    expect(phaseStatusLabel(null)).toBe('pending');
    expect(phaseLabel('boundary')).toBe('Boundary check');
    expect(phaseLabel('teleport')).toBe('teleport');
  });

  it('reads a phase frame only when its status is one the pipeline emits', () => {
    expect(readPhasePayload({ name: 'fill', index: 3, total: 5, status: 'started' })).toEqual({
      name: 'fill',
      status: 'started',
      index: 3,
    });
    expect(readPhasePayload({ name: 'fill', status: 'running' })).toBeNull();
    expect(readPhasePayload({ status: 'started' })).toBeNull();
    expect(readPhasePayload('not a phase')).toBeNull();
  });

  it('reads a log frame\'s message whatever else the frame carries', () => {
    expect(readLogLine({ message: 'uv sync: 12 packages resolved' })).toEqual({
      message: 'uv sync: 12 packages resolved',
      code: null,
      severity: null,
      nodeIds: [],
    });
    expect(readLogLine('uv sync: 12 packages resolved').message).toBe('uv sync: 12 packages resolved');
    expect(readLogLine({ code: 'port_type_mismatch', message: 'list[str] -> str' })).toMatchObject({
      message: 'list[str] -> str',
      code: 'port_type_mismatch',
    });
  });

  it('reads warning and error frames structurally', () => {
    expect(readWarningPayload({ code: 'orphan_node', message: 'no edges', nodeIds: ['n1'] })).toEqual({
      code: 'orphan_node',
      message: 'no edges',
      nodeIds: ['n1'],
    });
    expect(readWarningPayload({ message: 'no code' })).toBeNull();
    expect(readErrorPayload({ code: 'phase_failed', message: 'boom', exception: 'PhaseFailureError' })).toEqual({
      code: 'phase_failed',
      message: 'boom',
      exception: 'PhaseFailureError',
    });
    expect(readErrorPayload('boom').message).toBe('boom');
  });

  it('narrows the open `result` dict into the fields the result pane renders', () => {
    expect(parseCompileResult(compileDonePayloadFixture())).toEqual({
      projectPath: '/tmp/swarm-workspace/projects/linear-chat',
      runCommand: compileDonePayloadFixture().runCommand,
      diagram: compileDonePayloadFixture().diagram,
      filledNodeIds: ['intake', 'summarize'],
      attempts: 1,
      model: { provider: 'deepseek-official', model: 'deepseek-flash', source: 'settings-default' },
    });
    // A result a user cannot act on is not a result.
    expect(parseCompileResult({ diagram: 'stateDiagram-v2' })).toBeNull();
    expect(parseCompileResult(null)).toBeNull();
    expect(parseCompileResult('done')).toBeNull();
  });
});

describe('resume decision', () => {
  it('takes the snapshot\'s latestEventId as the cursor', () => {
    expect(resumeCursor(compileRunningSnapshotFixture())).toBe('9');
    expect(resumeCursor(compileRunningSnapshotFixture({ latestEventId: null }))).toBeNull();
  });

  it('knows when a resume would not be served anything', () => {
    const finished = compileSucceededSnapshotFixture();

    // Terminal job, cursor already consumed: replay would be empty.
    expect(hasUnseenEvents(finished, '9')).toBe(false);
    // Terminal job, older cursor: the retained log still holds frames 5..9,
    // including the terminal one that closes the stream cleanly.
    expect(hasUnseenEvents(finished, '4')).toBe(true);
    // No cursor at all: the whole retained log is available.
    expect(hasUnseenEvents(finished, null)).toBe(true);
    // A job with an empty log has nothing to replay either way.
    expect(hasUnseenEvents(compileSucceededSnapshotFixture({ latestEventId: null }), null)).toBe(false);
    // A cursor this build cannot read is treated as "subscribe anyway": the
    // cost of being wrong is an empty replay, never a missed event.
    expect(hasUnseenEvents(finished, 'opaque')).toBe(true);
  });
});
