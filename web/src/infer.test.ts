import { describe, it, expect } from 'vitest';
import { inferTemplate } from './infer';

// Mirrors tests/test_template_registry.py's inference cases exactly,
// PLUS a genuine 1-1 tie case proving the corrected tie rule
// (GROUP6_PLAN.md review finding C3): only a 0-0 tie falls back to
// "chat"; a 1-1 (or N-N) tie resolves to whichever template is checked
// first with a nonzero count ("websearch" before "orchestrator").
// Verified empirically against the real server-side
// `infer_template()` before writing these expectations.

describe('inferTemplate', () => {
  it('infers websearch from search/browse/news/latest keywords', () => {
    const result = inferTemplate('Search the web for the latest news on this topic.');
    expect(result.suggestion).toBe('websearch');
    expect(result.matchedKeywords).toContain('search');
    expect(result.matchedKeywords).toContain('latest');
    expect(result.matchedKeywords).toContain('news');
  });

  it('infers orchestrator from delegate/coordinate/sub-agent keywords', () => {
    const result = inferTemplate('Delegate to a sub-agent and coordinate the answer.');
    expect(result.suggestion).toBe('orchestrator');
    expect(result.matchedKeywords).toContain('delegate');
    expect(result.matchedKeywords).toContain('coordinate');
    expect(result.matchedKeywords).toContain('sub-agent');
  });

  it('infers orchestrator from the multi-word "plan and assign" keyword', () => {
    const result = inferTemplate('Plan and assign work to the right specialist.');
    expect(result.suggestion).toBe('orchestrator');
    expect(result.matchedKeywords).toContain('plan and assign');
  });

  it('defaults a genuinely ambiguous (0-0) intent to chat', () => {
    const result = inferTemplate('Have a friendly conversation about cooking.');
    expect(result.suggestion).toBe('chat');
    expect(result.matchedKeywords).toEqual([]);
  });

  it('lets a higher keyword count win over first-checked-template-wins', () => {
    // orchestrator has 2 matches ("delegate", "route") vs websearch's 1
    // ("search") -- orchestrator must win despite websearch being
    // checked first.
    const result = inferTemplate('Delegate and route this search request.');
    expect(result.suggestion).toBe('orchestrator');
  });

  it('resolves a genuine 1-1 tie to websearch (checked first), not chat', () => {
    // Verified server-side: infer_template("delegate to search") ->
    // suggestion="websearch", matched_keywords=("search",). A naive
    // "ties -> chat" implementation (GROUP6_PLAN.md v1's bug) would
    // wrongly return "chat" here.
    const result = inferTemplate('delegate to search');
    expect(result.suggestion).toBe('websearch');
    expect(result.matchedKeywords).toEqual(['search']);
  });

  it('resolves another genuine 1-1 tie to websearch for "Route the news"', () => {
    // Verified server-side: infer_template("Route the news") ->
    // suggestion="websearch", matched_keywords=("news",).
    const result = inferTemplate('Route the news');
    expect(result.suggestion).toBe('websearch');
    expect(result.matchedKeywords).toEqual(['news']);
  });
});
