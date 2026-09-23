import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import ModelSettings from './ModelSettings';
import type { SettingsResponse } from '../api/schema';

// The model-settings screen is where a brand-new user configures a provider
// and a key, and where dry run is switched on. Three properties matter enough
// to assert directly:
//
//   1. a saved key is never rendered (the server never sends it, and the form
//      must not imply otherwise -- the field is a password box that starts
//      empty, and only "configured (…abcd)" is shown);
//   2. saving sends exactly what the form shows, with the base URL only for
//      the provider that needs one, so a curated provider cannot silently be
//      routed through someone's proxy;
//   3. an environment-forced dry run is explained and locked, never silently
//      ignored.

const SETTINGS_BODY: SettingsResponse = {
  configPath: '/tmp/swarm-workspace/settings.json',
  configError: null,
  model: null,
  dryRun: false,
  dryRunForcedByEnv: false,
  dryRunEnvVars: [],
  providers: [
    {
      key: 'openai',
      label: 'OpenAI',
      requiresApiKey: true,
      requiresBaseUrl: false,
      apiKeyEnv: 'OPENAI_API_KEY',
      defaultModel: 'gpt-6-astra',
      models: ['gpt-6-astra', 'gpt-5.6-luna'],
      note: null,
      usable: true,
      unusableReason: null,
    },
    {
      key: 'custom',
      label: 'Custom OpenAI-compatible endpoint',
      requiresApiKey: true,
      requiresBaseUrl: true,
      apiKeyEnv: 'SWARM_API_KEY',
      defaultModel: null,
      models: [],
      note: 'Any server that speaks the OpenAI chat-completions API.',
      usable: false,
      unusableReason: 'a base URL is required',
    },
  ],
  resolvedDefault: { provider: 'deepseek-official', model: 'deepseek-v4-flash', source: 'bundle-default' },
  inheritedRoutes: 0,
  workspaceWritable: true,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

let requests: { url: string; method: string; body: unknown }[] = [];
let settingsBody: SettingsResponse = SETTINGS_BODY;
let putResponder: (body: unknown) => Response = () => jsonResponse(settingsBody);
let testResponder: () => Response = () => jsonResponse({ ok: true, detail: 'ok', latencyMs: 42, model: 'openai/gpt-6-astra' });

beforeEach(() => {
  requests = [];
  settingsBody = SETTINGS_BODY;
  putResponder = () => jsonResponse(settingsBody);
  testResponder = () =>
    jsonResponse({ ok: true, detail: 'ok', latencyMs: 42, model: 'openai/gpt-6-astra' });

  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? 'GET';
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      requests.push({ url, method, body });

      if (url.endsWith('/api/settings') && method === 'GET') return jsonResponse(settingsBody);
      if (url.endsWith('/api/settings') && method === 'PUT') return putResponder(body);
      if (url.endsWith('/api/settings/test')) return testResponder();
      return jsonResponse({ detail: `unexpected ${method} ${url}` }, 500);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('ModelSettings', () => {
  it('renders the provider catalog and no secret', async () => {
    render(<ModelSettings onClose={() => {}} />);

    await waitFor(() => expect(screen.getByLabelText('Provider')).toBeInTheDocument());
    expect(screen.getByRole('option', { name: 'OpenAI' })).toBeInTheDocument();
    // An unusable provider explains itself instead of failing later.
    expect(screen.getByRole('option', { name: /Custom OpenAI-compatible endpoint — a base URL is required/ })).toBeDisabled();
  });

  it('shows a stored key only as a hint, never as a value', async () => {
    settingsBody = {
      ...SETTINGS_BODY,
      model: {
        provider: 'openai',
        model: 'gpt-6-astra',
        baseUrl: null,
        reasoningEffort: null,
        hasApiKey: true,
        apiKeyHint: '…ab12',
      },
      resolvedDefault: { provider: 'openai', model: 'gpt-6-astra', source: 'app-config' },
    };

    render(<ModelSettings onClose={() => {}} />);

    const keyField = await screen.findByLabelText('API key');
    expect(keyField).toHaveValue('');
    expect(keyField).toHaveAttribute('type', 'password');
    expect(keyField).toHaveAttribute('placeholder', 'configured (…ab12)');
    expect(screen.getByText(/set in Model settings/)).toBeInTheDocument();
  });

  it('saves the provider, model and key', async () => {
    settingsBody = { ...SETTINGS_BODY };
    render(<ModelSettings onClose={() => {}} />);

    fireEvent.change(await screen.findByLabelText('Provider'), { target: { value: 'openai' } });
    fireEvent.change(screen.getByLabelText('Model id'), { target: { value: 'gpt-5.6-luna' } });
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-typed' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save model' }));

    await waitFor(() => expect(requests.some((r) => r.method === 'PUT')).toBe(true));
    const put = requests.find((r) => r.method === 'PUT')!;
    expect(put.url).toContain('/api/settings');
    expect(put.body).toEqual({
      model: {
        provider: 'openai',
        model: 'gpt-5.6-luna',
        baseUrl: null,
        apiKey: 'sk-typed',
        clearApiKey: false,
      },
    });
  });

  it('surfaces every validation problem the server reports', async () => {
    putResponder = () =>
      jsonResponse({ detail: ['a base URL is required', 'an unknown provider'] }, 422);
    render(<ModelSettings onClose={() => {}} />);

    fireEvent.change(await screen.findByLabelText('Provider'), { target: { value: 'openai' } });
    fireEvent.change(screen.getByLabelText('Model id'), { target: { value: 'gpt-6-astra' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save model' }));

    await waitFor(() => expect(screen.getByText('a base URL is required')).toBeInTheDocument());
    expect(screen.getByText('an unknown provider')).toBeInTheDocument();
  });

  it('reports a failed connection without exposing the key', async () => {
    testResponder = () =>
      jsonResponse({ ok: false, detail: 'AuthenticationError: invalid key', latencyMs: null, model: 'openai/gpt-6-astra' });
    render(<ModelSettings onClose={() => {}} />);

    fireEvent.change(await screen.findByLabelText('Provider'), { target: { value: 'openai' } });
    fireEvent.change(screen.getByLabelText('Model id'), { target: { value: 'gpt-6-astra' } });
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-typed' } });
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));

    await waitFor(() => expect(screen.getByText(/Connection failed/)).toBeInTheDocument());
    expect(document.body.textContent).not.toContain('sk-typed');
  });

  it('toggles dry run and explains an environment lock', async () => {
    settingsBody = {
      ...SETTINGS_BODY,
      dryRun: true,
      dryRunForcedByEnv: true,
      dryRunEnvVars: ['SWARM_FAKE_FILL'],
    };
    render(<ModelSettings onClose={() => {}} />);

    const toggle = await screen.findByLabelText('Dry run mode');
    expect(toggle).toBeChecked();
    expect(toggle).toBeDisabled();
    expect(screen.getByText(/Forced on by the environment: SWARM_FAKE_FILL/)).toBeInTheDocument();
  });

  it('turns dry run on with a minimal request body', async () => {
    render(<ModelSettings onClose={() => {}} />);

    const toggle = await screen.findByLabelText('Dry run mode');
    fireEvent.click(toggle);

    await waitFor(() => expect(requests.some((r) => r.method === 'PUT')).toBe(true));
    expect(requests.find((r) => r.method === 'PUT')!.body).toEqual({ dryRun: true });
  });
});
