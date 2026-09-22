import { describe, expect, it } from 'vitest';
import { formatRunValue, parseRunResult, readRunNodePayload, readRunStagePayload } from './runWire';

describe('readRunStagePayload', () => {
  it('reads the three stages and ignores anything else', () => {
    expect(readRunStagePayload({ status: 'compiling' })).toEqual({
      status: 'compiling',
      model: null,
      input: undefined,
      compiled: false,
    });
    expect(readRunStagePayload({ status: 'started', model: 'm', input: { a: 1 }, compiled: true })).toEqual({
      status: 'started',
      model: 'm',
      input: { a: 1 },
      compiled: true,
    });
    expect(readRunStagePayload({ status: 'bogus' })).toBeNull();
    expect(readRunStagePayload('text')).toBeNull();
  });
});

describe('readRunNodePayload', () => {
  it('distinguishes an absent field from a null value', () => {
    const started = readRunNodePayload({ nodeId: 'a', status: 'started', inputs: null });
    expect(started?.inputs).toBeNull();
    expect(started?.output).toBeUndefined();
    const done = readRunNodePayload({ nodeId: 'a', status: 'succeeded', output: [1], durationMs: 2.5 });
    expect(done?.output).toEqual([1]);
    expect(done?.durationMs).toBe(2.5);
  });

  it('rejects frames without a node id or with an unknown status', () => {
    expect(readRunNodePayload({ status: 'started' })).toBeNull();
    expect(readRunNodePayload({ nodeId: 'a', status: 'done' })).toBeNull();
  });
});

describe('parseRunResult', () => {
  it('needs an output key and a state object', () => {
    expect(parseRunResult({ output: null, state: {} })).toEqual({
      output: null,
      state: {},
      durationMs: null,
      model: null,
      compiled: false,
    });
    expect(parseRunResult({ state: {} })).toBeNull();
    expect(parseRunResult({ output: 'x', state: 'nope' })).toBeNull();
    // A compile result is not a run result.
    expect(parseRunResult({ projectPath: '/p', runCommand: 'uv run' })).toBeNull();
  });
});

describe('formatRunValue', () => {
  it('shows strings verbatim and objects as JSON', () => {
    expect(formatRunValue('hi')).toBe('hi');
    expect(formatRunValue({ a: 1 })).toBe('{\n  "a": 1\n}');
    expect(formatRunValue(undefined)).toBe('');
  });

  it('unwraps the tracer truncation marker', () => {
    expect(formatRunValue({ __preview__: 'abc', __truncated__: true })).toBe('abc\n… (truncated)');
    expect(formatRunValue({ __preview__: 'abc', __truncated__: false })).toBe('abc');
  });
});
