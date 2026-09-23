import { describe, it, expect } from 'vitest';
import type { HealthResponse } from '../api/schema';
import { modelState, needsAttention } from './modelState';

// One decision, two surfaces (the start screen's top bar and its in-page
// notice), so the precedence is pinned directly rather than only through the
// DOM. The credential case is the one worth being strict about: it is derived
// from structured flags, never from a server sentence, so a reworded blocker
// cannot change what the screen claims — and a missing `uv` must never be
// reported as a missing key.

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

describe('modelState', () => {
  it.each([
    ['no health yet', null, 'pending'],
    ['an unreadable app settings file', { ...BASE, appConfigError: 'boom' }, 'config'],
    ['an unreadable inherited file', { ...BASE, settingsError: 'boom' }, 'config'],
    ['a broken file even when a model resolves', { ...CONFIGURED, appConfigError: 'boom' }, 'config'],
    ['nothing configured', BASE, 'warn'],
    ['a configured, ready model', CONFIGURED, 'ok'],
    ['dry run with a model', { ...CONFIGURED, dryRun: true }, 'dry'],
    ['dry run with nothing configured', { ...BASE, dryRun: true, runReady: true }, 'dry'],
    ['a configured model with no credential', { ...CONFIGURED, runReady: false }, 'cred'],
    ['uv missing', { ...CONFIGURED, runReady: false, uvAvailable: false }, 'ok'],
    ['an unwritable workspace', { ...CONFIGURED, runReady: false, workspaceWritable: false }, 'ok'],
    ['nothing configured and uv missing', { ...BASE, uvAvailable: false }, 'warn'],
  ] as [string, HealthResponse | null, string][])('%s -> %s', (_label, health, expected) => {
    expect(modelState(health)).toBe(expected);
  });

  it('prefers dry run over a missing credential', () => {
    // Dry run needs no credential at all, so blaming one would be wrong.
    expect(modelState({ ...CONFIGURED, dryRun: true, runReady: false })).toBe('dry');
  });

  it('marks exactly the states that need the user to act', () => {
    expect(needsAttention('warn')).toBe(true);
    expect(needsAttention('cred')).toBe(true);
    expect(needsAttention('config')).toBe(true);
    expect(needsAttention('ok')).toBe(false);
    expect(needsAttention('dry')).toBe(false);
    expect(needsAttention('pending')).toBe(false);
  });
});
