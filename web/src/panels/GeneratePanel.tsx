import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from '../api/client';
import type {
  AttachmentOut,
  ClarifyQuestionOut,
  ClarifyResponse,
  FindingOut,
  GenerateGraphResponse,
  GenerateProgressEvent,
  GenerateStreamUpdate,
  HealthResponse,
  ProblemDetail,
  SwarmGraph,
} from '../api/schema';

const EXAMPLE_DESCRIPTION =
  'Take a support ticket. Classify it as billing or technical. Route billing tickets to a ' +
  'refund-policy agent and technical ones to a web-search agent. Summarize the answer.';

/** Mirrors the server's own accepted set, so the file picker cannot offer
 * something the upload will refuse. The server stays authoritative: this only
 * avoids a pointless round trip. */
const ACCEPTED_FILES = '.csv,.tsv,.xlsx,.pptx,.png,.jpg,.jpeg,.webp,.gif';

/** Mirrors `attachments/limits.py`. Used for local feedback only (a 6th file is
 * blocked here, and the running total is shown) — every cap is enforced
 * server-side as well. */
const MAX_FILES = 5;
const MAX_FILE_BYTES = 10 * 1024 * 1024;
const UPLOAD_TIMEOUT_MS = 60_000;

/** The radio value that reveals the free-text box in the questions step. */
const OTHER_CHOICE = '__other__';

interface GeneratePanelProps {
  /** Replace this graph's document instead of minting a new graph. */
  replaceGraphId?: string;
  /** Name to keep when replacing; ignored for a new graph. */
  keepName?: string;
  onGenerated: (
    graph: SwarmGraph,
    warnings: FindingOut[],
    attempts: number,
    dryRun?: boolean,
  ) => void;
  onCancel?: () => void;
  compact?: boolean;
  /** Bumped by the shell after a settings save, to re-read health. */
  settingsRevision?: number;
}

interface GenerateFailure {
  message: string;
  problems: string[];
  code: string | null;
}

interface Chip {
  /** Stable React key: the local file identity, independent of the server id. */
  key: string;
  name: string;
  bytes: number;
  status: 'reading' | 'ready' | 'error';
  attachment?: AttachmentOut;
  error?: string;
  warning?: string;
}

type Phase = 'idle' | 'analyzing' | 'generating' | 'failed';

/** Which half of the flow is on screen. Deliberately separate from `phase`:
 * pressing "Generate with answers" must leave the answers visible while the
 * draft runs, and a failure must land under whatever the user was looking at. */
type Step = 'composer' | 'questions';

/**
 * Read the failure out of an API error, in whichever shape the server used.
 *
 * Three shapes reach here and all three are legitimate: the structured
 * `{code, message, problems}` the attachment and generate routes return, a plain
 * string (the pre-existing model-setup and upstream failures, unchanged by this
 * feature), and a validation array. Assuming one of them would make the panel
 * throw while rendering an error -- the worst possible place to discover it.
 */
function readFailure(err: unknown): GenerateFailure {
  if (err instanceof ApiError) {
    const detail = err.detail as unknown;
    if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
      const record = detail as Partial<ProblemDetail>;
      const problems = Array.isArray(record.problems)
        ? record.problems.filter((p): p is string => typeof p === 'string')
        : [];
      const message =
        typeof record.message === 'string'
          ? record.message
          : `Generation failed (${err.status}).`;
      return {
        message,
        problems: problems.length > 0 ? problems : [],
        code: typeof record.code === 'string' ? record.code : null,
      };
    }
    if (typeof detail === 'string') {
      return { message: detail, problems: [], code: null };
    }
    if (Array.isArray(detail)) {
      return {
        message: `The request was rejected (${err.status}).`,
        problems: detail.map((item) =>
          item && typeof item === 'object' && 'msg' in item
            ? String((item as { msg: unknown }).msg)
            : String(item),
        ),
        code: null,
      };
    }
    return { message: `Generation failed (${err.status}).`, problems: [], code: null };
  }
  return { message: err instanceof Error ? err.message : String(err), problems: [], code: null };
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatChars(chars: number): string {
  return chars >= 1000 ? `${(chars / 1000).toFixed(1)}k characters` : `${chars} characters`;
}

