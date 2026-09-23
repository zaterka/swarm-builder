import type { HealthResponse } from '../api/schema';

/**
 * What the start screen says about the model, as one decision.
 *
 * Two surfaces read this: the top bar (always on screen, chrome) and the
 * in-page notice (only when something needs doing). They must never disagree,
 * so the decision is made once, here, from one `health` value — and it lives in
 * its own module because both the shell (`App.tsx`, which renders the bar) and a
 * panel (`GraphPicker`, which renders the notice) need it, and a shell importing
 * from a panel it renders would be backwards.
 *
 * **State comes from structured fields only.** The credential case is derived
 * from `runReady` *minus* every other cause of unreadiness (`uv`, the workspace,
 * a broken settings file). Nothing here classifies a server sentence, so a
 * reworded blocker can never silently change what the screen claims, and the
 * screen never blames a missing key for a missing `uv`.
 */
export type ModelState = 'pending' | 'config' | 'warn' | 'cred' | 'dry' | 'ok';

/** The screen's model state, as an explicit precedence: first match wins. */
export function modelState(health: HealthResponse | null): ModelState {
  if (!health) return 'pending';
  // A configuration file we cannot use is reported in its own right, because
  // "add your API key" is the wrong remedy for it.
  if (health.settingsError || health.appConfigError) return 'config';
  if (!health.modelConfigured && !health.dryRun) return 'warn';
  if (!health.dryRun && health.modelConfigured && !health.runReady) {
    // Only the credential can be missing here: every other cause of
    // `!runReady` is one of these flags, and when one is set the screen stays
    // quiet about credentials and lets the environment strip explain.
    const otherCause = !health.uvAvailable || !health.workspaceWritable;
    if (!otherCause) return 'cred';
  }
  if (health.dryRun) return 'dry';
  return 'ok';
}

/** Whether this state is one the user has to act on (the notice's condition). */
export function needsAttention(state: ModelState): boolean {
  return state === 'warn' || state === 'cred' || state === 'config';
}
