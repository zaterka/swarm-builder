import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/react';
import CompilePanel from './CompilePanel';
import { useGraphStore } from '../state/graphStore';
import {
  FIXTURE_COMPILE_ID,
  compileDonePayloadFixture,
  compileRunningSnapshotFixture,
  compileSseFramesFixture,
  compileSucceededSnapshotFixture,
  reviewCleanFixtureGraph,
} from '../../test/fixtures';

// The compile panel's *reload* path, end to end and against the server's real
// payloads:
//
//   * the status snapshot carries `status`, `result`, `error` and
//     `latestEventId` -- no `phases`, no `logTail`, no `warnings`. Applying it
//     must not blank the store's arrays, and the resubscribed stream must
//     refill the phase list and the log from the retained ring buffer
//     (PLAN.md edge case "Browser reload mid-compile").
//   * what is rendered is the server's own message -- the provenance line
//     ("model: provider:model (source=...)") that PLAN.md's model picker
//     depends on -- never `[object Object]`.
//   * a compile that already finished at the cursor this client consumed is
//     *not* resubscribed: an empty response would be retried as a lost
//     connection and end as a reported failure for a compile that succeeded.

const GRAPH_ID = 'fixture-review-clean';
const SESSION_KEY = `swarm-builder:compile:${GRAPH_ID}`;

const HEALTH_BODY = {
  version: '0.1.0',
  dshHome: '/tmp/dsh',
  settingsError: null,
  resolvedModel: { provider: 'deepseek-official', model: 'deepseek-flash', source: 'settings-default' },
  uvAvailable: true,
  uvCacheDir: '/tmp/uv-cache',
  uvCacheWritable: true,
  workspaceDir: '/tmp/swarm-workspace',
  workspaceWritable: true,
  webDistPresent: true,
  compileReady: true,
  blockers: [],
};

const MODELS_BODY = {
  routes: [],
  resolvedDefault: { provider: 'deepseek-official', model: 'deepseek-flash', source: 'settings-default' },
  settingsError: null,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** An SSE response with sse-starlette's real framing, one frame per chunk. */
function eventStreamResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const chunks = frames.map((frame) => encoder.encode(frame));
  let index = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      const chunk = chunks[index];
      if (chunk === undefined) {
        controller.close();
        return;
      }
      index += 1;
      controller.enqueue(chunk);
    },
  });
  return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } });
}

interface FetchStubHandlers {
  /** GET /api/compile/:compileId */
  snapshot: () => Response;
  /** GET /api/compile/:compileId/events -- absent means "no stream expected". */
  events?: () => Response;
}

