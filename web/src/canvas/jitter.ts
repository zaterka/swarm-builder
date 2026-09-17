// A stable, deterministic jitter angle derived from a node's own id --
// never re-randomized on re-render (theme.css's comment on this
// matters: "no visual state here is ever persisted in the graph
// document", and it must also never appear to "jump" between renders).
export function jitterDegForId(id: string): number {
  let hash = 0;
  for (let i = 0; i < id.length; i += 1) {
    hash = (hash * 31 + id.charCodeAt(i)) & 0xffffffff;
  }
  // Map to a small range, e.g. [-2, 2] degrees.
  const normalized = (Math.abs(hash) % 400) / 100 - 2;
  return normalized;
}
