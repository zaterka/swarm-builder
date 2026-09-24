import { describe, it, expect, vi, afterEach } from 'vitest';
import { api, subscribeCompileEvents } from './client';
import type { CompileSseEvent } from './compileWire';

// SSE frame-parsing tests against sse-starlette 3.4.11's REAL wire
// format (verified by reading its event.py): frames are separated by
// "\r\n\r\n" (DEFAULT_SEPARATOR = "\r\n", plus one extra separator per
// frame), comment/ping frames have no `data` field and must be
// silently discarded, and a multi-line payload arrives as consecutive
// `data:` lines that must be rejoined with "\n".
//
// GROUP6_PLAN.md review findings B1/B2/B3/B6 -- this is the test suite
// that closes them.

function streamFromChunks(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i]));
        i += 1;
      } else {
        controller.close();
      }
    },
  });
}

function mockEventStreamResponse(chunks: string[]): Response {
  return new Response(streamFromChunks(chunks), {
    status: 200,
    headers: { 'content-type': 'text/event-stream' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('subscribeCompileEvents SSE frame parsing', () => {
  it('parses CRLF-separated frames, ignores ping/comment frames, and rejoins multi-line data', async () => {
    const frames = [
      // A ping/comment frame -- sse-starlette emits these every 15s;
      // must be silently discarded (finding B2).
      ': ping - 12345\r\n\r\n',
      // A normal single-line "phase" event, carrying the payload shape the
      // pipeline really emits (docs/api.md "The five event types": the phase's
      // own status vocabulary is started/succeeded/failed).
      'id: 1\r\nevent: phase\r\ndata: {"name":"review","index":1,"total":5,"status":"started","eventId":1}\r\n\r\n',
      // A multi-line "log" event -- must be rejoined with "\n"
      // (finding B3).
      'id: 2\r\nevent: log\r\ndata: line one\r\ndata: line two\r\n\r\n',
      // A terminal "done" event, split across two chunk boundaries to
      // prove the buffering handles a frame arriving in pieces.
      'id: 3\r\nevent: don',
      'e\r\ndata: {"projectPath":"/tmp/proj"}\r\n\r\n',
    ];

    const fetchMock = vi.fn().mockResolvedValue(mockEventStreamResponse(frames));
    vi.stubGlobal('fetch', fetchMock);

    const received: CompileSseEvent[] = [];
    const lastIds: string[] = [];
    let closed = false;
    await new Promise<void>((resolve, reject) => {
      const close = subscribeCompileEvents('compile-123', {
        onEvent: (evt) => {
          received.push(evt);
          if (evt.type === 'done') {
            close();
            closed = true;
            resolve();
          }
        },
        onLastEventId: (id) => lastIds.push(id),
        onError: (err) => reject(err),
      });
      setTimeout(() => {
        if (!closed) {
          close();
          resolve();
        }
      }, 2000);
    });

    // Exactly 3 dispatched events -- the ping frame produced none.
    expect(received).toHaveLength(3);
    const [first, second, third] = received;
    if (!first || !second || !third) throw new Error('expected 3 defined events');

    expect(first).toMatchObject({ id: '1', type: 'phase' });
    expect(first.data).toEqual({ name: 'review', index: 1, total: 5, status: 'started', eventId: 1 });

    expect(second).toMatchObject({ id: '2', type: 'log' });
    // Multi-line data must be rejoined with "\n" before being handed
    // to onEvent (it isn't valid JSON, so it passes through as the raw
    // joined string).
    expect(second.data).toBe('line one\nline two');

    expect(third).toMatchObject({ id: '3', type: 'done' });
    expect(third.data).toEqual({ projectPath: '/tmp/proj' });

    expect(lastIds).toEqual(['1', '2', '3']);
  });

  it('sends the Last-Event-ID header when reconnecting with a persisted id', async () => {
    const frames = ['id: 42\r\nevent: done\r\ndata: {}\r\n\r\n'];
    const fetchMock = vi.fn().mockResolvedValue(mockEventStreamResponse(frames));
    vi.stubGlobal('fetch', fetchMock);

    await new Promise<void>((resolve, reject) => {
      const close = subscribeCompileEvents('compile-456', {
        lastEventId: '41',
        onEvent: (evt) => {
          if (evt.type === 'done') {
            close();
            resolve();
          }
        },
        onError: (err) => reject(err),
      });
    });

    expect(fetchMock).toHaveBeenCalled();
    const [, requestInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = requestInit.headers as Record<string, string>;
    expect(headers['Last-Event-ID']).toBe('41');
  });

  it('never reconnects after a clean done frame', async () => {
    const frames = ['id: 1\r\nevent: done\r\ndata: {}\r\n\r\n'];
    const fetchMock = vi.fn().mockResolvedValue(mockEventStreamResponse(frames));
    vi.stubGlobal('fetch', fetchMock);

    await new Promise<void>((resolve, reject) => {
      const close = subscribeCompileEvents('compile-789', {
        onEvent: (evt) => {
          if (evt.type === 'done') {
            close();
          }
        },
        onError: (err) => reject(err),
      });
      setTimeout(resolve, 300);
    });

    // Only the one connection attempt -- no reconnect after a clean done.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// Attachments and the clarify pass (PLAN-ATTACHMENTS-CLARIFY.md)
// ---------------------------------------------------------------------------

function jsonOk(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

describe('uploadAttachment', () => {
  it('posts multipart form data without a JSON content type', async () => {
    const fetchMock = vi.fn(async () => jsonOk({ attachment: { id: 'a'.repeat(32) }, warnings: [] }));
    vi.stubGlobal('fetch', fetchMock);
    const file = new File(['Order ID\n1\n'], 'orders.csv', { type: 'text/csv' });

    await api.uploadAttachment(file);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/graphs/attachments');
    expect(init.method).toBe('POST');
    expect(init.body).toBeInstanceOf(FormData);
    // The browser must set the multipart boundary itself; a JSON content type
    // here makes the server unable to parse the parts.
    const headers = (init.headers ?? {}) as Record<string, string>;
    expect(headers['Content-Type']).toBeUndefined();
    expect((init.body as FormData).get('file')).toBe(file);
  });

  it('passes the abort signal through so a slow upload can be cancelled', async () => {
    const fetchMock = vi.fn(async () => jsonOk({ attachment: {}, warnings: [] }));
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();

    await api.uploadAttachment(new File(['x'], 'a.csv'), { signal: controller.signal });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.signal).toBe(controller.signal);
  });

  it('surfaces the structured {code, message, problems} detail on a refusal', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            detail: {
              code: 'image_input_unsupported',
              message: 'no vision',
              problems: ['no vision'],
            },
          }),
          { status: 422, headers: { 'content-type': 'application/json' } },
        ),
      ),
    );

    await expect(api.uploadAttachment(new File(['x'], 'a.png'))).rejects.toMatchObject({
      status: 422,
      detail: { code: 'image_input_unsupported' },
    });
  });
});

describe('deleteAttachment', () => {
  it('deletes by encoded id', async () => {
    const fetchMock = vi.fn(async () => jsonOk({ attachmentId: 'x', deleted: true }));
    vi.stubGlobal('fetch', fetchMock);

    await api.deleteAttachment('abc/def');

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/graphs/attachments/abc%2Fdef');
    expect(init.method).toBe('DELETE');
  });
});

describe('clarifyGraph', () => {
  it('posts the description with attachment ids and no model override', async () => {
    const fetchMock = vi.fn(async () =>
      jsonOk({ needsClarification: false, questions: [], assumptions: [] }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await api.clarifyGraph({ description: 'Handle it.', attachmentIds: ['a'.repeat(32)] });

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/graphs/generate/clarify');
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(body.description).toBe('Handle it.');
    expect(body.attachmentIds).toEqual(['a'.repeat(32)]);
    // The analysis pass uses the same resolved route as the draft, so there is
    // deliberately no per-request override to send.
    expect(body).not.toHaveProperty('modelOverride');
  });
});