function installFetchStub(handlers: FetchStubHandlers): Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>> {
  const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(
    (input) => {
      const url = String(input);
      if (url.endsWith('/api/health')) return Promise.resolve(jsonResponse(HEALTH_BODY));
      if (url.endsWith('/api/models')) return Promise.resolve(jsonResponse(MODELS_BODY));
      if (url.endsWith('/review')) return Promise.resolve(jsonResponse({ ok: true, errors: [], warnings: [] }));
      if (url.endsWith('/export')) return Promise.resolve(jsonResponse({ detail: 'no compiled project yet' }, 404));
      if (url.endsWith('/events')) {
        return Promise.resolve(
          handlers.events ? handlers.events() : jsonResponse({ detail: 'no stream expected in this test' }, 404),
        );
      }
      if (url.includes('/api/compile/')) return Promise.resolve(handlers.snapshot());
      return Promise.resolve(jsonResponse({ detail: `unstubbed request: ${url}` }, 404));
    },
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function eventStreamCalls(fetchMock: Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>) {
  return fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/events'));
}

beforeEach(() => {
  sessionStorage.clear();
  useGraphStore.getState().loadGraph(reviewCleanFixtureGraph());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe('CompilePanel reload mid-compile', () => {
  it('rebuilds state from the real snapshot and lets the replay refill phases, log and warnings', async () => {
    // The browser was reloaded mid-compile, so sessionStorage holds the job and
    // the last event id this client saw -- event 4, while the job is already at
    // event 9.
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({ compileId: FIXTURE_COMPILE_ID, lastEventId: '4' }));

    const fetchMock = installFetchStub({
      snapshot: () => jsonResponse(compileRunningSnapshotFixture()),
      events: () => eventStreamResponse(compileSseFramesFixture()),
    });

    const { container } = render(<CompilePanel />);

    await waitFor(() => {
      expect(useGraphStore.getState().compile.status).toBe('succeeded');
    });

    // The stream was resumed from the client's own cursor, not from scratch and
    // not from the snapshot's newer one.
    const calls = eventStreamCalls(fetchMock);
    expect(calls).toHaveLength(1);
    const headers = calls[0]?.[1]?.headers as Record<string, string> | undefined;
    expect(headers?.['Last-Event-ID']).toBe('4');
    expect(headers?.Accept).toBe('text/event-stream');

    const compile = useGraphStore.getState().compile;

    // S1: the arrays the snapshot does not carry are still arrays, holding what
    // the replay delivered.
    expect(Array.isArray(compile.logLines)).toBe(true);
    expect(Array.isArray(compile.phases)).toBe(true);
    expect(Array.isArray(compile.warnings)).toBe(true);
    expect(compile.logLines).toEqual(['model: deepseek-official:deepseek-flash (source=settings-default)', 'scaffolded 7 files']);
    expect(compile.phases).toEqual([{ name: 'scaffold', status: 'started', attempt: 1 }]);
    expect(compile.warnings).toEqual([
      {
        code: 'state_field_unused',
        message: "state field 'notes' is declared but never read",
        nodeIds: ['search_the_web'],
      },
    ]);
    expect(compile.latestEventId).toBe('9');

    // S2: what the user reads is the server's message, not a stringified
    // object -- including the provenance line the model picker depends on.
    expect(container.querySelector('.sb-log-tail')?.textContent).toBe(
      'model: deepseek-official:deepseek-flash (source=settings-default)\nscaffolded 7 files',
    );
    expect(container.textContent ?? '').not.toContain('[object Object]');

    // A warning renders structurally, like a review finding.
    expect(container.querySelector('.sb-finding-warning')?.textContent).toBe(
      "state_field_unused: state field 'notes' is declared but never read",
    );

    // S3: the phase list shows the server's own vocabulary, with human labels,
    // and the phases no event has reached yet are visibly pending.
    const phaseText = container.querySelector('.sb-phase-list')?.textContent ?? '';
    expect(phaseText).toContain('Scaffold: running');
    expect(phaseText).toContain('Review: pending');
    expect(phaseText).toContain('Validate: pending');

    // The result pane reads the real `result`: `projectPath`, `runCommand`, the
    // diagram (not a `renderedDiagram`, which never existed on the wire) and
    // the route the compile spent.
    expect(container.textContent ?? '').toContain('/tmp/swarm-workspace/projects/linear-chat');
    expect(container.querySelector('.sb-diagram')?.textContent ?? '').toContain('stateDiagram-v2');
    expect(container.textContent ?? '').toContain('Compiled with deepseek-official / deepseek-flash (via settings-default)');

    // Nothing is left to resume once the terminal frame has been consumed.
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it('a fresh tab resumes from the snapshot\'s own cursor instead of replaying the log from the start', async () => {
    // No persisted cursor: this tab never saw an event. docs/api.md gives the
    // snapshot's `latestEventId` as exactly the value such a client passes back
    // as `Last-Event-ID` "when it wants to resume rather than replay from the
    // start", so the stream resumes at the snapshot's cursor and only the
    // events after it arrive.
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({ compileId: FIXTURE_COMPILE_ID }));

    const fetchMock = installFetchStub({
      snapshot: () => jsonResponse(compileRunningSnapshotFixture()),
      events: () =>
        eventStreamResponse([
          'id: 10\r\nevent: log\r\ndata: {"message":"phase 5: uv sync","eventId":10,"createdAt":"2026-09-16T23:40:23.100000+00:00"}\r\n\r\n',
          `id: 11\r\nevent: done\r\ndata: ${JSON.stringify({ ...compileDonePayloadFixture(), eventId: 11, createdAt: '2026-09-16T23:40:24.000000+00:00' })}\r\n\r\n`,
        ]),
    });

    render(<CompilePanel />);

    await waitFor(() => {
      expect(useGraphStore.getState().compile.status).toBe('succeeded');
    });

    const headers = eventStreamCalls(fetchMock)[0]?.[1]?.headers as Record<string, string> | undefined;
    expect(headers?.['Last-Event-ID']).toBe('9');

    const compile = useGraphStore.getState().compile;
    // Only what came after the cursor -- the retained history is not replayed.
    expect(compile.logLines).toEqual(['phase 5: uv sync']);
    expect(compile.result?.projectPath).toBe('/tmp/swarm-workspace/projects/linear-chat');
  });

  it('does not resubscribe a compile that already finished at our cursor, so it cannot report a false failure', async () => {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({ compileId: FIXTURE_COMPILE_ID, lastEventId: '9' }));
    const fetchMock = installFetchStub({
      snapshot: () => jsonResponse(compileSucceededSnapshotFixture()),
    });

    const { container } = render(<CompilePanel />);

    await waitFor(() => {
      expect(useGraphStore.getState().compile.status).toBe('succeeded');
    });
    // Give a would-be subscription a chance to appear before asserting absence.
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(eventStreamCalls(fetchMock)).toHaveLength(0);
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();

    const compile = useGraphStore.getState().compile;
    expect(compile.status).toBe('succeeded');
    expect(compile.error).toBeNull();
    expect(compile.result?.projectPath).toBe('/tmp/swarm-workspace/projects/linear-chat');
    expect(container.textContent ?? '').toContain('/tmp/swarm-workspace/projects/linear-chat');
  });

  it('reports an evicted compile instead of resuming a stream that cannot exist', async () => {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({ compileId: FIXTURE_COMPILE_ID, lastEventId: '4' }));
    const fetchMock = installFetchStub({
      snapshot: () => jsonResponse({ detail: 'no such compile job' }, 404),
    });

    const { container } = render(<CompilePanel />);

    await waitFor(() => {
      expect(useGraphStore.getState().compile.error).not.toBeNull();
    });
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(eventStreamCalls(fetchMock)).toHaveLength(0);
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
    expect(useGraphStore.getState().compile.status).toBe('idle');
    expect(useGraphStore.getState().compile.error).toBe(
      'The previous compile is no longer available on the server. Compile again.',
    );
    expect(container.textContent ?? '').toContain('no longer available on the server');
  });
});
