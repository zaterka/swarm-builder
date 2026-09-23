import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import GraphPicker from './GraphPicker';
import type { HealthResponse } from '../api/schema';

// The start screen's body. Since PLAN-V3.2 the model lives in the shell's top
// bar (see StartTopBar.test.tsx) and this page carries only a *notice*, and only
// when the user has to act. Two things are therefore worth asserting here:
//
//   1. in the steady state nothing sits between "New graph" and "Describe your
//      workflow" -- asserted by DOM order, not merely by the notice's absence;
//   2. when something does need doing, the notice names the remedy in this
//      application's own words, and names nothing outside it.
//
// `health` is a prop now: the shell owns the read, so the bar and this notice
// can never disagree about the model.

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const BASE_HEALTH: HealthResponse = {
  version: '0.1.0',
  dshHome: '/tmp/dsh',
  settingsError: null,
  resolvedModel: { provider: 'deepseek-official', model: 'deepseek-v4-flash', source: 'bundle-default' },
  uvAvailable: true,
  uvCacheDir: '/tmp/uv-cache',
  uvCacheWritable: true,
  workspaceDir: '/tmp/swarm-workspace',
  workspaceWritable: true,
  webDistPresent: true,
  compileReady: false,
  blockers: [
    'No model is configured yet. Choose a provider and add your API key in Model settings, or turn on Dry run mode to build and run offline.',
  ],
  runReady: false,
  runBlockers: [],
  dryRun: false,
  dryRunForcedByEnv: false,
  modelConfigured: false,
  appConfigPath: '/tmp/swarm-workspace/settings.json',
  appConfigError: null,
};

const CONFIGURED: HealthResponse = {
  ...BASE_HEALTH,
  resolvedModel: { provider: 'openai', model: 'gpt-6-astra', source: 'app-config' },
  modelConfigured: true,
  compileReady: true,
  runReady: true,
  blockers: [],
};

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      // GeneratePanel still reads its own health: it is also the in-graph
      // popover, where no caller has one to hand down.
      if (url.endsWith('/api/health')) return jsonResponse(BASE_HEALTH);
      if (url.endsWith('/api/graphs')) return jsonResponse({ graphs: [], errors: [] });
      return jsonResponse({}, 404);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function notice(): HTMLElement {
  return screen.getByTestId('model-notice');
}

function renderPicker(health: HealthResponse | null, onOpenSettings = () => {}) {
  return render(
    <GraphPicker
      onOpen={() => {}}
      onCreateNew={() => {}}
      onGenerated={() => {}}
      onOpenSettings={onOpenSettings}
      settingsRevision={0}
      health={health}
    />,
  );
}

describe('GraphPicker model notice', () => {
  it('offers a way out when nothing is configured', async () => {
    const onOpenSettings = vi.fn();
    renderPicker(BASE_HEALTH, onOpenSettings);

    expect(notice()).toHaveAttribute('data-state', 'warn');
    expect(within(notice()).getByText('No model is configured yet.')).toBeInTheDocument();
    expect(notice().textContent).toContain('turn on Dry run mode');

    within(notice()).getByRole('button', { name: 'Set up a model' }).click();
    expect(onOpenSettings).toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText(/Compile blocked/)).toBeInTheDocument());
  });

  it('asks for a key only when a credential is the missing piece', () => {
    renderPicker({ ...CONFIGURED, runReady: false, compileReady: false });

    expect(notice()).toHaveAttribute('data-state', 'cred');
    expect(notice().textContent).toContain('No API key for this model yet');
    expect(within(notice()).getByRole('button', { name: 'Add a key' })).toBeInTheDocument();
  });

  it.each([
    ['uv is missing', { uvAvailable: false }],
    ['the workspace is unwritable', { workspaceWritable: false }],
  ])('never blames the key when the real cause is %s', (_label, override) => {
    renderPicker({ ...CONFIGURED, runReady: false, compileReady: false, ...override });

    // The state falls back to `ok`, so there is no notice at all and nothing
    // claims a credential is missing; the strip still lists the server's reason.
    expect(screen.queryByTestId('model-notice')).not.toBeInTheDocument();
  });

  it('reports a broken settings file instead of asking for a key', () => {
    renderPicker({ ...BASE_HEALTH, appConfigError: 'failed to parse settings.json' });

    expect(notice()).toHaveAttribute('data-state', 'config');
    expect(notice().textContent).toContain('could not be read');
    expect(notice().textContent).not.toContain('API key');
    expect(
      within(notice()).getByRole('button', { name: 'Fix in Model settings' }),
    ).toBeInTheDocument();
  });

  it.each([
    ['a configured model', CONFIGURED],
    ['dry run', { ...CONFIGURED, dryRun: true }],
    ['health not resolved yet', null],
  ] as [string, HealthResponse | null][])(
    'puts nothing between the hero and Describe for %s',
    (_label, health) => {
      renderPicker(health);

      expect(screen.queryByTestId('model-notice')).not.toBeInTheDocument();
      // Position, not just absence: the hero's next sibling is the Describe box.
      const hero = document.querySelector('.sb-start-hero');
      expect(hero?.nextElementSibling?.className).toContain('sb-start-generate');
    },
  );

  it('never names a component outside the application, in text or attributes', () => {
    renderPicker(BASE_HEALTH);

    const el = notice();
    const attributes = Array.from(el.querySelectorAll('*'))
      .flatMap((node) => [node.getAttribute('title'), node.getAttribute('aria-label')])
      .filter((value): value is string => typeof value === 'string');
    const haystack = [el.textContent ?? '', ...attributes].join(' ');

    for (const foreign of [
      'settings.yaml',
      'DSH_HOME',
      'agent-default-model',
      'SWARM_MODEL',
      'harness',
    ]) {
      expect(haystack).not.toContain(foreign);
    }
    expect(haystack).not.toContain('/Users/');
    expect(haystack).not.toContain('/tmp/');
  });

  it('leaves the model to the top bar', async () => {
    // The environment strip used to print it too; three surfaces describing one
    // source is what PLAN-V3.2 removed.
    renderPicker(CONFIGURED);

    await waitFor(() => expect(screen.getByText(/Ready to compile/)).toBeInTheDocument());
    expect(screen.queryByText(/openai \/ gpt-6-astra/)).not.toBeInTheDocument();
    // What the strip keeps: readiness, the workspace path and the version.
    expect(screen.getByTitle('/tmp/swarm-workspace')).toBeInTheDocument();
    expect(screen.getByText('v0.1.0')).toBeInTheDocument();
  });

  it('follows the health the shell hands it', () => {
    const { rerender } = renderPicker(BASE_HEALTH);
    expect(notice()).toHaveAttribute('data-state', 'warn');

    // The shell re-reads health after a settings save and hands the new value
    // down; the notice disappears because there is nothing left to do.
    rerender(
      <GraphPicker
        onOpen={() => {}}
        onCreateNew={() => {}}
        onGenerated={() => {}}
        onOpenSettings={() => {}}
        settingsRevision={1}
        health={CONFIGURED}
      />,
    );

    expect(screen.queryByTestId('model-notice')).not.toBeInTheDocument();
  });
});
