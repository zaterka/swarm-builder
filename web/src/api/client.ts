// Typed fetch client for every documented HTTP API endpoint
// (PLAN.md "HTTP API"), plus a hand-rolled SSE subscription for compile
// events. Every success/error shape is imported from `api/schema.ts` (itself
// the single indirection point into the generated `web/src/types.ts`), and
// the SSE frame vocabulary from `api/compileWire.ts` -- never redeclared a
// second time here.
import type {
  CompileSnapshot,
  DeleteGraphResponse,
  ExportResponse,
  GenerateGraphRequest,
  GenerateGraphResponse,
  GraphListResponse,
  HealthResponse,
  ModelsResponse,
  ReviewResponse,
  RunListResponse,
  RunRecord,
  SettingsResponse,
  SettingsUpdateRequest,
  StartCompileRequest,
  StartCompileResponse,
  StartRunRequest,
  StartRunResponse,
  SwarmGraph,
  TemplateEntryOut,
  TestConnectionRequest,
  TestConnectionResponse,
  ValidationError,
} from './schema';
import type { CompileSseEvent } from './compileWire';

const API_BASE = '/api';

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** Every route in routes/*.py that fails returns FastAPI's own
 * `{"detail": ...}` convention (HTTPException), confirmed by reading
 * every route file -- this is the one error shape every non-2xx
 * response in this client parses into. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string | ValidationError[] | unknown;

  constructor(status: number, detail: string | ValidationError[] | unknown) {
    super(typeof detail === 'string' ? detail : JSON.stringify(detail));
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/** GET /api/graphs/:id/export 404 -- no compiled project yet. */
export class NotCompiledError extends ApiError {}

/** POST /api/compile 409 -- a compile is already running for this graph
 * (PLAN.md I4 / edge case "Concurrent compiles of one graph -> 409"). */
export class CompileConflictError extends ApiError {}

/** POST /api/graphs/:id/review 503 -- swarm_builder.compile.review could
 * not be imported (a transient/defensive state per graphs.py's own
 * docstring). */
export class ReviewUnavailableError extends ApiError {}

/** GET /api/templates 503 -- swarm_builder.templates.registry could not
 * be imported. */
export class TemplatesUnavailableError extends ApiError {}

/** PUT /api/settings 422 -- the submitted provider/model/key combination is
 * not usable. `detail` carries every problem found, so the form can show them
 * all at once rather than one per attempt. */
export class SettingsValidationError extends ApiError {
  get problems(): string[] {
    return Array.isArray(this.detail) ? (this.detail as string[]) : [];
  }
}

// ---------------------------------------------------------------------------
// Low-level request helper
// ---------------------------------------------------------------------------

