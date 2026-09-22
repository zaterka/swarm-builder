import { useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { FindingOut, GenerateGraphResponse, HealthResponse, SwarmGraph } from '../api/schema';

const EXAMPLE_DESCRIPTION =
  'Take a support ticket. Classify it as billing or technical. Route billing tickets to a ' +
  'refund-policy agent and technical ones to a web-search agent. Summarize the answer.';

interface GeneratePanelProps {
  /** Replace this graph's document instead of minting a new graph. */
  replaceGraphId?: string;
  /** Name to keep when replacing; ignored for a new graph. */
  keepName?: string;
  onGenerated: (graph: SwarmGraph, warnings: FindingOut[], attempts: number) => void;
  onCancel?: () => void;
  compact?: boolean;
}

interface GenerateFailure {
  message: string;
  problems: string[];
}

function readFailure(err: unknown): GenerateFailure {
  if (err instanceof ApiError) {
    const detail = err.detail;
    if (detail && typeof detail === 'object' && 'problems' in detail) {
      const record = detail as { message?: unknown; problems?: unknown };
      return {
        message: typeof record.message === 'string' ? record.message : 'Generation failed.',
        problems: Array.isArray(record.problems)
          ? record.problems.filter((p): p is string => typeof p === 'string')
          : [],
      };
    }
    return { message: typeof detail === 'string' ? detail : `Generation failed (${err.status}).`, problems: [] };
  }
  return { message: err instanceof Error ? err.message : String(err), problems: [] };
}

/**
 * Describe a workflow in prose and get a whole graph back
 * (PLAN-V2-FEATURES.md, Feature 2). The server materializes and reviews the
 * model's draft; this panel only collects the text, shows progress, and
 * hands the saved document to the caller.
 */
export function GeneratePanel({ replaceGraphId, keepName, onGenerated, onCancel, compact = false }: GeneratePanelProps) {
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<GenerateFailure | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  const modelConfigured = health ? health.resolvedModel.source !== 'bundle-default' : true;
  const canGenerate = description.trim().length > 0 && !busy && modelConfigured;

  const handleGenerate = async () => {
    if (replaceGraphId && !window.confirm('Replace the current graph with a generated one? This cannot be undone.')) {
      return;
    }
    setBusy(true);
    setFailure(null);
    try {
      const response: GenerateGraphResponse = await api.generateGraph({
        description: description.trim(),
        graphId: replaceGraphId ?? null,
        name: replaceGraphId ? keepName ?? null : null,
        modelOverride: null,
      });
      onGenerated(response.graph, response.warnings, response.attempts);
      setDescription('');
    } catch (err) {
      setFailure(readFailure(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`sb-generate${compact ? ' sb-generate-compact' : ''}`}>
      {!compact && <h2 className="sb-generate-title">Describe a workflow</h2>}
      <p className="sb-generate-hint">
        {replaceGraphId
          ? 'Describe the workflow you want instead; the current canvas will be replaced.'
          : 'Say what should happen, step by step. You get a draft graph to review and edit.'}
      </p>
      <textarea
        className="sb-generate-input"
        value={description}
        placeholder={EXAMPLE_DESCRIPTION}
        onChange={(e) => setDescription(e.target.value)}
        disabled={busy}
        rows={compact ? 4 : 5}
        aria-label="Workflow description"
      />
      <div className="sb-action-row">
        <button className="sb-btn-primary" disabled={!canGenerate} onClick={() => void handleGenerate()}>
          {busy ? 'Generating…' : replaceGraphId ? 'Regenerate graph' : 'Generate graph'}
        </button>
        {!description && !busy && (
          <button type="button" className="sb-btn-ghost sb-btn-sm" onClick={() => setDescription(EXAMPLE_DESCRIPTION)}>
            Use the example
          </button>
        )}
        {onCancel && (
          <button type="button" className="sb-btn-ghost sb-btn-sm" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
        )}
        {health && (
          <span className="sb-hint">
            {modelConfigured
              ? `${health.resolvedModel.provider} / ${health.resolvedModel.model}`
              : 'No model route configured — set agent-default-model or SWARM_MODEL.'}
          </span>
        )}
      </div>
      {busy && (
        <div className="sb-hint" aria-live="polite">
          Drafting nodes and edges, then checking the result with the reviewer… this usually takes
          10–40 seconds.
        </div>
      )}
      {failure && (
        <div className="sb-finding sb-finding-error">
          <strong>{failure.message}</strong>
          {failure.problems.length > 0 && (
            <ul className="sb-generate-problems">
              {failure.problems.map((problem, i) => (
                <li key={i}>{problem}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

export default GeneratePanel;
