import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import DryRunBadge from './DryRunBadge';

// The workspace toolbar's chip. PLAN-V3.2 gave the *start* screen its own bar,
// which renders its chip from the same CSS class — deliberately **not** by
// reusing this component with an optional `health` prop, which would have put
// two code paths (and a null-vs-undefined trap on the first render) into the one
// component the workspace depends on.
//
// So this test exists to pin the behaviour that restructure must not touch: the
// badge still reads its own health, and still says nothing when dry run is off.

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

const HEALTH = {
  version: '0.1.0',
  dshHome: '/tmp/dsh',
  settingsError: null,
  resolvedModel: { provider: 'openai', model: 'gpt-6-astra', source: 'app-config' },
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

let body = { ...HEALTH };
let calls = 0;

beforeEach(() => {
  body = { ...HEALTH };
  calls = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/api/health')) {
        calls += 1;
        return jsonResponse(body);
      }
      return jsonResponse({});
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('DryRunBadge', () => {
  it('fetches its own health and stays silent when dry run is off', async () => {
    render(<DryRunBadge revision={0} />);

    await waitFor(() => expect(calls).toBe(1));
    expect(screen.queryByText(/DRY RUN/)).not.toBeInTheDocument();
  });

  it('renders the chip when dry run is on, and marks an environment-forced one', async () => {
    body = { ...HEALTH, dryRun: true };
    render(<DryRunBadge revision={0} />);
    expect(await screen.findByText('DRY RUN')).toBeInTheDocument();
    cleanup();

    body = { ...HEALTH, dryRun: true, dryRunForcedByEnv: true };
    render(<DryRunBadge revision={1} />);
    expect(await screen.findByText('DRY RUN (env)')).toBeInTheDocument();
  });

  it('re-reads on a new revision', async () => {
    const { rerender } = render(<DryRunBadge revision={0} />);
    await waitFor(() => expect(calls).toBe(1));

    body = { ...HEALTH, dryRun: true };
    rerender(<DryRunBadge revision={1} />);

    expect(await screen.findByText('DRY RUN')).toBeInTheDocument();
    expect(calls).toBe(2);
  });
});
