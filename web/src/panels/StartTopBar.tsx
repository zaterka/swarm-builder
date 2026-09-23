import type { HealthResponse } from '../api/schema';
import { modelSourceLabel } from './modelSource';
import { modelState } from './modelState';

/**
 * The start screen's top bar: the same chrome the workspace has, so one habit
 * works on both screens.
 *
 * It is a sibling of the start column rather than a child of it: `.sb-start` is
 * a centered 860px column with its own padding, so a bar inside it would be
 * clipped, inset from the window edges and pushed down — visibly not the
 * toolbar it is meant to mirror.
 *
 * The **button is always rendered**, in every state including before
 * `/api/health` answers, and always says "Model settings": a screen whose only
 * way to configure a model appears and disappears is a screen that is missing it
 * exactly when someone looks. Everything that *changes* with state lives in the
 * summary (and in the in-page notice, for the three states that need action).
 */
export function StartTopBar(props: {
  health: HealthResponse | null;
  onOpenSettings: () => void;
}) {
  const { health } = props;
  const state = modelState(health);
  const model = health?.resolvedModel;
  const configured = health?.modelConfigured ?? false;

  // The summary is a *value*, never a sentence: the notice below owns the
  // claims. With nothing configured and dry run on it says exactly that, rather
  // than presenting the internal offline default as a choice the user made.
  const summary =
    state === 'pending'
      ? 'Checking…'
      : configured && model
        ? `${model.provider} / ${model.model}`
        : state === 'dry'
          ? 'offline default (not configured)'
          : 'not configured';

  const provenance = configured && model ? modelSourceLabel(model.source) : null;

  return (
    <div className="sb-toolbar sb-toolbar-start" data-state={state} data-testid="model-bar">
      <span className="sb-model-bar-label">Model</span>
      <span
        className="sb-model-bar-summary"
        // Provenance belongs in the accessible name, not only in a `title`: a
        // title is not reliably announced, and the row this bar replaces had
        // provenance in its text, so attribute-only would be a regression.
        aria-label={provenance ? `${summary}, ${provenance}` : summary}
        title={provenance ?? undefined}
      >
        {summary}
      </span>
      {health?.dryRun && (
        <span
          className="sb-dry-run-badge"
          title="Describe and compile use built-in stubs; runs use a keyless test model."
        >
          DRY RUN{health.dryRunForcedByEnv ? ' (env)' : ''}
        </span>
      )}
      <button
        className="sb-btn-ghost sb-btn-sm sb-model-bar-action"
        onClick={props.onOpenSettings}
        title="Choose the provider and API key used for describing, compiling and running"
      >
        Model settings
      </button>
    </div>
  );
}

export default StartTopBar;
