// Pure client-side keyword template inference (PLAN.md "Templates":
// "Template inference is a pure function in shared/, called
// client-side"). This mirrors
// `src/swarm_builder/templates/registry.py::infer_template` EXACTLY --
// including its tie-breaking rule, which GROUP6_PLAN.md v1 stated
// incorrectly (review finding C3) and v2 corrects here.
//
// registry.py iterates its `_KEYWORDS` dict in insertion order
// (websearch, then orchestrator) and picks the template with the
// STRICTLY GREATER match count seen so far (`if len(matched) >
// best_count`). Only a genuine 0-0 tie (no keyword from either set
// matches) falls back to "chat" -- a 1-1 (or N-N) tie resolves to
// whichever template was checked first with a nonzero count, i.e.
// "websearch" wins a tie against "orchestrator". Order below is
// load-bearing: do not reorder or convert to an object literal whose
// key order isn't guaranteed to survive refactors.
import type { TemplateId } from './api/schema';

const KEYWORDS: ReadonlyArray<readonly [TemplateId, readonly string[]]> = [
  ['websearch', ['search', 'browse', 'news', 'latest']],
  ['orchestrator', ['delegate', 'coordinate', 'route', 'sub-agent', 'plan and assign']],
];

export interface InferenceResult {
  suggestion: TemplateId;
  matchedKeywords: string[];
}

export function inferTemplate(intent: string): InferenceResult {
  const lowered = intent.toLowerCase();

  let bestTemplate: TemplateId = 'chat';
  let bestCount = 0;
  let bestMatched: string[] = [];

  for (const [templateId, keywords] of KEYWORDS) {
    const matched = keywords.filter((kw) => lowered.includes(kw));
    if (matched.length > bestCount) {
      bestCount = matched.length;
      bestTemplate = templateId;
      bestMatched = matched;
    }
  }

  if (bestCount === 0) {
    return { suggestion: 'chat', matchedKeywords: [] };
  }
  return { suggestion: bestTemplate, matchedKeywords: bestMatched };
}
