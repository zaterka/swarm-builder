// Client-side mirror of `src/swarm_builder/slugify.py` -- see
// GROUP6_PLAN.md decision 3 for why this duplication exists (node ids
// must be assigned once, client-side, at node-creation time, and the
// algorithm must exactly match the server's `review.py::_check_node_ids`
// re-check or a client-valid document fails server review).
//
// Algorithm (mirrors slugify.py's docstring exactly):
// 1. Unicode-normalize (NFKD) and drop everything that does not fold to
//    ASCII.
// 2. Lowercase, collapse every run of non [a-z0-9] to a single
//    underscore, trim leading/trailing underscores.
// 3. An empty result falls back to `step_<index>`.
// 4. A leading digit is prefixed with an underscore.
// 5. A result colliding with a Python keyword gets a trailing
//    underscore.
// 6. `slugifyTitles` deduplicates collisions across an ordered list with
//    a numeric suffix (_2, _3, ...).

// The COMPLETE keyword.kwlist + keyword.softkwlist for the pinned
// server interpreter (Python 3.14.5, per `.python-version` -- captured
// by running `python -c "import keyword; print(keyword.kwlist,
// keyword.softkwlist)"` against that exact interpreter). Do not trim
// this to a "plausible subset" -- review.py re-checks with the same two
// stdlib functions and hard-errors on any mismatch (GROUP6_PLAN.md
// review finding C2).
const PYTHON_KEYWORDS = new Set<string>([
  // keyword.kwlist (35 entries)
  'False',
  'None',
  'True',
  'and',
  'as',
  'assert',
  'async',
  'await',
  'break',
  'class',
  'continue',
  'def',
  'del',
  'elif',
  'else',
  'except',
  'finally',
  'for',
  'from',
  'global',
  'if',
  'import',
  'in',
  'is',
  'lambda',
  'nonlocal',
  'not',
  'or',
  'pass',
  'raise',
  'return',
  'try',
  'while',
  'with',
  'yield',
  // keyword.softkwlist (4 entries)
  '_',
  'case',
  'match',
  'type',
]);

const NON_SLUG_CHARS_RE = /[^a-z0-9]+/g;
const IDENTIFIER_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

function fallback(index: number): string {
  return `step_${index}`;
}

/** Slugify one title in isolation (no cross-title deduplication). */
export function slugify(title: string, index = 0): string {
  // NFKD-normalize then drop anything that doesn't fold into the ASCII
  // range, mirroring Python's `.encode("ascii", "ignore")`.
  const normalized = title.normalize('NFKD');
  const asciiOnly = normalized.replace(/[^\x00-\x7F]/g, '');
  const lowered = asciiOnly.toLowerCase();
  const collapsed = lowered.replace(NON_SLUG_CHARS_RE, '_').replace(/^_+|_+$/g, '');

  if (!collapsed) {
    return fallback(index);
  }

  let result = collapsed;
  if (/^[0-9]/.test(result)) {
    result = `_${result}`;
  }
  if (PYTHON_KEYWORDS.has(result)) {
    result = `${result}_`;
  }
  if (!IDENTIFIER_RE.test(result)) {
    return fallback(index);
  }
  return result;
}

/**
 * Slugify an ordered list of titles, deduplicating collisions with a
 * numeric suffix. Returns one slug per input title, in order.
 */
export function slugifyTitles(titles: readonly string[]): string[] {
  const used = new Set<string>();
  const result: string[] = [];
  titles.forEach((title, index) => {
    const base = slugify(title, index);
    let candidate = base;
    let suffix = 2;
    while (used.has(candidate)) {
      candidate = `${base}_${suffix}`;
      suffix += 1;
    }
    used.add(candidate);
    result.push(candidate);
  });
  return result;
}

/**
 * Assign a fresh, unique node id for a newly-created node, given the
 * titles/ids of every node that already exists. Mirrors slugifyTitles's
 * dedup search but seeded against an existing id set rather than
 * re-slugifying the whole list, since existing node ids must never be
 * recomputed (GROUP6_PLAN.md decision 3 -- "assigned exactly once, at
 * node creation... and never changes again").
 */
export function assignNodeId(title: string, existingIds: readonly string[], index = 0): string {
  const existing = new Set(existingIds);
  const base = slugify(title, index);
  let candidate = base;
  let suffix = 2;
  while (existing.has(candidate)) {
    candidate = `${base}_${suffix}`;
    suffix += 1;
  }
  return candidate;
}
