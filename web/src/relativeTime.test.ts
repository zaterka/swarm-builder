import { describe, it, expect } from 'vitest';
import { formatRelativeTime } from './relativeTime';

const NOW = new Date('2026-09-17T18:00:00Z');

describe('formatRelativeTime', () => {
  it('collapses anything under a minute to "just now"', () => {
    expect(formatRelativeTime('2026-09-17T17:59:40Z', NOW)).toBe('just now');
  });

  it('reports minutes within the hour', () => {
    expect(formatRelativeTime('2026-09-17T17:55:00Z', NOW)).toBe('5 minutes ago');
  });

  it('reports hours within the day', () => {
    expect(formatRelativeTime('2026-09-17T15:00:00Z', NOW)).toBe('3 hours ago');
  });

  it('reports days within the week', () => {
    expect(formatRelativeTime('2026-09-16T18:00:00Z', NOW)).toBe('yesterday');
  });

  it('falls back to an absolute date beyond a week', () => {
    expect(formatRelativeTime('2026-08-05T18:00:00Z', NOW)).toBe('Aug 5');
  });

  it('includes the year for a different year', () => {
    expect(formatRelativeTime('2025-08-05T18:00:00Z', NOW)).toBe('Aug 5, 2025');
  });

  it('returns an empty string for an unparseable timestamp', () => {
    expect(formatRelativeTime('not a date', NOW)).toBe('');
  });
});
