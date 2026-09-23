import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { HealthResponse } from '../api/schema';

/**
 * A persistent "DRY RUN" marker.
 *
 * Dry run is a mode in which the pipeline answers with deterministic stubs and
 * keyless test models instead of real model output. That is genuinely useful
 * (build and run with no credentials at all) and genuinely dangerous to
 * mistake for real output, so the mode is never silent: this badge is rendered
 * for as long as it is on, wherever the user is.
 *
 * It reads `/api/health` itself rather than taking the state as a prop, so a
 * toggle from the settings screen is reflected everywhere without threading a
 * store through the shell. `revision` is bumped by the shell after a save,
 * which is all the invalidation the fresh-per-request server needs.
 */
export function DryRunBadge({ revision = 0 }: { revision?: number }) {
  const [health, setHealth] = useState<HealthResponse | null>(null);

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch(() => setHealth(null));
  }, [revision]);

  if (!health?.dryRun) return null;

  return (
    <span
      className="sb-dry-run-badge"
      role="status"
      title={
        health.dryRunForcedByEnv
          ? 'Dry run is on because the server environment sets it; the pipeline uses built-in stubs and a keyless test model. A job already running keeps the setting it started with.'
          : 'Dry run is on: generating and compiling use built-in stubs and runs use a keyless test model, with no credentials in the child environment. Output is not real model output. A job already running keeps the setting it started with.'
      }
    >
      DRY RUN
      {health.dryRunForcedByEnv ? ' (env)' : ''}
    </span>
  );
}

export default DryRunBadge;
