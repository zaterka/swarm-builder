import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import GeneratePanel from './GeneratePanel';

// The two-phase describe flow (PLAN-ATTACHMENTS-CLARIFY.md): supplementary files
// are uploaded one per request and referenced by id, an analysis pass decides
// whether questions are needed, and the draft is requested with the answers.
//
// These tests pin the behaviours that are easy to lose in a refactor and
// expensive to get wrong in front of a user: a file that fails to upload must not
// block the rest, a failed generation must not wipe what was typed, the same
// description must not trigger a second analysis call, and closing the panel
// mid-upload must not leave a file on disk.

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** A `/generate/stream` reply in sse-starlette's framing. */
function sseResponse(frames: [event: string, data: unknown][]): Response {
  const text = frames
    .map(([event, data]) => `event: ${event}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`)
    .join('');
  return new Response(text, { status: 200, headers: { 'content-type': 'text/event-stream' } });
}

const HEALTH = {
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
  runReady: true,
  runBlockers: [],
  dryRun: false,
  dryRunForcedByEnv: false,
  modelConfigured: true,
  appConfigPath: '/tmp/swarm-workspace/settings.json',
  appConfigError: null,
};

const ATTACHMENT = {
  id: 'a'.repeat(32),
  filename: 'orders.csv',
  kind: 'csv',
  mediaType: 'text/csv',
  bytes: 2048,
  chars: 42,
  truncated: false,
  notes: ['only the first 200 rows were read'],
  preview: '| Order ID |\n| --- |\n| 42 |',
  expiresAt: '2026-01-01T12:00:00Z',
};

const NEEDS_CLARIFICATION = {
  needsClarification: true,
  questions: [
    {
      id: 'q1',
      question: 'Who approves a refund?',
      why: 'The graph routes on this answer.',
      options: [
        { label: 'A refund-policy agent', description: null },
        { label: 'A human reviewer', description: null },
      ],
      recommended: 'A refund-policy agent',
    },
  ],
  assumptions: ['The ticket arrives as text.'],
  understanding: 'Triage a support ticket.',
  model: { provider: 'deepseek-official', model: 'deepseek-flash', source: 'settings-default' },
  dryRun: false,
};

const CLEAR = {
  ...NEEDS_CLARIFICATION,
  needsClarification: false,
  questions: [],
};

const GRAPH = {
  id: 'g1',
  name: 'Triage',
  entryNodeId: 'intake',
  exitNodeId: 'reply',
  stateFields: [],
  nodes: [],
  edges: [],
  updatedAt: '2026-01-01T00:00:00Z',
};

const GENERATED = { graph: GRAPH, warnings: [], attempts: 1, dryRun: false, attachments: [] };

interface Options {
  clarify?: unknown | (() => Response);
  generate?: unknown | (() => Response);
  upload?: unknown | (() => Response);
}

function stubFetch(options: Options = {}) {
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init: init ?? {} });
      const pick = (value: unknown | (() => Response) | undefined, fallback: unknown) => {
        if (typeof value === 'function') return (value as () => Response)();
        return jsonResponse(value ?? fallback);
      };
      if (url.endsWith('/api/health')) return jsonResponse(HEALTH);
      if (url.endsWith('/api/graphs/generate/clarify')) return pick(options.clarify, CLEAR);
      if (url.endsWith('/api/graphs/generate/stream')) {
        if (typeof options.generate === 'function') return (options.generate as () => Response)();
        return sseResponse([['done', options.generate ?? GENERATED]]);
      }
      if (url.endsWith('/api/graphs/attachments')) return pick(options.upload, { attachment: ATTACHMENT, warnings: [] });
      if (url.includes('/api/graphs/attachments/')) return jsonResponse({ attachmentId: 'x', deleted: true });
      return jsonResponse({});
    }),
  );
  return calls;
}

