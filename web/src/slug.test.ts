import { describe, it, expect } from 'vitest';
import { slugify, slugifyTitles } from './slug';

// Mirrors tests/test_slugify.py exactly (GROUP6_PLAN.md decision 3 /
// review finding C2 -- the algorithm and the keyword blocklist must be
// byte-for-byte parity with the server's slugify.py, since review.py
// re-checks with the same two stdlib functions and hard-errors on any
// mismatch).

describe('slugify', () => {
  it('slugifies a simple title', () => {
    expect(slugify('Summarize results')).toBe('summarize_results');
  });

  it('falls back to step_<index> for a non-ASCII title', () => {
    expect(slugify('日本語のタイトル', 3)).toBe('step_3');
  });

  it('falls back to step_<index> for a punctuation-only title', () => {
    expect(slugify('!!!???', 5)).toBe('step_5');
  });

  it('falls back to step_<index> for an empty title', () => {
    expect(slugify('', 7)).toBe('step_7');
  });

  it('appends a trailing underscore for a Python hard keyword', () => {
    expect(slugify('import')).toBe('import_');
  });

  it('appends a trailing underscore for a Python soft keyword', () => {
    // Soft keywords are the trap this parity test exists to catch
    // (GROUP6_PLAN.md review finding C2): "Match", "Case", "Type" all
    // fold to a bare soft keyword and need the trailing underscore too.
    expect(slugify('Match')).toBe('match_');
    expect(slugify('Case')).toBe('case_');
    expect(slugify('Type')).toBe('type_');
  });

  it('prefixes a leading digit with an underscore', () => {
    expect(slugify('123 Go')).toBe('_123_go');
  });
});

describe('slugifyTitles', () => {
  it('deduplicates repeated titles with a numeric suffix', () => {
    const slugs = slugifyTitles(['Research', 'Research', 'Research']);
    expect(slugs).toEqual(['research', 'research_2', 'research_3']);
    expect(new Set(slugs).size).toBe(3);
  });

  it('is stable and order-dependent across repeated calls', () => {
    const titles = ['Fetch', 'Fetch', 'Summarize'];
    const first = slugifyTitles(titles);
    const second = slugifyTitles([...titles]);
    expect(first).toEqual(second);
    expect(first).toEqual(['fetch', 'fetch_2', 'summarize']);
  });

  it('does not let a dedup suffix collide with a later organic match', () => {
    const slugs = slugifyTitles(['Fetch', 'Fetch', 'Fetch 2']);
    expect(new Set(slugs).size).toBe(3);
    expect(slugs[0]).toBe('fetch');
    expect(slugs[1]).toBe('fetch_2');
    expect(slugs[2]).not.toBe(slugs[0]);
    expect(slugs[2]).not.toBe(slugs[1]);
  });

  it('produces a valid, unique, non-keyword identifier for every title in a mixed batch', () => {
    const titles = [
      'Summarize results',
      '日本語',
      '!!!',
      'import',
      'class',
      '123 kickoff',
      'Summarize results',
      '',
    ];
    const slugs = slugifyTitles(titles);
    expect(slugs).toHaveLength(titles.length);
    expect(new Set(slugs).size).toBe(slugs.length);
    for (const slug of slugs) {
      expect(slug).toMatch(/^[A-Za-z_][A-Za-z0-9_]*$/);
    }
  });
});
