import { describe, it, expect, vi, afterEach } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import StartTopBar from './StartTopBar';
import type { HealthResponse } from '../api/schema';
import { modelSourceLabel } from './modelSource';

// The start screen's chrome. Two properties matter enough to assert directly:
//
//   1. the "Model settings" button is there in *every* state, including before
//      /api/health answers -- a screen whose only way to configure a model
//      appears and disappears is missing it exactly when someone looks;
//   2. the summary is a value, never a claim, and never presents the internal
//      offline default as a choice the user made.

const BASE: HealthResponse = {
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
  blockers: [],
  runReady: false,
  runBlockers: [],
  dryRun: false,
  dryRunForcedByEnv: false,
  modelConfigured: false,
  appConfigPath: '/tmp/swarm-workspace/settings.json',
  appConfigError: null,
};

const CONFIGURED: HealthResponse = {
  ...BASE,
  resolvedModel: { provider: 'openai', model: 'gpt-6-astra', source: 'app-config' },
  modelConfigured: true,
  compileReady: true,
  runReady: true,
};

function bar(): HTMLElement {
  return screen.getByTestId('model-bar');
}

afterEach(cleanup);

describe('StartTopBar', () => {
  it.each([
    ['pending', null, 'Checking…'],
    ['warn', BASE, 'not configured'],
    ['ok', CONFIGURED, 'openai / gpt-6-astra'],
    ['cred', { ...CONFIGURED, runReady: false }, 'openai / gpt-6-astra'],
    ['dry with a model', { ...CONFIGURED, dryRun: true }, 'openai / gpt-6-astra'],
    ['dry with nothing configured', { ...BASE, dryRun: true, runReady: true }, 'offline default (not configured)'],
    ['config', { ...BASE, appConfigError: 'boom' }, 'not configured'],
  ] as [string, HealthResponse | null, string][])(
    'summarises %s as %s',
    (state, health, summary) => {
      render(<StartTopBar health={health} onOpenSettings={() => {}} />);

      expect(bar()).toHaveAttribute('data-state', state.split(' ')[0]!);
      expect(bar().textContent).toContain(summary);
    },
  );

  it('always offers the settings button, even before health resolves', () => {
    const onOpenSettings = vi.fn();
    render(<StartTopBar health={null} onOpenSettings={onOpenSettings} />);

    const button = screen.getByRole('button', { name: 'Model settings' });
    button.click();
    expect(onOpenSettings).toHaveBeenCalled();
  });

  it('carries provenance in the accessible name, not only in a title', () => {
    // A `title` is not reliably announced, and the row this bar replaced had
    // provenance in its text: attribute-only would be a regression.
    render(<StartTopBar health={CONFIGURED} onOpenSettings={() => {}} />);

    const summary = screen.getByLabelText(
      `openai / gpt-6-astra, ${modelSourceLabel('app-config')}`,
    );
    expect(summary).toBeInTheDocument();
    expect(summary).toHaveAttribute('title', modelSourceLabel('app-config'));
  });

  it('shows the dry-run chip, and says when the environment forced it', () => {
    render(<StartTopBar health={{ ...CONFIGURED, dryRun: true }} onOpenSettings={() => {}} />);
    expect(screen.getByText('DRY RUN')).toBeInTheDocument();
    cleanup();

    render(
      <StartTopBar
        health={{ ...CONFIGURED, dryRun: true, dryRunForcedByEnv: true }}
        onOpenSettings={() => {}}
      />,
    );
    expect(screen.getByText('DRY RUN (env)')).toBeInTheDocument();
  });

  it('shows no chip when dry run is off', () => {
    render(<StartTopBar health={CONFIGURED} onOpenSettings={() => {}} />);
    expect(screen.queryByText(/DRY RUN/)).not.toBeInTheDocument();
  });

  it('never names a component outside the application, in text or attributes', () => {
    render(<StartTopBar health={{ ...BASE, appConfigError: 'boom' }} onOpenSettings={() => {}} />);

    const el = bar();
    const attributes = Array.from(el.querySelectorAll('*'))
      .flatMap((node) => [node.getAttribute('title'), node.getAttribute('aria-label')])
      .filter((value): value is string => typeof value === 'string');
    const haystack = [el.textContent ?? '', ...attributes].join(' ');

    for (const foreign of ['settings.yaml', 'DSH_HOME', 'agent-default-model', 'SWARM_MODEL', 'harness']) {
      expect(haystack).not.toContain(foreign);
    }
    expect(haystack).not.toContain('/Users/');
    expect(haystack).not.toContain('/tmp/');
  });
});