function uploadFile(name = 'orders.csv', size = 2048): File {
  const file = new File(['x'.repeat(16)], name, { type: 'text/csv' });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

beforeEach(() => {
  stubFetch();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('GeneratePanel attachments', () => {
  it('uploads a chosen file and shows what was extracted', async () => {
    const calls = stubFetch();
    render(<GeneratePanel onGenerated={() => undefined} />);

    fireEvent.change(screen.getByLabelText('Attach supplementary files'), {
      target: { files: [uploadFile()] },
    });

    await waitFor(() => expect(screen.getByText('42 characters')).toBeInTheDocument());
    const uploads = calls.filter((call) => call.init.method === 'POST' && call.url.endsWith('/attachments'));
    expect(uploads).toHaveLength(1);
    // The user can check what context will reach their provider.
    expect(screen.getByText('What we read')).toBeInTheDocument();
    // Visible before anything is attached, too -- the next test asserts that.
    expect(
      screen.getByText('Files you attach are sent to deepseek-official / deepseek-flash as context.'),
    ).toBeInTheDocument();
  });

  it('discloses the provider before any file is attached', async () => {
    render(<GeneratePanel onGenerated={() => undefined} />);

    expect(
      await screen.findByText(
        'Files you attach are sent to deepseek-official / deepseek-flash as context.',
      ),
    ).toBeInTheDocument();
  });

  it('tells the user to remove an image the model cannot read', async () => {
    stubFetch({
      clarify: () =>
        jsonResponse(
          {
            detail: {
              code: 'image_input_unsupported',
              message: 'deepseek-official / deepseek-v4-flash does not accept image input',
              problems: [],
            },
          },
          422,
        ),
    });
    render(<GeneratePanel onGenerated={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Workflow description'), { target: { value: 'Read it.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));

    expect(
      await screen.findByText(/Remove the image below .* then try again/),
    ).toBeInTheDocument();
  });

  it('reports a file the server refused without losing the others', async () => {
    stubFetch({
      upload: () =>
        jsonResponse(
          {
            detail: {
              code: 'image_input_unsupported',
              message: 'deepseek-official / deepseek-v4-flash does not accept image input',
              problems: [],
            },
          },
          422,
        ),
    });
    render(<GeneratePanel onGenerated={() => undefined} />);

    fireEvent.change(screen.getByLabelText('Attach supplementary files'), {
      target: { files: [uploadFile('screen.png')] },
    });

    await waitFor(() =>
      expect(
        screen.getByText(/does not accept image input/),
      ).toBeInTheDocument(),
    );
  });

  it('blocks a sixth file locally and says why', async () => {
    render(<GeneratePanel onGenerated={() => undefined} />);
    const input = screen.getByLabelText('Attach supplementary files');
    for (let index = 0; index < 5; index += 1) {
      fireEvent.change(input, { target: { files: [uploadFile(`f${index}.csv`)] } });
      // eslint-disable-next-line no-await-in-loop
      await waitFor(() => expect(screen.getAllByText('42 characters')).toHaveLength(index + 1));
    }

    fireEvent.change(input, { target: { files: [uploadFile('sixth.csv')] } });

    expect(await screen.findByText('at most 5 files can be attached')).toBeInTheDocument();
  });

  it('deletes the attachment when its chip is removed', async () => {
    const calls = stubFetch();
    render(<GeneratePanel onGenerated={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Attach supplementary files'), {
      target: { files: [uploadFile()] },
    });
    await waitFor(() => expect(screen.getByText('42 characters')).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText('Remove orders.csv'));

    await waitFor(() =>
      expect(calls.some((call) => call.init.method === 'DELETE')).toBe(true),
    );
    expect(screen.queryByText('42 characters')).not.toBeInTheDocument();
  });

  it('discards an upload it received when the panel unmounts', async () => {
    const calls = stubFetch();
    const { unmount } = render(<GeneratePanel onGenerated={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Attach supplementary files'), {
      target: { files: [uploadFile()] },
    });
    await waitFor(() => expect(screen.getByText('42 characters')).toBeInTheDocument());

    unmount();

    await waitFor(() => expect(calls.some((call) => call.init.method === 'DELETE')).toBe(true));
  });
});

describe('GeneratePanel clarifying questions', () => {
  it('asks the questions the analysis pass returned, with the recommendation preselected', async () => {
    stubFetch({ clarify: NEEDS_CLARIFICATION });
    render(<GeneratePanel onGenerated={() => undefined} />);

    fireEvent.change(screen.getByLabelText('Workflow description'), {
      target: { value: 'Handle refunds.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));

    expect(await screen.findByText('Who approves a refund?')).toBeInTheDocument();
    expect(screen.getByText('Triage a support ticket.')).toBeInTheDocument();
    const recommended = screen.getByRole('radio', { name: /A refund-policy agent/ }) as HTMLInputElement;
    expect(recommended.checked).toBe(true);
  });

  it('submits the chosen answers and files to the draft request', async () => {
    const calls = stubFetch({ clarify: NEEDS_CLARIFICATION });
    const onGenerated = vi.fn();
    render(<GeneratePanel onGenerated={onGenerated} />);

    fireEvent.change(screen.getByLabelText('Attach supplementary files'), {
      target: { files: [uploadFile()] },
    });
    await waitFor(() => expect(screen.getByText('42 characters')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Workflow description'), {
      target: { value: 'Handle refunds.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));
    await screen.findByText('Who approves a refund?');

    fireEvent.click(screen.getByRole('radio', { name: 'A human reviewer' }));
    fireEvent.click(screen.getByRole('button', { name: 'Generate with answers' }));

    await waitFor(() => expect(onGenerated).toHaveBeenCalled());
    const generate = calls.find((call) => call.url.endsWith('/api/graphs/generate/stream'));
    const body = JSON.parse(String(generate?.init.body)) as Record<string, unknown>;
    expect(body.answers).toEqual([
      { questionId: 'q1', question: 'Who approves a refund?', answer: 'A human reviewer' },
    ]);
    expect(body.attachmentIds).toEqual([ATTACHMENT.id]);
  });

  it('accepts a typed answer through "Other…"', async () => {
    const calls = stubFetch({ clarify: NEEDS_CLARIFICATION });
    render(<GeneratePanel onGenerated={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Workflow description'), { target: { value: 'Handle refunds.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));
    await screen.findByText('Who approves a refund?');

    fireEvent.click(screen.getByRole('radio', { name: 'Other…' }));
    fireEvent.change(screen.getByLabelText('Answer for: Who approves a refund?'), {
      target: { value: 'The finance lead' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Generate with answers' }));

    await waitFor(() => expect(calls.some((call) => call.url.endsWith('/api/graphs/generate/stream'))).toBe(true));
    const generate = calls.find((call) => call.url.endsWith('/api/graphs/generate/stream'));
    const body = JSON.parse(String(generate?.init.body)) as { answers: { answer: string }[] };
    expect(body.answers[0]?.answer).toBe('The finance lead');
  });

  it('keeps the questions and answers on screen while the draft runs', async () => {
    // A real regression: while generating, the panel must not swap back to the
    // composer, or the user watches their answers disappear mid-request.
    // An object property, not a `let`: TypeScript narrows a local assigned only
    // inside a callback to `null` at the use site and then refuses the call.
    const gate: { release?: () => void } = {};
    const generate = () =>
      new Promise<Response>((resolve) => {
        gate.release = () => resolve(sseResponse([['done', GENERATED]]));
      });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/api/health')) return jsonResponse(HEALTH);
        if (url.endsWith('/api/graphs/generate/clarify')) return jsonResponse(NEEDS_CLARIFICATION);
        if (url.endsWith('/api/graphs/generate/stream')) return generate();
        return jsonResponse({});
      }),
    );
    render(<GeneratePanel onGenerated={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Workflow description'), { target: { value: 'Handle refunds.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));
    await screen.findByText('Who approves a refund?');

    fireEvent.click(screen.getByRole('button', { name: 'Generate with answers' }));

    expect(await screen.findByRole('button', { name: 'Generating…' })).toBeInTheDocument();
    expect(screen.getByText('Who approves a refund?')).toBeInTheDocument();
    expect(screen.queryByLabelText('Workflow description')).not.toBeInTheDocument();
    gate.release?.();
  });

  it('shows which step the draft is on, including a reviewer rejection', async () => {
    const encoder = new TextEncoder();
    const stream: { push?: (event: string, data: unknown) => void; end?: () => void } = {};
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        stream.push = (event, data) =>
          controller.enqueue(encoder.encode(`event: ${event}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`));
        stream.end = () => controller.close();
      },
    });
    stubFetch({
      generate: () => new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    });
    const onGenerated = vi.fn();
    render(<GeneratePanel onGenerated={onGenerated} />);
    fireEvent.change(screen.getByLabelText('Workflow description'), { target: { value: 'Handle refunds.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));

    expect(await screen.findByText(/The model is drafting nodes and edges/)).toBeInTheDocument();
    stream.push?.('progress', { stage: 'drafting', attempt: 1, maxAttempts: 3, problems: [] });
    stream.push?.('progress', { stage: 'reviewing', attempt: 1, maxAttempts: 3, problems: [] });
    expect(await screen.findByText(/Checking draft 1 against the reviewer's rules/)).toBeInTheDocument();
    stream.push?.('progress', { stage: 'rejected', attempt: 1, maxAttempts: 3, problems: ['a', 'b'] });
    stream.push?.('progress', { stage: 'drafting', attempt: 2, maxAttempts: 3, problems: [] });
    expect(
      await screen.findByText(/found 2 problems in draft 1; the model is redrafting \(attempt 2 of 3\)/),
    ).toBeInTheDocument();
    expect(screen.getByText('Draft 2 of 3')).toBeInTheDocument();

    stream.push?.('done', GENERATED);
    stream.end?.();
    await waitFor(() => expect(onGenerated).toHaveBeenCalled());
    expect(screen.queryByText(/redrafting/)).not.toBeInTheDocument();
  });

  it('reports a failure that arrives inside the stream', async () => {
    stubFetch({
      generate: () =>
        sseResponse([
          ['progress', { stage: 'drafting', attempt: 1, maxAttempts: 3, problems: [] }],
          [
            'error',
            {
              status: 422,
              detail: { code: 'generation_failed', message: 'could not produce a review-clean graph', problems: ['dangling edge'] },
            },
          ],
        ]),
    });
    render(<GeneratePanel onGenerated={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Workflow description'), { target: { value: 'Handle refunds.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));

    expect(await screen.findByText('could not produce a review-clean graph')).toBeInTheDocument();
    expect(screen.getByText('dangling edge')).toBeInTheDocument();
  });

  it('generates anyway with no answers when the user skips the questions', async () => {
    const calls = stubFetch({ clarify: NEEDS_CLARIFICATION });
    render(<GeneratePanel onGenerated={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Workflow description'), { target: { value: 'Handle refunds.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));
    await screen.findByText('Who approves a refund?');

    fireEvent.click(screen.getByRole('button', { name: 'Generate anyway' }));

    await waitFor(() => expect(calls.some((call) => call.url.endsWith('/api/graphs/generate/stream'))).toBe(true));
    const generate = calls.find((call) => call.url.endsWith('/api/graphs/generate/stream'));
    expect(JSON.parse(String(generate?.init.body)).answers).toEqual([]);
  });

  it('goes straight to drafting when the description is clear, and reports the files used', async () => {
    const calls = stubFetch({ clarify: CLEAR, generate: { graph: GRAPH, warnings: [], attempts: 1, dryRun: false, attachments: [{ id: ATTACHMENT.id, filename: 'orders.csv', kind: 'csv', chars: 42, truncated: false, notes: ['only the first 200 rows were read'] }] } });
    const onGenerated = vi.fn();
    render(<GeneratePanel onGenerated={onGenerated} />);

    fireEvent.change(screen.getByLabelText('Workflow description'), {
      target: { value: 'Take a ticket, classify it, reply.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));

    await waitFor(() => expect(onGenerated).toHaveBeenCalled());
    expect(screen.queryByText('Who approves a refund?')).not.toBeInTheDocument();
    expect(calls.some((call) => call.url.endsWith('/api/graphs/generate/clarify'))).toBe(true);
    expect(await screen.findByText('Drafted from your description and 1 file.')).toBeInTheDocument();
    expect(screen.getByText('orders.csv: only the first 200 rows were read')).toBeInTheDocument();
  });

  it('analyzes once per description: going back and clicking again reuses the answer', async () => {
    const calls = stubFetch({ clarify: NEEDS_CLARIFICATION });
    render(<GeneratePanel onGenerated={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Workflow description'), { target: { value: 'Handle refunds.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));
    await screen.findByText('Who approves a refund?');
    fireEvent.click(screen.getByRole('button', { name: 'Back to description' }));

    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));

    await screen.findByText('Who approves a refund?');
    expect(calls.filter((call) => call.url.endsWith('/api/graphs/generate/clarify'))).toHaveLength(1);
    expect(screen.getByText('Questions already asked for this description.')).toBeInTheDocument();
  });

  it('falls back to drafting when the analysis call fails upstream', async () => {
    const calls = stubFetch({
      clarify: () => new Response(JSON.stringify({ detail: 'model API error: 400' }), { status: 502 }),
    });
    const onGenerated = vi.fn();
    render(<GeneratePanel onGenerated={onGenerated} />);
    fireEvent.change(screen.getByLabelText('Workflow description'), { target: { value: 'Handle refunds.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));

    await waitFor(() => expect(onGenerated).toHaveBeenCalled());
    expect(calls.some((call) => call.url.endsWith('/api/graphs/generate/stream'))).toBe(true);
    expect(screen.getByText(/Could not ask clarifying questions: model API error/)).toBeInTheDocument();
  });

  it('stops on a 422 and shows the problems', async () => {
    stubFetch({
      clarify: () =>
        jsonResponse({ detail: { code: 'attachment_expired', message: 'that attachment expired', problems: [] } }, 422),
    });
    render(<GeneratePanel onGenerated={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Workflow description'), { target: { value: 'Handle refunds.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));

    expect(await screen.findByText('that attachment expired')).toBeInTheDocument();
    expect(screen.queryByText('Who approves a refund?')).not.toBeInTheDocument();
  });

  it('keeps the description, files and answers after a failed draft, and can retry', async () => {
    const calls = stubFetch({
      clarify: NEEDS_CLARIFICATION,
      generate: () =>
        jsonResponse(
          { detail: { code: 'generation_failed', message: 'could not produce a review-clean graph', problems: ['port_type_mismatch'] } },
          422,
        ),
    });
    render(<GeneratePanel onGenerated={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Attach supplementary files'), {
      target: { files: [uploadFile()] },
    });
    await waitFor(() => expect(screen.getByText('42 characters')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Workflow description'), { target: { value: 'Handle refunds.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));
    await screen.findByText('Who approves a refund?');
    fireEvent.click(screen.getByRole('button', { name: 'Generate with answers' }));

    expect(await screen.findByText('could not produce a review-clean graph')).toBeInTheDocument();
    expect(screen.getByText('port_type_mismatch')).toBeInTheDocument();
    // The questions stay on screen (with the chosen answer) rather than being
    // swapped out for the composer mid-flow.
    expect(screen.getByText('Who approves a refund?')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: 'A human reviewer' }));
    expect((screen.getByRole('radio', { name: 'A human reviewer' }) as HTMLInputElement).checked).toBe(true);

    // Going back shows that nothing the user typed or attached was lost.
    fireEvent.click(screen.getByRole('button', { name: 'Back to description' }));
    expect(screen.getByLabelText('Workflow description')).toHaveValue('Handle refunds.');
    expect(screen.getByText('42 characters')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));
    expect(calls.filter((call) => call.url.endsWith('/api/graphs/generate/clarify'))).toHaveLength(1);
  });

  it('clears the failure when the user tries again', async () => {
    stubFetch({
      clarify: NEEDS_CLARIFICATION,
      generate: () =>
        jsonResponse(
          { detail: { code: 'generation_failed', message: 'could not produce a review-clean graph', problems: ['port_type_mismatch'] } },
          422,
        ),
    });
    render(<GeneratePanel onGenerated={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Workflow description'), { target: { value: 'Handle refunds.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate graph' }));
    await screen.findByText('Who approves a refund?');
    fireEvent.click(screen.getByRole('button', { name: 'Generate with answers' }));
    await screen.findByText('port_type_mismatch');

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(screen.queryByText('port_type_mismatch')).not.toBeInTheDocument();
  });
});
