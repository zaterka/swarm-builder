import { useCallback, useEffect, useState } from 'react';
import { api, ApiError, SettingsValidationError } from '../api/client';
import type {
  ProviderOut,
  SettingsResponse,
  TestConnectionResponse,
} from '../api/schema';
import { modelSourceLabel } from './modelSource';

/**
 * The one screen a new user needs: pick a provider, paste a key, optionally
 * toggle dry run (PLAN-V3). Everything here is app-owned -- no file, no
 * environment variable and no component outside Swarm Builder is named.
 *
 * Two deliberate properties:
 *
 * - **The key is write-only.** The server reports only whether one is stored
 *   and its last four characters, so this form never holds a saved secret and
 *   never renders one; the field is a password input that starts empty and a
 *   placeholder that says a key is already configured.
 * - **Dry run is a first-class choice, not a hidden env var.** A brand-new
 *   user can build and run offline from this switch alone, and when the
 *   environment forces dry run on, the switch is locked and says which
 *   variable did it rather than silently ignoring the user.
 */
interface ModelSettingsProps {
  /** Called after a successful save, so the caller can refresh health. */
  onSaved?: (settings: SettingsResponse) => void;
  onClose: () => void;
}

type Mode = 'configured' | 'none';

export function ModelSettings({ onSaved, onClose }: ModelSettingsProps) {
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [test, setTest] = useState<TestConnectionResponse | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const adopt = useCallback((next: SettingsResponse) => {
    setSettings(next);
    setProvider(next.model?.provider ?? '');
    setModel(next.model?.model ?? '');
    setBaseUrl(next.model?.baseUrl ?? '');
    setApiKey('');
  }, []);

  useEffect(() => {
    api
      .getSettings()
      .then(adopt)
      .catch((err: unknown) =>
        setLoadError(err instanceof ApiError ? String(err.detail) : String(err)),
      );
  }, [adopt]);

  const selected: ProviderOut | undefined = settings?.providers.find((p) => p.key === provider);
  const mode: Mode = settings?.model ? 'configured' : 'none';

  /** Save the form as it stands. */
  const saveModel = async () => {
    if (!selected) return;
    setBusy(true);
    setProblems([]);
    setNotice(null);
    try {
      const next = await api.putSettings({
        model: {
          provider: selected.key,
          model: model.trim(),
          baseUrl: selected.requiresBaseUrl ? baseUrl.trim() : null,
          apiKey: apiKey.trim() ? apiKey.trim() : null,
          clearApiKey: false,
        },
      });
      adopt(next);
      setNotice('Saved. New compiles and runs use this model immediately.');
      onSaved?.(next);
    } catch (err) {
      if (err instanceof SettingsValidationError) setProblems(err.problems);
      else setProblems([err instanceof Error ? err.message : String(err)]);
    } finally {
      setBusy(false);
    }
  };

  const removeKey = async () => {
    if (!settings?.model) return;
    setBusy(true);
    setProblems([]);
    try {
      const next = await api.putSettings({
        model: {
          provider: settings.model.provider,
          model: settings.model.model,
          baseUrl: settings.model.baseUrl ?? null,
          clearApiKey: true,
        },
      });
      adopt(next);
      setNotice('Stored key removed.');
      onSaved?.(next);
    } finally {
      setBusy(false);
    }
  };

  const forgetModel = async () => {
    setBusy(true);
    setProblems([]);
    try {
      const next = await api.putSettings({ clearModel: true });
      adopt(next);
      setNotice('Model cleared.');
      onSaved?.(next);
    } finally {
      setBusy(false);
    }
  };

  const setDryRun = async (value: boolean) => {
    setBusy(true);
    setProblems([]);
    try {
      const next = await api.putSettings({ dryRun: value });
      adopt(next);
      onSaved?.(next);
    } catch (err) {
      setProblems([err instanceof Error ? err.message : String(err)]);
    } finally {
      setBusy(false);
    }
  };

  const runTest = async () => {
    if (!selected) return;
    setBusy(true);
    setTest(null);
    try {
      const result = await api.testConnection({
        provider: selected.key,
        model: model.trim(),
        baseUrl: selected.requiresBaseUrl ? baseUrl.trim() : null,
        apiKey: apiKey.trim() ? apiKey.trim() : null,
      });
      setTest(result);
    } catch (err) {
      setTest({
        ok: false,
        detail: err instanceof ApiError ? String(err.detail) : String(err),
        latencyMs: null,
        model: '',
      });
    } finally {
      setBusy(false);
    }
  };

  if (loadError) {
    return (
      <div className="sb-modal-backdrop" role="dialog" aria-modal="true" aria-label="Model settings">
        <div className="sb-modal">
          <h2>Model settings</h2>
          <p className="sb-finding sb-finding-error">Could not load settings: {loadError}</p>
          <button className="sb-btn-ghost" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    );
  }

  if (!settings) {
    return (
      <div className="sb-modal-backdrop" role="dialog" aria-modal="true" aria-label="Model settings">
        <div className="sb-modal">
          <h2>Model settings</h2>
          <p className="sb-hint">Loading…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="sb-modal-backdrop" role="dialog" aria-modal="true" aria-label="Model settings">
      <div className="sb-modal">
        <div className="sb-modal-head">
          <h2>Model settings</h2>
          <button className="sb-btn-ghost sb-btn-sm" onClick={onClose} aria-label="Close">
            Close
          </button>
        </div>

        <p className="sb-hint">
          Swarm Builder uses the model you choose here for describing, compiling and running
          workflows. Your key is stored in <code>{settings.configPath}</code> on this machine and
          never leaves the server.
        </p>

        {settings.configError && (
          <div className="sb-finding sb-finding-error">
            Your saved model settings could not be read ({settings.configError}). Saving below
            replaces the file.
          </div>
        )}

        <section className="sb-settings-section">
          <h3>Provider</h3>
          <select
            aria-label="Provider"
            value={provider}
            onChange={(e) => {
              const key = e.target.value;
              setProvider(key);
              const spec = settings.providers.find((p) => p.key === key);
              setModel(spec?.defaultModel ?? '');
              setBaseUrl('');
              setApiKey('');
              setTest(null);
              setProblems([]);
            }}
          >
            <option value="">Choose a provider…</option>
            {settings.providers.map((p) => (
              <option key={p.key} value={p.key} disabled={!p.usable}>
                {p.label}
                {p.usable ? '' : ` — ${p.unusableReason}`}
              </option>
            ))}
          </select>
          {selected?.note && <p className="sb-hint">{selected.note}</p>}

          {selected && (
            <>
              <label className="sb-field">
                <span>Model</span>
                <input
                  list="sb-model-suggestions"
                  value={model}
                  placeholder={selected.defaultModel ?? 'model id'}
                  onChange={(e) => setModel(e.target.value)}
                  aria-label="Model id"
                />
              </label>
              <datalist id="sb-model-suggestions">
                {selected.models.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
              <p className="sb-hint">
                Any model id this provider accepts works, even if it is not in the list.
              </p>

              {selected.requiresBaseUrl && (
                <label className="sb-field">
                  <span>Base URL</span>
                  <input
                    value={baseUrl}
                    placeholder="https://host/v1"
                    onChange={(e) => setBaseUrl(e.target.value)}
                    aria-label="Base URL"
                  />
                </label>
              )}

              {selected.requiresApiKey && (
                <label className="sb-field">
                  <span>API key</span>
                  <input
                    type="password"
                    autoComplete="off"
                    value={apiKey}
                    placeholder={
                      settings.model?.hasApiKey && settings.model.provider === selected.key
                        ? `configured (${settings.model.apiKeyHint})`
                        : `paste your ${selected.label} key`
                    }
                    onChange={(e) => setApiKey(e.target.value)}
                    aria-label="API key"
                  />
                </label>
              )}
              {selected.apiKeyEnv && (
                <p className="sb-hint">
                  Leave this empty to use <code>{selected.apiKeyEnv}</code> from the environment
                  that starts the server.
                </p>
              )}
            </>
          )}
        </section>

        <section className="sb-settings-section">
          <h3>Dry run mode</h3>
          <label className="sb-switch">
            <input
              type="checkbox"
              checked={settings.dryRun}
              disabled={busy || settings.dryRunForcedByEnv}
              onChange={(e) => void setDryRun(e.target.checked)}
              aria-label="Dry run mode"
            />
            <span>Build and run offline, with built-in stub models</span>
          </label>
          <p className="sb-hint">
            {settings.dryRunForcedByEnv
              ? `Forced on by the environment: ${settings.dryRunEnvVars.join(', ')}. Unset it to control this here.`
              : 'No API calls and no credentials: generating and compiling use deterministic stubs, and runs use a keyless test model. Output is not real model output.'}
          </p>
        </section>

        {problems.length > 0 && (
          <div className="sb-finding sb-finding-error">
            <ul>
              {problems.map((problem, i) => (
                <li key={i}>{problem}</li>
              ))}
            </ul>
          </div>
        )}

        {test && (
          <div className={`sb-finding ${test.ok ? 'sb-finding-ok' : 'sb-finding-error'}`}>
            {test.ok
              ? `Connection works${test.latencyMs != null ? ` (${test.latencyMs} ms)` : ''}.`
              : `Connection failed: ${test.detail}`}
          </div>
        )}

        {notice && <div className="sb-finding sb-finding-ok">{notice}</div>}

        <div className="sb-action-row">
          <button
            className="sb-btn-primary"
            disabled={busy || !selected || model.trim() === ''}
            onClick={() => void saveModel()}
          >
            {mode === 'configured' ? 'Save' : 'Save model'}
          </button>
          {selected && (
            <button className="sb-btn-ghost" disabled={busy} onClick={() => void runTest()}>
              Test connection
            </button>
          )}
          {settings.model?.hasApiKey && (
            <button className="sb-btn-ghost sb-btn-sm" disabled={busy} onClick={() => void removeKey()}>
              Remove stored key
            </button>
          )}
          {settings.model && (
            <button className="sb-btn-danger sb-btn-sm" disabled={busy} onClick={() => void forgetModel()}>
              Forget model
            </button>
          )}
        </div>

        <footer className="sb-settings-advanced">
          <h4>Advanced</h4>
          <p className="sb-hint">
            {settings.inheritedRoutes > 0
              ? `An existing model configuration on this machine contributes ${settings.inheritedRoutes} route(s); a model saved here overrides it.`
              : 'No external model configuration was found, and none is required.'}
          </p>
          <p className="sb-hint">
            The key is stored on this machine only and is removed from the server's environment if
            you clear it here. An exported project run elsewhere needs that variable set itself —
            its <code>.env.example</code> names it (never the value).
          </p>
          <p className="sb-hint">
            Currently in use: <strong>{settings.resolvedDefault.provider} / {settings.resolvedDefault.model}</strong>{' '}
            — {modelSourceLabel(settings.resolvedDefault.source)}.
          </p>
        </footer>
      </div>
    </div>
  );
}

export default ModelSettings;