async function parseErrorDetail(response: Response): Promise<unknown> {
  try {
    const body = await response.json();
    if (body && typeof body === 'object' && 'detail' in body) {
      return (body as { detail: unknown }).detail;
    }
    return body;
  } catch {
    return response.statusText;
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    const detail = await parseErrorDetail(response);
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

// ---------------------------------------------------------------------------
// Graph CRUD, health, templates, models, review, export
// ---------------------------------------------------------------------------

function health(): Promise<HealthResponse> {
  return request<HealthResponse>('/health');
}

function listGraphs(): Promise<GraphListResponse> {
  return request<GraphListResponse>('/graphs');
}

function getGraph(id: string): Promise<SwarmGraph> {
  return request<SwarmGraph>(`/graphs/${encodeURIComponent(id)}`);
}

function putGraph(id: string, graph: SwarmGraph): Promise<SwarmGraph> {
  return request<SwarmGraph>(`/graphs/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: JSON.stringify(graph),
  });
}

async function deleteGraph(
  id: string,
  opts?: { project?: boolean },
): Promise<DeleteGraphResponse> {
  const query = opts?.project ? '?project=true' : '';
  return request<DeleteGraphResponse>(`/graphs/${encodeURIComponent(id)}${query}`, {
    method: 'DELETE',
  });
}

async function listTemplates(): Promise<TemplateEntryOut[]> {
  try {
    return await request<TemplateEntryOut[]>('/templates');
  } catch (err) {
    if (err instanceof ApiError && err.status === 503) {
      throw new TemplatesUnavailableError(err.status, err.detail);
    }
    throw err;
  }
}

function getModels(): Promise<ModelsResponse> {
  return request<ModelsResponse>('/models');
}

/** The in-app model settings (`GET /api/settings`). Never carries the key. */
function getSettings(): Promise<SettingsResponse> {
  return request<SettingsResponse>('/settings');
}

/** Save a provider/model/key and/or the dry-run switch. An omitted `model`
 * leaves the stored one alone; `clearModel: true` removes it. */
async function putSettings(body: SettingsUpdateRequest): Promise<SettingsResponse> {
  try {
    return await request<SettingsResponse>('/settings', {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  } catch (err) {
    if (err instanceof ApiError && err.status === 422) {
      throw new SettingsValidationError(err.status, err.detail);
    }
    throw err;
  }
}

/** Make one minimal real model call to check a key. Everything omitted falls
 * back to the saved configuration. */
function testConnection(body: TestConnectionRequest): Promise<TestConnectionResponse> {
  return request<TestConnectionResponse>('/settings/test', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

async function reviewGraph(id: string): Promise<ReviewResponse> {
  try {
    return await request<ReviewResponse>(`/graphs/${encodeURIComponent(id)}/review`, {
      method: 'POST',
    });
  } catch (err) {
    if (err instanceof ApiError && err.status === 503) {
      throw new ReviewUnavailableError(err.status, err.detail);
    }
    throw err;
  }
}

async function exportGraph(
  id: string,
  target: 'pydantic-graph' | 'langgraph' = 'pydantic-graph',
): Promise<ExportResponse> {
  try {
    return await request<ExportResponse>(
      `/graphs/${encodeURIComponent(id)}/export?target=${encodeURIComponent(target)}`,
    );
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      throw new NotCompiledError(err.status, err.detail);
    }
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Compile
//
// Every shape here is the server's real one: `StartCompileResponse`,
// `CompileSnapshot` (the response of both `GET` and `DELETE
// /api/compile/{compileId}`) and the SSE frames in `api/compileWire.ts`.
// ---------------------------------------------------------------------------

async function compileRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  });

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // no JSON body -- leave body null
  }

  if (!response.ok) {
    const detail =
      body && typeof body === 'object' && 'detail' in body
        ? (body as { detail: unknown }).detail
        : response.statusText;
    if (response.status === 409) {
      throw new CompileConflictError(response.status, detail);
    }
    throw new ApiError(response.status, detail);
  }

  return body as T;
}