function formatElapsed(seconds: number): string {
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

const PROGRESS_STEPS = ['Check description', 'Draft', 'Review', 'Save'] as const;

interface GenerateProgressProps {
  phase: 'analyzing' | 'generating';
  update: GenerateStreamUpdate | null;
  /** The most recent rejected draft, kept after the redraft starts. */
  rejection: GenerateProgressEvent | null;
  elapsedSeconds: number;
}

/** Where the describe flow is: a segmented bar (one segment per step, the
 * active one animated), the step names, and a line saying what is happening. */
function GenerateProgress({ phase, update, rejection, elapsedSeconds }: GenerateProgressProps) {
  let active: number;
  let status: string;
  if (phase === 'analyzing') {
    active = 0;
    status = 'Checking whether anything is ambiguous before drafting…';
  } else if (update?.stage === 'saving') {
    active = 3;
    status = 'The draft passed review. Saving the graph…';
  } else if (update?.stage === 'reviewing') {
    active = 2;
    status = `Checking draft ${update.attempt} against the reviewer's rules…`;
  } else {
    active = 1;
    const attempt = update && update.stage !== 'rejected' ? update.attempt : (rejection?.attempt ?? 0) + 1;
    const count = rejection?.problems.length ?? 0;
    status =
      rejection && attempt > 1
        ? `The reviewer found ${count} problem${count === 1 ? '' : 's'} in draft ${rejection.attempt}; ` +
          `the model is redrafting (attempt ${attempt} of ${rejection.maxAttempts})…`
        : 'The model is drafting nodes and edges. This is usually the longest step…';
  }
  const draftLabel =
    update && 'attempt' in update && update.attempt > 1
      ? `Draft ${update.attempt} of ${update.maxAttempts}`
      : PROGRESS_STEPS[1];

  return (
    <div className="sb-generate-progress" role="status" aria-live="polite">
      <ol className="sb-generate-progress-steps">
        {PROGRESS_STEPS.map((label, index) => {
          const state = index < active ? 'done' : index === active ? 'active' : 'pending';
          return (
            <li
              key={label}
              className={`sb-generate-progress-step sb-generate-progress-${state}`}
              aria-current={state === 'active' ? 'step' : undefined}
            >
              <span className="sb-generate-progress-bar" />
              <span className="sb-generate-progress-label">{index === 1 ? draftLabel : label}</span>
            </li>
          );
        })}
      </ol>
      <p className="sb-generate-progress-status">
        <span className="sb-generate-progress-spinner" aria-hidden="true" />
        {status}
        <span className="sb-generate-progress-elapsed">{formatElapsed(elapsedSeconds)}</span>
      </p>
    </div>
  );
}

/**
 * Describe a workflow in prose — optionally with supplementary files — and get a
 * whole graph back (PLAN-V2-FEATURES.md Feature 2, extended by
 * PLAN-ATTACHMENTS-CLARIFY.md).
 *
 * The panel is a small state machine because the flow is genuinely two-phase:
 * an analysis pass decides whether the request is ambiguous, and only then does
 * drafting happen. Both phases are skippable-by-the-user ("Generate anyway") and
 * neither clears what the user typed on failure.
 */
export function GeneratePanel({
  replaceGraphId,
  keepName,
  onGenerated,
  onCancel,
  compact = false,
  settingsRevision = 0,
}: GeneratePanelProps) {
  const [description, setDescription] = useState('');
  const [phase, setPhase] = useState<Phase>('idle');
  const [step, setStep] = useState<Step>('composer');
  const [failure, setFailure] = useState<GenerateFailure | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [chips, setChips] = useState<Chip[]>([]);
  const [dragging, setDragging] = useState(false);
  const [analysis, setAnalysis] = useState<ClarifyResponse | null>(null);
  const [analysisKey, setAnalysisKey] = useState<string | null>(null);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [customAnswers, setCustomAnswers] = useState<Record<string, string>>({});
  const [assumptionsOpen, setAssumptionsOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [doneNotice, setDoneNotice] = useState<string | null>(null);
  /** Notes the server attached to the files a generation used, shown once. */
  const [attachmentWarnings, setAttachmentWarnings] = useState<string[]>([]);
  const [progress, setProgress] = useState<GenerateStreamUpdate | null>(null);
  const [rejection, setRejection] = useState<GenerateProgressEvent | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  const fileInput = useRef<HTMLInputElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  /** Server ids this panel has received and not yet handed to a generation, so
   * an unmount can discard them instead of leaving them for the whole TTL. */
  const liveIds = useRef<Set<string>>(new Set());
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      abortRef.current?.abort();
      // Fire and forget: an orphaned upload would otherwise sit in the workspace
      // for six hours and never be used.
      for (const id of liveIds.current) {
        void api.deleteAttachment(id).catch(() => undefined);
      }
      liveIds.current.clear();
    };
  }, []);

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch(() => setHealth(null));
  }, [settingsRevision]);

  const modelConfigured = health ? health.modelConfigured || health.dryRun : true;
  const dryRun = analysis?.dryRun ?? health?.dryRun ?? false;
  const busy = phase === 'analyzing' || phase === 'generating';

  useEffect(() => {
    if (!busy) return undefined;
    const startedAt = Date.now();
    setElapsedSeconds(0);
    const timer = window.setInterval(
      () => setElapsedSeconds(Math.floor((Date.now() - startedAt) / 1000)),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [busy]);
  const readyChips = chips.filter((chip) => chip.status === 'ready');
  const uploading = chips.some((chip) => chip.status === 'reading');
  const canGenerate =
    description.trim().length > 0 && !busy && !uploading && modelConfigured;
  const totalBytes = readyChips.reduce((sum, chip) => sum + chip.bytes, 0);

  /** The model the privacy line names: the most recent server answer wins, so a
   * settings change cannot leave a stale provider on screen. */
  const modelLine = useMemo(() => {
    const model = analysis?.model ?? health?.resolvedModel ?? null;
    return model ? `${model.provider} / ${model.model}` : 'your configured model';
  }, [analysis, health]);

  const revision = useMemo(() => {
    const ids = readyChips
      .map((chip) => chip.attachment?.id ?? '')
      .filter(Boolean)
      .join(',');
    return `${description.trim()}::${ids}`;
  }, [description, readyChips]);

  const uploadFiles = useCallback(
    async (files: File[]) => {
      const accepted: File[] = [];
      const rejected: Chip[] = [];
      const room = MAX_FILES - chips.length;
      for (const file of files) {
        if (accepted.length >= room) {
          rejected.push({
            key: `${file.name}-over`,
            name: file.name,
            bytes: file.size,
            status: 'error',
            error: `at most ${MAX_FILES} files can be attached`,
          });
          continue;
        }
        if (file.size > MAX_FILE_BYTES) {
          rejected.push({
            key: `${file.name}-big`,
            name: file.name,
            bytes: file.size,
            status: 'error',
            error: `larger than ${formatBytes(MAX_FILE_BYTES)}`,
          });
          continue;
        }
        accepted.push(file);
      }
      const queued: Chip[] = [
        ...rejected,
        ...accepted.map((file) => ({
          key: `${file.name}-${file.size}-${file.lastModified}`,
          name: file.name,
          bytes: file.size,
          status: 'reading' as const,
        })),
      ];
      if (queued.length > 0) {
        setChips((current) => [...current, ...queued]);
        setDoneNotice(null);
      }
      await Promise.all(
        accepted.map(async (file) => {
          const key = `${file.name}-${file.size}-${file.lastModified}`;
          const controller = new AbortController();
          const timer = window.setTimeout(() => controller.abort(), UPLOAD_TIMEOUT_MS);
          try {
            const response = await api.uploadAttachment(file, { signal: controller.signal });
            liveIds.current.add(response.attachment.id);
            setChips((current) =>
              current.map((chip) =>
                chip.key === key
                  ? {
                      ...chip,
                      status: 'ready',
                      attachment: response.attachment,
                      warning: response.warnings[0],
                    }
                  : chip,
              ),
            );
          } catch (err) {
            const failureForFile = readFailure(err);
            setChips((current) =>
              current.map((chip) =>
                chip.key === key
                  ? {
                      ...chip,
                      status: 'error',
                      error:
                        controller.signal.aborted
                          ? 'upload timed out'
                          : failureForFile.message,
                    }
                  : chip,
              ),
            );
          } finally {
            window.clearTimeout(timer);
          }
        }),
      );
    },
    [chips.length],
  );

  const removeChip = (chip: Chip) => {
    setChips((current) => current.filter((candidate) => candidate.key !== chip.key));
    const id = chip.attachment?.id;
    if (id) {
      liveIds.current.delete(id);
      void api.deleteAttachment(id).catch(() => undefined);
    }
  };

  const runGenerate = useCallback(
    async (questions: ClarifyQuestionOut[]) => {
      if (replaceGraphId && !window.confirm('Replace the current graph with a generated one? This cannot be undone.')) {
        return;
      }
      setPhase('generating');
      setFailure(null);
      setProgress(null);
      setRejection(null);
      const controller = new AbortController();
      abortRef.current = controller;
      const answers = questions.flatMap((question) => {
        const choice = choices[question.id];
        const answer =
          choice === OTHER_CHOICE ? (customAnswers[question.id] ?? '').trim() : (choice ?? '');
        if (!answer) return [];
        return [{ questionId: question.id, question: question.question, answer }];
      });
      try {
        const response: GenerateGraphResponse = await api.generateGraphStream(
          {
            description: description.trim(),
            graphId: replaceGraphId ?? null,
            name: replaceGraphId ? (keepName ?? null) : null,
            modelOverride: null,
            attachmentIds: readyChips
              .map((chip) => chip.attachment?.id)
              .filter((id): id is string => Boolean(id)),
            answers,
          },
          {
            signal: controller.signal,
            onUpdate: (update) => {
              setProgress(update);
              if (update.stage === 'rejected') setRejection(update);
            },
          },
        );
        liveIds.current.clear();
        const used = response.attachments ?? [];
        const fileCount = used.length;
        setDoneNotice(
          fileCount > 0
            ? `Drafted from your description and ${fileCount} file${fileCount === 1 ? '' : 's'}.`
            : 'Drafted from your description.',
        );
        setAttachmentWarnings(
          used.flatMap((item) => item.notes.map((note) => `${item.filename}: ${note}`)),
        );
        setDescription('');
        setChips([]);
        setAnalysis(null);
        setAnalysisKey(null);
        setChoices({});
        setCustomAnswers({});
        setStep('composer');
        setPhase('idle');
        onGenerated(response.graph, response.warnings, response.attempts, response.dryRun);
      } catch (err) {
        setFailure(readFailure(err));
        setPhase('failed');
      }
    },
    [
      choices,
      customAnswers,
      description,
      keepName,
      onGenerated,
      readyChips,
      replaceGraphId,
    ],
  );

  const handleGenerate = useCallback(async () => {
    setDoneNotice(null);
    setAttachmentWarnings([]);
    // Analyze once per (description, attachment set) revision: going back to the
    // description and clicking again without editing must not spend another call.
    if (analysis && analysisKey === revision) {
      if (analysis.needsClarification) {
        setNotice('Questions already asked for this description.');
        setStep('questions');
      } else {
        await runGenerate([]);
      }
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setPhase('analyzing');
    setFailure(null);
    setNotice(null);
    let response: ClarifyResponse;
    try {
      response = await api.clarifyGraph(
        {
          description: description.trim(),
          attachmentIds: readyChips
            .map((chip) => chip.attachment?.id)
            .filter((id): id is string => Boolean(id)),
        },
        { signal: controller.signal },
      );
    } catch (err) {
      const analyzeFailure = readFailure(err);
      if (err instanceof ApiError && err.status === 422) {
        setFailure(analyzeFailure);
        setPhase('failed');
        if (analyzeFailure.code === 'attachment_expired') {
          // The ids come back in `problems`: drop those chips so the next click
          // sends a request the server can accept.
          const expired = new Set(
            analyzeFailure.problems
              .map((problem) => /'([0-9a-f]{32})'/.exec(problem)?.[1])
              .filter((id): id is string => Boolean(id)),
          );
          if (expired.size > 0) {
            setChips((current) =>
              current.filter((chip) => !expired.has(chip.attachment?.id ?? '')),
            );
          }
        }
        return;
      }
      // Anything else (502 from the provider, a network failure): the draft can
      // still be attempted, so say what happened and carry on.
      setNotice(`Could not ask clarifying questions: ${analyzeFailure.message}`);
      setAnalysis(null);
      setAnalysisKey(null);
      await runGenerate([]);
      return;
    }
    setAnalysis(response);
    setAnalysisKey(revision);
    if (response.needsClarification) {
      const preselected: Record<string, string> = {};
      for (const question of response.questions) {
        preselected[question.id] = question.recommended;
      }
      setChoices(preselected);
      setCustomAnswers({});
      setAssumptionsOpen(false);
      setStep('questions');
      setPhase('idle');
      return;
    }
    await runGenerate([]);
  }, [analysis, analysisKey, description, readyChips, revision, runGenerate]);

  const questions = analysis?.questions ?? [];

  return (
    <div
      className={`sb-generate${compact ? ' sb-generate-compact' : ''}${
        dragging ? ' sb-generate-dragging' : ''
      }`}
      onDragOver={(event) => {
        if (busy) return;
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => {
        if (busy) return;
        event.preventDefault();
        setDragging(false);
        const dropped = Array.from(event.dataTransfer?.files ?? []);
        if (dropped.length > 0) void uploadFiles(dropped);
      }}
    >
      {!compact && <h2 className="sb-generate-title">Describe a workflow</h2>}
      <p className="sb-generate-hint">
        {replaceGraphId
          ? 'Describe the workflow you want instead; the current canvas will be replaced.'
          : 'Say what should happen, step by step. You get a draft graph to review and edit.'}
      </p>

      {step === 'questions' ? (
        <section className="sb-questions" aria-label="Clarifying questions">
          {analysis?.understanding && (
            <p className="sb-questions-understanding">{analysis.understanding}</p>
          )}
          {analysis && analysis.assumptions.length > 0 && (
            <div className="sb-questions-assumptions">
              <button
                type="button"
                className="sb-btn-ghost sb-btn-sm"
                onClick={() => setAssumptionsOpen((open) => !open)}
                aria-expanded={assumptionsOpen}
              >
                {assumptionsOpen ? 'Hide' : 'Show'} what the draft will assume (
                {analysis.assumptions.length})
              </button>
              {assumptionsOpen && (
                <ul>
                  {analysis.assumptions.map((assumption) => (
                    <li key={assumption}>{assumption}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
          {questions.map((question) => (
            <fieldset className="sb-question" key={question.id}>
              <legend>{question.question}</legend>
              <p className="sb-question-why">{question.why}</p>
              {question.options.map((option) => (
                <label className="sb-question-option" key={option.label}>
                  <input
                    type="radio"
                    name={`sb-question-${question.id}`}
                    value={option.label}
                    checked={choices[question.id] === option.label}
                    onChange={() =>
                      setChoices((current) => ({ ...current, [question.id]: option.label }))
                    }
                  />
                  <span>
                    {option.label}
                    {option.description ? (
                      <em className="sb-question-option-note"> — {option.description}</em>
                    ) : null}
                  </span>
                </label>
              ))}
              <label className="sb-question-option">
                <input
                  type="radio"
                  name={`sb-question-${question.id}`}
                  value={OTHER_CHOICE}
                  checked={choices[question.id] === OTHER_CHOICE}
                  onChange={() =>
                    setChoices((current) => ({ ...current, [question.id]: OTHER_CHOICE }))
                  }
                />
                <span>Other…</span>
              </label>
              {choices[question.id] === OTHER_CHOICE && (
                <input
                  type="text"
                  className="sb-question-free"
                  placeholder="Type your answer"
                  aria-label={`Answer for: ${question.question}`}
                  value={customAnswers[question.id] ?? ''}
                  onChange={(event) =>
                    setCustomAnswers((current) => ({
                      ...current,
                      [question.id]: event.target.value,
                    }))
                  }
                />
              )}
            </fieldset>
          ))}
          <div className="sb-action-row">
            <button
              className="sb-btn-primary"
              disabled={busy}
              onClick={() => void runGenerate(questions)}
            >
              {phase === 'generating' ? 'Generating…' : 'Generate with answers'}
            </button>
            <button
              type="button"
              className="sb-btn-ghost sb-btn-sm"
              disabled={busy}
              onClick={() => void runGenerate([])}
            >
              Generate anyway
            </button>
            <button
              type="button"
              className="sb-btn-ghost sb-btn-sm"
              disabled={busy}
              onClick={() => {
                setStep('composer');
                setPhase('idle');
                setFailure(null);
              }}
            >
              Back to description
            </button>
          </div>
        </section>
      ) : (
        <>
          <textarea
            className="sb-generate-input"
            value={description}
            placeholder={EXAMPLE_DESCRIPTION}
            onChange={(event) => setDescription(event.target.value)}
            disabled={busy}
            rows={compact ? 4 : 5}
            aria-label="Workflow description"
          />
          <div className="sb-attach-row">
            <input
              ref={fileInput}
              type="file"
              multiple
              accept={ACCEPTED_FILES}
              className="sb-attach-input"
              aria-label="Attach supplementary files"
              onChange={(event) => {
                const files = Array.from(event.target.files ?? []);
                event.target.value = '';
                if (files.length > 0) void uploadFiles(files);
              }}
            />
            <button
              type="button"
              className="sb-btn-ghost sb-btn-sm"
              disabled={busy || chips.length >= MAX_FILES}
              onClick={() => fileInput.current?.click()}
            >
              Attach files
            </button>
            <span className="sb-hint">
              Excel (.xlsx), PowerPoint (.pptx), CSV, or images — drop them here.
              {readyChips.length > 0 && ` ${readyChips.length}/${MAX_FILES} files, ${formatBytes(totalBytes)}.`}
            </span>
          </div>
          {chips.length > 0 && (
            <ul className="sb-attach-chips">
              {chips.map((chip) => (
                <li
                  className={`sb-attach-chip sb-attach-${chip.status}`}
                  key={chip.key}
                  title={[
                    chip.attachment?.notes.join('; '),
                    chip.warning,
                    chip.error,
                  ]
                    .filter(Boolean)
                    .join(' — ')}
                >
                  <span className="sb-attach-chip-name">{chip.name}</span>
                  <span className="sb-attach-chip-meta">
                    {chip.status === 'reading' && 'reading…'}
                    {chip.status === 'error' && (chip.error ?? 'could not be read')}
                    {chip.status === 'ready' && chip.attachment && (
                      <>
                        {chip.attachment.kind === 'image'
                          ? 'image'
                          : formatChars(chip.attachment.chars)}
                        {chip.attachment.truncated && (
                          <span className="sb-attach-chip-truncated">truncated</span>
                        )}
                      </>
                    )}
                  </span>
                  {chip.status === 'ready' && chip.attachment?.preview && (
                    <details className="sb-attach-chip-preview">
                      <summary>What we read</summary>
                      <pre>{chip.attachment.preview}</pre>
                    </details>
                  )}
                  {chip.status === 'ready' && chip.warning && (
                    <span className="sb-attach-chip-warning">{chip.warning}</span>
                  )}
                  <button
                    type="button"
                    className="sb-btn-ghost sb-btn-sm"
                    aria-label={`Remove ${chip.name}`}
                    disabled={busy}
                    onClick={() => removeChip(chip)}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}
          {/* Always visible, not only once a file is attached: the point of a
              disclosure is to inform the decision to attach one. */}
          <p className="sb-attach-privacy">
            {dryRun
              ? 'Dry run is on: files are read locally and no model sees them.'
              : `Files you attach are sent to ${modelLine} as context.`}
          </p>
          <div className="sb-action-row">
            <button
              className="sb-btn-primary"
              disabled={!canGenerate}
              onClick={() => void handleGenerate()}
            >
              {phase === 'analyzing' && 'Checking the description…'}
              {phase === 'generating' && 'Generating…'}
              {phase !== 'analyzing' &&
                phase !== 'generating' &&
                (replaceGraphId ? 'Regenerate graph' : 'Generate graph')}
            </button>
            {!description && !busy && (
              <button
                type="button"
                className="sb-btn-ghost sb-btn-sm"
                onClick={() => setDescription(EXAMPLE_DESCRIPTION)}
              >
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
                {dryRun
                  ? 'Dry run: drafts come from the built-in stub, not a model.'
                  : modelConfigured
                    ? `${health.resolvedModel.provider} / ${health.resolvedModel.model}`
                    : 'No model configured yet.'}
              </span>
            )}
          </div>
        </>
      )}

      {uploading && (
        <div className="sb-hint" aria-live="polite">
          Reading the attached files…
        </div>
      )}
      {(phase === 'analyzing' || phase === 'generating') && (
        <GenerateProgress
          phase={phase}
          update={progress}
          rejection={rejection}
          elapsedSeconds={elapsedSeconds}
        />
      )}
      {notice && <div className="sb-hint">{notice}</div>}
      {doneNotice && (
        <div className="sb-generated-notice-inline" role="status">
          {doneNotice}
          {attachmentWarnings.length > 0 && (
            <ul>
              {attachmentWarnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {failure && (
        <div className="sb-finding sb-finding-error" role="alert">
          <strong>{failure.message}</strong>
          {failure.problems.length > 0 && (
            <ul className="sb-generate-problems">
              {failure.problems.map((problem, i) => (
                <li key={i}>{problem}</li>
              ))}
            </ul>
          )}
          {failure.code === 'image_input_unsupported' && (
            <p className="sb-generate-hint">
              Remove the image below (or pick a vision-capable model in Model settings), then
              try again.
            </p>
          )}
          <div className="sb-action-row">
            <button
              type="button"
              className="sb-btn-sm"
              onClick={() => {
                setFailure(null);
                setPhase('idle');
              }}
            >
              Try again
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default GeneratePanel;