function startCompile(
  graphId: string,
  target: StartCompileRequest['target'] = 'pydantic-graph',
): Promise<StartCompileResponse> {
  const payload: StartCompileRequest = { graphId, target };
  return compileRequest<StartCompileResponse>('/compile', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

function getCompileSnapshot(compileId: string): Promise<CompileSnapshot> {
  return compileRequest<CompileSnapshot>(`/compile/${encodeURIComponent(compileId)}`);
}

/** DELETE /api/compile/:compileId answers with the same snapshot `GET` does,
 * carrying `status: "cancelled"` (docs/api.md) -- so a caller gets the job's
 * final state without a second request. */
function cancelCompile(compileId: string): Promise<CompileSnapshot> {
  return compileRequest<CompileSnapshot>(`/compile/${encodeURIComponent(compileId)}`, {
    method: 'DELETE',
  });
}

// ---------------------------------------------------------------------------
// Describe -> generate (`routes/generate.py`). One structured-output model
// call plus up to two repair rounds; a 422 carries `{message, problems}`.
// ---------------------------------------------------------------------------

function generateGraph(body: GenerateGraphRequest): Promise<GenerateGraphResponse> {
  return request<GenerateGraphResponse>('/graphs/generate', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// Runs (`routes/runs.py`). Starting and history are run-specific; the live
// job (snapshot, cancel, events) is served by the generic `/api/jobs/:id`
// endpoints, which are the compile endpoints under a kind-neutral path.
// ---------------------------------------------------------------------------

function startRun(graphId: string, input: unknown, compileIfStale = true): Promise<StartRunResponse> {
  const payload: StartRunRequest = { input, compileIfStale };
  return compileRequest<StartRunResponse>(`/graphs/${encodeURIComponent(graphId)}/runs`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

function listRuns(graphId: string): Promise<RunListResponse> {
  return request<RunListResponse>(`/graphs/${encodeURIComponent(graphId)}/runs`);
}

function getRun(graphId: string, runId: string): Promise<RunRecord> {
  return request<RunRecord>(
    `/graphs/${encodeURIComponent(graphId)}/runs/${encodeURIComponent(runId)}`,
  );
}

function getJobSnapshot(jobId: string): Promise<CompileSnapshot> {
  return compileRequest<CompileSnapshot>(`/jobs/${encodeURIComponent(jobId)}`);
}

function cancelJob(jobId: string): Promise<CompileSnapshot> {
  return compileRequest<CompileSnapshot>(`/jobs/${encodeURIComponent(jobId)}`, {
    method: 'DELETE',
  });
}

// ---------------------------------------------------------------------------
// SSE: hand-rolled fetch + ReadableStream parsing
//
// Why not EventSource: EventSource cannot set a `Last-Event-ID` request
// header on its INITIAL connection (browsers only resend it
// automatically on their own internal reconnect), and the documented
// contract requires resuming from a persisted id even on a fresh page
// load (PLAN.md: "Reconnect is Last-Event-ID-based ... GET
// /api/compile/:compileId is for a client that has no Last-Event-ID at
// all, e.g. a fresh tab"). A hand-rolled client is the only way to
// satisfy that on the very first connection attempt.
//
// Wire format, matching sse-starlette 3.4.11's real encoder (verified
// by reading its `event.py`), NOT the naive "\n\n"-separated textbook
// SSE format a first draft of this client assumed:
//   - DEFAULT_SEPARATOR is "\r\n"; ServerSentEvent.encode() terminates
//     every frame with an EXTRA separator, so real frames end in
//     "\r\n\r\n".
//   - a comment/ping frame (`: ping - <ts>`, emitted every 15s) has no
//     `data` field at all and must be silently discarded, never
//     dispatched.
//   - a multi-line payload becomes consecutive `data:` lines that must
//     be rejoined with "\n".
//   - exactly one leading space after the field-name colon is stripped.
// ---------------------------------------------------------------------------

const FRAME_SEPARATOR_RE = /\r\n\r\n|\n\n|\r\r/;
const LINE_SEPARATOR_RE = /\r\n|\n|\r/;

interface ParsedFrame {
  id?: string;
  event?: string;
  data?: string;
}

function parseFrame(rawFrame: string): ParsedFrame | null {
  const lines = rawFrame.split(LINE_SEPARATOR_RE);
  let id: string | undefined;
  let event: string | undefined;
  const dataLines: string[] = [];

  for (const line of lines) {
    if (line === '' || line.startsWith(':')) {
      // Blank line (frame padding) or a comment/ping line -- ignored.
      continue;
    }
    const colonIndex = line.indexOf(':');
    const field = colonIndex === -1 ? line : line.slice(0, colonIndex);
    let value = colonIndex === -1 ? '' : line.slice(colonIndex + 1);
    if (value.startsWith(' ')) {
      value = value.slice(1);
    }
    if (field === 'id') {
      id = value;
    } else if (field === 'event') {
      event = value;
    } else if (field === 'data') {
      dataLines.push(value);
    }
    // Any other field name (e.g. `retry`) is ignored -- not needed by
    // this client, which owns its own reconnect/backoff policy.
  }

  if (dataLines.length === 0) {
    // A frame with no data field is never dispatched -- this is what
    // makes ping/comment frames a no-op (GROUP6_PLAN.md review finding
    // B2) rather than a bogus JSON.parse('') crash.
    return null;
  }

  return { id, event, data: dataLines.join('\n') };
}

export interface SubscribeCompileEventsOptions {
  lastEventId?: string;
  onEvent: (event: CompileSseEvent) => void;
  onError: (err: Error) => void;
  /** Called every time a frame is received, with the id of that frame,
   * so a caller can persist "last seen id" for reload-resume
   * (GROUP6_PLAN.md decision 5 / review finding B6). Optional. */
  onLastEventId?: (id: string) => void;
  /** Maximum reconnect attempts after a transport-level error before
   * giving up and calling onError. Defaults to 5. */
  maxReconnectAttempts?: number;
}

/**
 * Subscribe to GET /api/compile/:compileId/events. Returns a function
 * that permanently closes the subscription (idempotent, safe to call
 * more than once) -- GROUP6_PLAN.md review finding B7.
 *
 * Reconnection is owned entirely by this function (there is no
 * EventSource underneath to do it for us -- review finding B5): a
 * transport-level error (not an application-level `done`/`error`
 * frame, and not an explicit `close()` call) triggers a capped
 * exponential backoff retry, resuming with `Last-Event-ID` set to the
 * last frame id actually received so far (never a numeric/lexicographic
 * "max" of ids -- review finding B4, since SSE ids are opaque strings).
 */
export function subscribeCompileEvents(
  compileId: string,
  opts: SubscribeCompileEventsOptions,
): () => void {
  return subscribeJobEventsAt('/compile', compileId, opts);
}

/** The same subscription against `/api/jobs/:id/events`, for run jobs. */
export function subscribeJobEvents(jobId: string, opts: SubscribeCompileEventsOptions): () => void {
  return subscribeJobEventsAt('/jobs', jobId, opts);
}

function subscribeJobEventsAt(
  basePath: '/compile' | '/jobs',
  compileId: string,
  opts: SubscribeCompileEventsOptions,
): () => void {
  let closed = false;
  let abortController: AbortController | null = null;
  let lastEventId = opts.lastEventId;
  let reconnectAttempt = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  const maxAttempts = opts.maxReconnectAttempts ?? 5;

  const backoffMs = (attempt: number) => Math.min(500 * 2 ** attempt, 8000);

  async function connectOnce(): Promise<'ended-clean' | 'ended-error'> {
    abortController = new AbortController();
    const headers: Record<string, string> = { Accept: 'text/event-stream' };
    if (lastEventId !== undefined) {
      headers['Last-Event-ID'] = lastEventId;
    }

    let response: Response;
    try {
      response = await fetch(
        `${API_BASE}${basePath}/${encodeURIComponent(compileId)}/events`,
        { headers, signal: abortController.signal },
      );
    } catch (err) {
      if (closed) return 'ended-clean';
      throw err;
    }

    const contentType = response.headers.get('content-type') ?? '';
    if (!response.ok || !contentType.startsWith('text/event-stream')) {
      // A 404 for an unknown/evicted compileId, a 400 for a malformed or
      // too-old Last-Event-ID (the `gap: true` body), or a 503 -- never fed
      // to the frame parser (review finding B7).
      let detail: unknown;
      try {
        detail = await response.json();
      } catch {
        detail = response.statusText;
      }
      throw new ApiError(
        response.status,
        detail && typeof detail === 'object' && 'detail' in detail
          ? (detail as { detail: unknown }).detail
          : detail,
      );
    }

    if (!response.body) {
      throw new Error('compile events response had no readable body');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let endedClean = false;

    try {
      while (!closed) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let match = buffer.match(FRAME_SEPARATOR_RE);
        while (match) {
          const rawFrame = buffer.slice(0, match.index);
          buffer = buffer.slice((match.index ?? 0) + match[0].length);
          const parsed = parseFrame(rawFrame);
          if (parsed && parsed.data !== undefined) {
            if (parsed.id !== undefined) {
              lastEventId = parsed.id;
              opts.onLastEventId?.(parsed.id);
            }
            const eventType = parsed.event ?? 'message';
            let data: unknown = parsed.data;
            try {
              data = JSON.parse(parsed.data);
            } catch {
              // Not JSON -- pass the raw string through.
            }
            opts.onEvent({ id: parsed.id ?? '', type: eventType, data });
            if (eventType === 'done' || eventType === 'error') {
              endedClean = true;
            }
          }
          match = buffer.match(FRAME_SEPARATOR_RE);
        }
      }
    } finally {
      reader.cancel().catch(() => {});
    }

    return endedClean || closed ? 'ended-clean' : 'ended-error';
  }

  async function run() {
    while (!closed) {
      try {
        const outcome = await connectOnce();
        if (outcome === 'ended-clean' || closed) {
          return;
        }
        // Transport ended without a done/error frame -- reconnect.
      } catch (err) {
        if (closed) return;
        if (err instanceof ApiError) {
          opts.onError(err);
          return;
        }
      }

      reconnectAttempt += 1;
      if (reconnectAttempt > maxAttempts) {
        opts.onError(new Error(`compile events stream lost after ${maxAttempts} reconnect attempts`));
        return;
      }
      await new Promise<void>((resolve) => {
        reconnectTimer = setTimeout(resolve, backoffMs(reconnectAttempt));
      });
    }
  }

  run().catch((err) => {
    if (!closed) opts.onError(err instanceof Error ? err : new Error(String(err)));
  });

  return () => {
    if (closed) return;
    closed = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    abortController?.abort();
  };
}

// ---------------------------------------------------------------------------
// Public surface
// ---------------------------------------------------------------------------

export const api = {
  health,
  listGraphs,
  getGraph,
  putGraph,
  deleteGraph,
  listTemplates,
  getModels,
  getSettings,
  putSettings,
  testConnection,
  reviewGraph,
  exportGraph,
  startCompile,
  getCompileSnapshot,
  cancelCompile,
  subscribeCompileEvents,
  generateGraph,
  startRun,
  listRuns,
  getRun,
  getJobSnapshot,
  cancelJob,
  subscribeJobEvents,
};
