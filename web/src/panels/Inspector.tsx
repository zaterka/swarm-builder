import { useEffect, useState } from 'react';
import { useGraphStore } from '../state/graphStore';
import { inferTemplate } from '../infer';
import {
  DATABASE_INPUT_TYPES,
  DATABASE_OUTPUT_TYPES,
  NOSQL_OPERATIONS,
  PORT_TYPES,
  REDUCER_IDS,
  TEMPLATE_IDS,
  isDatabaseKind,
  isWriteDatabaseNode,
  type AnyDatabaseStarter,
  type DatabaseKind,
  type NosqlOperation,
  type NormalizedNosqlSpec,
  type NormalizedSqlSpec,
  type NormalizedSwarmNode,
  type NormalizedVectorDocument,
  type NormalizedVectorSpec,
  type PortType,
  type ReducerId,
  type TemplateId,
} from '../api/schema';
import { api, TemplatesUnavailableError } from '../api/client';
import type { TemplateEntryOut } from '../api/schema';
import { KIND_LABELS } from './Palette';
import StateFieldsPanel from './StateFieldsPanel';
import { formatRunValue } from '../api/runWire';

/**
 * Per-node editor (PLAN.md "Frontend" -> "Inspector"). Shows the
 * StateFieldsPanel (as a pinned tab, GROUP6_PLAN.md review finding C4)
 * when no node is selected, or when a node IS selected, so switching
 * between the two never requires deselecting.
 */
export function Inspector() {
  const graph = useGraphStore((s) => s.graph);
  const selectedNodeId = useGraphStore((s) => s.selectedNodeId);
  const updateNode = useGraphStore((s) => s.updateNode);
  const setEntryNodeId = useGraphStore((s) => s.setEntryNodeId);
  const setExitNodeId = useGraphStore((s) => s.setExitNodeId);
  const addDecisionBranch = useGraphStore((s) => s.addDecisionBranch);
  const removeDecisionBranch = useGraphStore((s) => s.removeDecisionBranch);
  const setDelegatesTo = useGraphStore((s) => s.setDelegatesTo);
  const runNodes = useGraphStore((s) => s.run.nodes);
  const databaseStarters = useGraphStore((s) => s.databaseStarters);
  const databaseStartersStatus = useGraphStore((s) => s.databaseStartersStatus);
  const databaseStartersError = useGraphStore((s) => s.databaseStartersError);
  const retryDatabaseStarters = useGraphStore((s) => s.retryDatabaseStarters);

  const [showStateFields, setShowStateFields] = useState(false);
  const [templates, setTemplates] = useState<TemplateEntryOut[] | null>(null);
  const [templatesUnavailable, setTemplatesUnavailable] = useState(false);
  // "instructions follows intent until diverged" is UI-only state, never
  // written to the wire document (every SwarmBaseModel has
  // extra="forbid" -- there is no schema field for this flag). Tracked
  // per node id, client-side only, reset when a different node is
  // selected via the `key` prop on the node editor further down.
  const [divergedNodeIds, setDivergedNodeIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    api
      .listTemplates()
      .then(setTemplates)
      .catch((err) => {
        if (err instanceof TemplatesUnavailableError) {
          setTemplatesUnavailable(true);
        }
      });
  }, []);

  if (!graph) {
    return <div className="sb-inspector-empty">No graph loaded.</div>;
  }

  const node = selectedNodeId ? graph.nodes.find((n) => n.id === selectedNodeId) : null;

  const tabs = (
    <div className="sb-inspector-tabs">
      <button
        className={!showStateFields ? 'sb-tab-active' : ''}
        onClick={() => setShowStateFields(false)}
        disabled={!node}
      >
        Node
      </button>
      <button className={showStateFields ? 'sb-tab-active' : ''} onClick={() => setShowStateFields(true)}>
        State fields
      </button>
    </div>
  );

  if (showStateFields || !node) {
    return (
      <div className="sb-inspector">
        {tabs}
        <StateFieldsPanel />
      </div>
    );
  }

  const inference = inferTemplate(node.intent);
  const otherAgentNodes = graph.nodes.filter((n) => n.kind === 'agent' && n.id !== node.id);
  const otherNodesForBranchTarget = graph.nodes.filter((n) => n.id !== node.id);

  const databaseKind: DatabaseKind | null = isDatabaseKind(node.kind) ? node.kind : null;
  const starter: AnyDatabaseStarter | null = databaseKind
    ? (databaseStarters?.[databaseKind] ?? null)
    : null;

  // Every database node in this graph that can be an agent tool: read-only,
  // and not already gone. A write-mode node is named in a note instead of
  // being offered, because the compiler rejects `db_write_as_tool` -- offering
  // it would be offering an agent a handle that turns the graph unbuildable.
  const readOnlyDatabaseNodes = graph.nodes.filter(
    (n) => isDatabaseKind(n.kind) && !isWriteDatabaseNode(n),
  );
  const writeDatabaseNodes = graph.nodes.filter(
    (n) => isDatabaseKind(n.kind) && isWriteDatabaseNode(n),
  );

  return (
    <div className="sb-inspector">
      {tabs}
      <div className="sb-inspector-node">
        <label>
          Title
          <input value={node.title} onChange={(e) => updateNode(node.id, { title: e.target.value })} />
        </label>
        <div className="sb-node-id-hint">
          id: <code>{node.id}</code> (assigned once at creation; renaming the title never changes it)
        </div>

        {runNodes[node.id] && <LastRunSection trace={runNodes[node.id]!} />}

        <div className="sb-entry-exit-controls">
          <label>
            <input
              type="checkbox"
              checked={graph.entryNodeId === node.id}
              onChange={() => setEntryNodeId(node.id)}
            />
            Entry node
          </label>
          <label>
            <input
              type="checkbox"
              checked={graph.exitNodeId === node.id}
              onChange={() => setExitNodeId(node.id)}
            />
            Exit node
          </label>
        </div>

        <label>
          Intent (plain English — the primary field)
          <textarea
            value={node.intent}
            onChange={(e) => {
              const intent = e.target.value;
              const patch: Record<string, unknown> = { intent };
              // Mirror intent into agent.instructions unless the user
              // has already diverged it manually (review finding A12).
              if (node.kind === 'agent' && node.agent && !divergedNodeIds.has(node.id)) {
                patch.agent = { ...node.agent, instructions: intent };
              }
              updateNode(node.id, patch);
            }}
          />
        </label>

        <label>
          Input type
          <select
            value={node.io.inputType}
            onChange={(e) => updateNode(node.id, { io: { ...node.io, inputType: e.target.value as PortType } })}
          >
            {portTypeOptions(
              databaseKind ? DATABASE_INPUT_TYPES[databaseKind] : PORT_TYPES,
              node.io.inputType,
            ).map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label>
          Output type
          <select
            value={node.io.outputType}
            onChange={(e) => updateNode(node.id, { io: { ...node.io, outputType: e.target.value as PortType } })}
          >
            {portTypeOptions(databaseKind ? DATABASE_OUTPUT_TYPES : PORT_TYPES, node.io.outputType).map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        {databaseKind && (
          <div className="sb-hint">
            A database node is fixed by its kind: it binds the incoming value into its one declared
            operation and returns rows, so the pair is{' '}
            <code>{DATABASE_INPUT_TYPES[databaseKind].join(' | ')}</code> →{' '}
            <code>list[json]</code>. A <code>list</code> input is rejected by Phase 1.
          </div>
        )}

        <fieldset>
          <legend>Reads</legend>
          <StateFieldMultiSelect
            all={graph.stateFields}
            selected={node.reads}
            onChange={(reads) => updateNode(node.id, { reads })}
          />
        </fieldset>
        <fieldset>
          <legend>Writes</legend>
          <StateFieldMultiSelect
            all={graph.stateFields}
            selected={node.writes}
            onChange={(writes) => updateNode(node.id, { writes })}
          />
        </fieldset>

        {node.kind === 'agent' && node.agent && (
          <AgentFields
            key={node.id}
            agent={node.agent}
            intent={node.intent}
            template={node.template}
            inferredSuggestion={inference.suggestion}
            matchedKeywords={inference.matchedKeywords}
            templates={templates}
            templatesUnavailable={templatesUnavailable}
            otherAgentNodeOptions={otherAgentNodes.map((n) => ({ id: n.id, title: n.title }))}
            databaseToolOptions={readOnlyDatabaseNodes.map((n) => ({
              entry: `${n.kind}:${n.id}`,
              label: `${n.title} — read-only repository tool`,
            }))}
            writeDatabaseNodeTitles={writeDatabaseNodes.map((n) => n.title)}
            onTemplateChange={(t) => updateNode(node.id, { template: t })}
            onAgentChange={(agentPatch) => updateNode(node.id, { agent: { ...node.agent!, ...agentPatch } })}
            onDelegatesToChange={(ids) => setDelegatesTo(node.id, ids)}
            diverged={divergedNodeIds.has(node.id)}
            onDiverge={() => setDivergedNodeIds((prev) => new Set(prev).add(node.id))}
          />
        )}

        {node.kind === 'programmatic' && node.programmatic && (
          <ProgrammaticFields
            spec={node.programmatic}
            onChange={(patch) => updateNode(node.id, { programmatic: { ...node.programmatic!, ...patch } })}
          />
        )}

        {node.kind === 'decision' && node.decision && (
          <DecisionFields
            decisionNodeId={node.id}
            note={node.decision.note}
            branches={node.decision.branches ?? []}
            targetOptions={otherNodesForBranchTarget.map((n) => ({ id: n.id, title: n.title }))}
            onNoteChange={(note) => updateNode(node.id, { decision: { ...node.decision!, note } })}
            onAddBranch={(match, targetNodeId) => addDecisionBranch(node.id, match, targetNodeId)}
            onRemoveBranch={(match) => removeDecisionBranch(node.id, match)}
          />
        )}

        {node.kind === 'join' && node.join && (
          <JoinFields
            spec={node.join}
            onChange={(patch) => updateNode(node.id, { join: { ...node.join!, ...patch } })}
          />
        )}

        {/* Database fields exist for database kinds only -- and, on the other
            side of the same rule, the agent fields above are gated on
            `kind === 'agent'`, so a database node can never reach the
            Template select (templates are agent-only: `db_template_set`). */}
        {databaseKind && (
          <DatabaseFields
            key={node.id}
            node={node}
            starter={starter}
            startersStatus={databaseStartersStatus}
            startersError={databaseStartersError}
            onRetryStarters={() => void retryDatabaseStarters()}
            onResetToStarter={() => {
              if (!starter) return;
              updateNode(node.id, starterNodePatch(starter));
            }}
            onChange={(patch) => updateNode(node.id, patch)}
          />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Database nodes
// ---------------------------------------------------------------------------

/** The offered port types, plus the document's current value when it is
 * outside that set (a hand-edited or older document) -- so the select shows
 * what the document really declares instead of silently displaying its first
 * option as if it were selected. */
function portTypeOptions(allowed: readonly PortType[], current: PortType): readonly PortType[] {
  return allowed.includes(current) ? allowed : [...allowed, current];
}

/** The store patch that restores a node to its kind's starter: the spec **and**
 * the ports, because a database node's I/O pair is part of what the starter
 * declares. Restoring both is what makes "Reset to example" leave a node that
 * compiles, which one without the other would not. */
function starterNodePatch(starter: AnyDatabaseStarter): Partial<NormalizedSwarmNode> {
  switch (starter.kind) {
    case 'sql':
      return { sql: starter.spec, io: starter.io };
    case 'nosql':
      return { nosql: starter.spec, io: starter.io };
    case 'vector':
      return { vector: starter.spec, io: starter.io };
  }
}

function DatabaseFields(props: {
  node: NormalizedSwarmNode;
  starter: AnyDatabaseStarter | null;
  startersStatus: 'idle' | 'loading' | 'ready' | 'unavailable';
  startersError: string | null;
  onRetryStarters: () => void;
  onResetToStarter: () => void;
  onChange: (patch: Partial<NormalizedSwarmNode>) => void;
}) {
  const kind = props.node.kind as DatabaseKind;
  // The per-kind body's text/JSON drafts are local state (a half-typed JSON
  // literal must never reach the store), so restoring the starter has to
  // remount them rather than only change their props.
  const [resetRevision, setResetRevision] = useState(0);

  const reset = () => {
    setResetRevision((n) => n + 1);
    props.onResetToStarter();
  };

  return (
    <>
      <div className="sb-db-starter">
        <div>
          <strong>{KIND_LABELS[kind]}</strong>
          {props.starter && (
            <div className="sb-hint">
              Example: {props.starter.label} — {props.starter.description}
            </div>
          )}
        </div>
        <button
          type="button"
          className="sb-btn-sm"
          onClick={reset}
          disabled={!props.starter}
          title={
            props.starter
              ? "Replace this node's operation, example data and ports with the example"
              : 'The database starter catalog is unavailable'
          }
        >
          Reset to example
        </button>
      </div>
      {!props.starter && (
        <div className="sb-hint">
          {props.startersStatus === 'loading'
            ? 'Loading the database starter catalog…'
            : 'The database starter catalog is unavailable, so the example and its ports cannot be restored.'}
          {props.startersError ? ` (${props.startersError})` : ''}{' '}
          {props.startersStatus !== 'loading' && (
            <button type="button" className="sb-btn-sm" onClick={props.onRetryStarters}>
              Retry
            </button>
          )}
        </div>
      )}

      {props.starter && props.starter.envVars.length > 0 && (
        // Everything here comes from the catalog entry: no environment variable
        // name and no extra name is written down in this bundle, so the two can
        // never disagree with the project the server actually generates.
        <div className="sb-hint">
          The mock runs with no configuration. To use a real database, switch the generated project
          to live mode and set {props.starter.envVars.map((name) => <code key={name}>{name}</code>)}
          {' '}— install the driver with <code>uv sync --extra {props.starter.liveExtra}</code>.
        </div>
      )}

      {kind === 'sql' &&
        (props.node.sql ? (
          <SqlFields
            key={resetRevision}
            spec={props.node.sql}
            onChange={(sql) => props.onChange({ sql })}
          />
        ) : (
          <MissingSpec kind={kind} />
        ))}

      {kind === 'nosql' &&
        (props.node.nosql ? (
          <NosqlFields
            key={resetRevision}
            spec={props.node.nosql}
            onChange={(nosql) => props.onChange({ nosql })}
          />
        ) : (
          <MissingSpec kind={kind} />
        ))}

      {kind === 'vector' &&
        (props.node.vector ? (
          <VectorFields
            key={resetRevision}
            spec={props.node.vector}
            onChange={(vector) => props.onChange({ vector })}
          />
        ) : (
          <MissingSpec kind={kind} />
        ))}

      <label>
        Note (free text; emitted as a comment above the operation)
        <textarea
          aria-label="database note"
          value={(props.node.sql ?? props.node.nosql ?? props.node.vector)?.note ?? ''}
          onChange={(e) => props.onChange(notePatch(props.node, e.target.value || null))}
        />
      </label>
    </>
  );
}

/** `note` is the one database spec field all three kinds share, so its patch
 * has to name the node's own spec key. */
function notePatch(
  node: NormalizedSwarmNode,
  note: string | null,
): Partial<NormalizedSwarmNode> {
  switch (node.kind) {
    case 'sql':
      return node.sql ? { sql: { ...node.sql, note } } : {};
    case 'nosql':
      return node.nosql ? { nosql: { ...node.nosql, note } } : {};
    case 'vector':
      return node.vector ? { vector: { ...node.vector, note } } : {};
    default:
      return {};
  }
}

function MissingSpec(props: { kind: DatabaseKind }) {
  return (
    <div className="sb-hint" role="note">
      This node declares no {props.kind} spec yet, so Phase 1 reports{' '}
      <code>db_empty_operation</code> naming it. Use “Reset to example” to fill it
      in from this kind&apos;s starter.
    </div>
  );
}

function SqlFields(props: { spec: NormalizedSqlSpec; onChange: (spec: NormalizedSqlSpec) => void }) {
  const { spec, onChange } = props;
  return (
    <>
      <label>
        Query (SQL — the one operation this node runs)
        <textarea
          aria-label="sql query"
          rows={4}
          value={spec.query}
          onChange={(e) => onChange({ ...spec, query: e.target.value })}
        />
      </label>
      <div className="sb-hint">
        A <code>str</code> input binds as <code>:input</code>; a <code>json</code> input&apos;s keys
        bind by their own names. At least one placeholder is required.
      </div>

      <label>
        Seed SQL (the mock&apos;s schema and example rows)
        <textarea
          aria-label="sql seed"
          rows={6}
          value={spec.seedSql}
          onChange={(e) => onChange({ ...spec, seedSql: e.target.value })}
        />
      </label>
      <div className="sb-hint">
        Runs once against the in-memory mock before the read-only guard is armed, so it may contain
        DDL. A seed that does not parse is reported by Phase 1.
      </div>

      <label>
        <input
          type="checkbox"
          checked={spec.write}
          onChange={(e) => onChange({ ...spec, write: e.target.checked })}
        />
        Write mode (runs the statement with <code>execute()</code> instead of <code>query()</code>)
      </label>
      {spec.write && (
        <div className="sb-callout" role="note">
          <strong>A write node cannot be used as an agent tool.</strong> An agent can only call a
          read-only repository tool, so this node is not offered in any agent&apos;s Tools list, and
          Phase 1 rejects a graph that lists it.
        </div>
      )}
    </>
  );
}

function NosqlFields(props: {
  spec: NormalizedNosqlSpec;
  onChange: (spec: NormalizedNosqlSpec) => void;
}) {
  const { spec, onChange } = props;
  return (
    <>
      <label>
        Collection
        <input
          aria-label="nosql collection"
          value={spec.collection}
          onChange={(e) => onChange({ ...spec, collection: e.target.value })}
        />
      </label>

      <label>
        Operation
        <select
          value={spec.operation}
          onChange={(e) => onChange({ ...spec, operation: e.target.value as NosqlOperation })}
        >
          {NOSQL_OPERATIONS.map((op) => (
            <option key={op} value={op}>
              {op}
            </option>
          ))}
        </select>
      </label>
      {spec.operation === 'insert_one' && (
        <div className="sb-callout" role="note">
          <strong><code>insert_one</code> is a write.</strong> This node cannot be used as an agent
          tool and is not offered in any agent&apos;s Tools list.
        </div>
      )}

      <JsonField
        label="Filter (JSON object)"
        ariaLabel="nosql filter"
        value={spec.filter}
        parse={parseJsonObject}
        onValidChange={(filter) => onChange({ ...spec, filter })}
      />
      <div className="sb-hint">
        A value of <code>&quot;$input&quot;</code> is replaced with the incoming value; a{' '}
        <code>json</code> input is merged over the filter&apos;s top level.
      </div>

      <NumberField
        label="Limit"
        ariaLabel="nosql limit"
        value={spec.limit}
        integer
        onChange={(limit) => onChange({ ...spec, limit })}
      />

      <JsonField
        label="Seed documents (JSON array)"
        ariaLabel="nosql seed"
        value={spec.seed}
        parse={parseJsonObjectList}
        onValidChange={(seed) => onChange({ ...spec, seed })}
      />
    </>
  );
}

function VectorFields(props: {
  spec: NormalizedVectorSpec;
  onChange: (spec: NormalizedVectorSpec) => void;
}) {
  const { spec, onChange } = props;
  return (
    <>
      <label>
        Collection
        <input
          aria-label="vector collection"
          value={spec.collection}
          onChange={(e) => onChange({ ...spec, collection: e.target.value })}
        />
      </label>

      <NumberField
        label="top_k (how many matches to return)"
        ariaLabel="vector top k"
        value={spec.topK}
        integer
        onChange={(topK) => onChange({ ...spec, topK })}
      />

      <NumberField
        label="min_score (cosine, -1..1; 0 means no threshold)"
        ariaLabel="vector min score"
        value={spec.minScore}
        onChange={(minScore) => onChange({ ...spec, minScore })}
      />

      <JsonField
        label="Seed documents (JSON array of {id, text, metadata})"
        ariaLabel="vector seed"
        value={spec.seed}
        parse={parseVectorDocuments}
        onValidChange={(seed) => onChange({ ...spec, seed })}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Validated text inputs
//
// A JSON textarea cannot write on every keystroke: the graph document has
// `extra="forbid"` and typed fields, so a half-typed literal would either be
// rejected by the server or, worse, be stored as a string where the schema
// wants a list of documents. Each of these keeps the raw text locally, writes
// to the store only when the text parses *and* means the right thing, and shows
// the reason inline when it does not.
// ---------------------------------------------------------------------------

type JsonParseResult<T> = { ok: true; value: T } | { ok: false; error: string };

function parseJson(raw: string): JsonParseResult<unknown> {
  try {
    return { ok: true, value: JSON.parse(raw) };
  } catch (err) {
    return { ok: false, error: `Not valid JSON: ${err instanceof Error ? err.message : String(err)}` };
  }
}

function parseJsonObject(raw: string): JsonParseResult<Record<string, unknown>> {
  const parsed = parseJson(raw);
  if (!parsed.ok) return parsed;
  const value = parsed.value;
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return { ok: false, error: 'Must be a JSON object, e.g. {"status": "$input"}.' };
  }
  return { ok: true, value: value as Record<string, unknown> };
}

function parseJsonObjectList(raw: string): JsonParseResult<Record<string, unknown>[]> {
  const parsed = parseJson(raw);
  if (!parsed.ok) return parsed;
  const value = parsed.value;
  if (!Array.isArray(value)) return { ok: false, error: 'Must be a JSON array of objects.' };
  for (const entry of value) {
    if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) {
      return { ok: false, error: 'Every element must be a JSON object.' };
    }
  }
  return { ok: true, value: value as Record<string, unknown>[] };
}

function parseVectorDocuments(raw: string): JsonParseResult<NormalizedVectorDocument[]> {
  const parsed = parseJsonObjectList(raw);
  if (!parsed.ok) return parsed;
  const documents: NormalizedVectorDocument[] = [];
  for (const entry of parsed.value) {
    if (typeof entry['id'] !== 'string' || typeof entry['text'] !== 'string') {
      return { ok: false, error: 'Every document needs a string "id" and a string "text".' };
    }
    const metadata = entry['metadata'];
    if (metadata !== undefined && (typeof metadata !== 'object' || metadata === null || Array.isArray(metadata))) {
      return { ok: false, error: 'A document\'s "metadata" must be a JSON object.' };
    }
    documents.push({
      id: entry['id'],
      text: entry['text'],
      metadata: (metadata as Record<string, unknown> | undefined) ?? {},
    });
  }
  return { ok: true, value: documents };
}

function JsonField<T>(props: {
  label: string;
  ariaLabel: string;
  value: T;
  parse: (raw: string) => JsonParseResult<T>;
  onValidChange: (value: T) => void;
  rows?: number;
}) {
  const [text, setText] = useState(() => JSON.stringify(props.value, null, 2));
  const [error, setError] = useState<string | null>(null);

  const onChange = (raw: string) => {
    setText(raw);
    const parsed = props.parse(raw);
    if (!parsed.ok) {
      setError(parsed.error);
      return;
    }
    setError(null);
    props.onValidChange(parsed.value);
  };

  return (
    <label>
      {props.label}
      <textarea
        aria-label={props.ariaLabel}
        aria-invalid={error ? true : undefined}
        rows={props.rows ?? 5}
        value={text}
        onChange={(e) => onChange(e.target.value)}
      />
      {error && (
        <span className="sb-field-error" role="alert">
          {error} The graph keeps its previous value.
        </span>
      )}
    </label>
  );
}

function NumberField(props: {
  label: string;
  ariaLabel: string;
  value: number;
  integer?: boolean;
  onChange: (value: number) => void;
}) {
  const [text, setText] = useState(() => String(props.value));
  const [error, setError] = useState<string | null>(null);

  const onChange = (raw: string) => {
    setText(raw);
    const parsed = Number(raw.trim());
    if (raw.trim() === '' || !Number.isFinite(parsed)) {
      setError('Must be a number.');
      return;
    }
    if (props.integer && !Number.isInteger(parsed)) {
      setError('Must be a whole number.');
      return;
    }
    setError(null);
    props.onChange(parsed);
  };

  return (
    <label>
      {props.label}
      <input
        aria-label={props.ariaLabel}
        aria-invalid={error ? true : undefined}
        inputMode="decimal"
        value={text}
        onChange={(e) => onChange(e.target.value)}
      />
      {error && (
        <span className="sb-field-error" role="alert">
          {error} The graph keeps its previous value.
        </span>
      )}
    </label>
  );
}

function StateFieldMultiSelect(props: {
  all: { name: string; type: string }[];
  selected: string[];
  onChange: (names: string[]) => void;
}) {
  const toggle = (name: string) => {
    if (props.selected.includes(name)) {
      props.onChange(props.selected.filter((n) => n !== name));
    } else {
      props.onChange([...props.selected, name]);
    }
  };
  if (props.all.length === 0) {
    return <div className="sb-hint">No state fields declared yet.</div>;
  }
  return (
    <div>
      {props.all.map((field) => (
        <label key={field.name} style={{ display: 'block' }}>
          <input type="checkbox" checked={props.selected.includes(field.name)} onChange={() => toggle(field.name)} />
          {field.name} <span className="sb-hint">({field.type})</span>
        </label>
      ))}
    </div>
  );
}

function AgentFields(props: {
  agent: { instructions: string; tools: string[]; delegatesTo: string[] };
  intent: string;
  template: TemplateId | null | undefined;
  inferredSuggestion: TemplateId;
  matchedKeywords: string[];
  templates: TemplateEntryOut[] | null;
  templatesUnavailable: boolean;
  otherAgentNodeOptions: { id: string; title: string }[];
  /** One entry per read-only database node in the graph. The value written to
   * `agent.tools` is the namespaced `<kind>:<node_id>` form the emitter
   * resolves to a repository tool. */
  databaseToolOptions: { entry: string; label: string }[];
  /** Titles of database nodes that are in write mode, named in a note instead
   * of being offered (Phase 1's `db_write_as_tool`). */
  writeDatabaseNodeTitles: string[];
  onTemplateChange: (t: TemplateId) => void;
  onAgentChange: (patch: Partial<{ instructions: string; tools: string[]; delegatesTo: string[] }>) => void;
  onDelegatesToChange: (ids: string[]) => void;
  diverged: boolean;
  onDiverge: () => void;
}) {
  const templateEntry = props.templates?.find((t) => t.id === props.template);
  const defaultTools = templateEntry?.defaultTools ?? [];

  const toggleTool = (tool: string) => {
    const has = props.agent.tools.includes(tool);
    props.onAgentChange({ tools: has ? props.agent.tools.filter((t) => t !== tool) : [...props.agent.tools, tool] });
  };

  const toggleDelegate = (id: string) => {
    const has = props.agent.delegatesTo.includes(id);
    props.onDelegatesToChange(
      has ? props.agent.delegatesTo.filter((d) => d !== id) : [...props.agent.delegatesTo, id],
    );
  };

  return (
    <>
      <label>
        Template
        <select value={props.template ?? ''} onChange={(e) => props.onTemplateChange(e.target.value as TemplateId)}>
          <option value="">(unset — inferred)</option>
          {TEMPLATE_IDS.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </label>
      <div className="sb-hint">
        Inferred suggestion: <strong>{props.inferredSuggestion}</strong>
        {props.matchedKeywords.length > 0 && ` (matched: ${props.matchedKeywords.join(', ')})`}
      </div>

      <label>
        Instructions
        <textarea
          value={props.agent.instructions}
          onChange={(e) => {
            props.onDiverge();
            props.onAgentChange({ instructions: e.target.value });
          }}
        />
      </label>
      {!props.diverged && (
        <div className="sb-hint">Instructions follow Intent until edited directly.</div>
      )}

      <fieldset>
        <legend>Tools</legend>
        {props.templatesUnavailable && (
          <div className="sb-hint">Template catalog temporarily unavailable — free-form only.</div>
        )}
        {defaultTools.map((tool) => (
          <label key={tool} style={{ display: 'block' }}>
            <input type="checkbox" checked={props.agent.tools.includes(tool)} onChange={() => toggleTool(tool)} />
            {tool}
          </label>
        ))}

        {props.databaseToolOptions.length > 0 && (
          <div className="sb-hint">
            Database nodes in this graph. These entries are <strong>read-only</strong>: the agent can
            only run the node&apos;s declared read operation, so it cannot author a statement.
          </div>
        )}
        {props.databaseToolOptions.map((opt) => (
          <label key={opt.entry} style={{ display: 'block' }}>
            <input
              type="checkbox"
              checked={props.agent.tools.includes(opt.entry)}
              onChange={() => toggleTool(opt.entry)}
            />
            {opt.label} <code>{opt.entry}</code>
          </label>
        ))}
        {props.writeDatabaseNodeTitles.length > 0 && (
          <div className="sb-hint">
            Not offered: {props.writeDatabaseNodeTitles.join(', ')} — a write-mode database node cannot
            be an agent tool.
          </div>
        )}
      </fieldset>

      <fieldset>
        <legend>Delegates to (called as tools, not graph steps)</legend>
        {props.otherAgentNodeOptions.length === 0 && <div className="sb-hint">No other agent nodes yet.</div>}
        {props.otherAgentNodeOptions.map((opt) => (
          <label key={opt.id} style={{ display: 'block' }}>
            <input
              type="checkbox"
              checked={props.agent.delegatesTo.includes(opt.id)}
              onChange={() => toggleDelegate(opt.id)}
            />
            {opt.title}
          </label>
        ))}
      </fieldset>
    </>
  );
}

function ProgrammaticFields(props: {
  spec: { needs: string[]; signatureHint: string | null };
  onChange: (patch: Partial<{ needs: string[]; signatureHint: string | null }>) => void;
}) {
  const [newNeed, setNewNeed] = useState('');
  return (
    <>
      <fieldset>
        <legend>Needs (extra PyPI packages)</legend>
        <ul className="sb-item-list">
          {props.spec.needs.map((need) => (
            <li key={need}>
              <code>{need}</code>
              <button
                className="sb-btn-danger sb-btn-sm"
                onClick={() => props.onChange({ needs: props.spec.needs.filter((n) => n !== need) })}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
        <div className="sb-add-row">
          <input value={newNeed} onChange={(e) => setNewNeed(e.target.value)} placeholder="package-name" />
          <button
            onClick={() => {
              if (!newNeed.trim()) return;
              props.onChange({ needs: [...props.spec.needs, newNeed.trim()] });
              setNewNeed('');
            }}
          >
            Add
          </button>
        </div>
      </fieldset>
      <label>
        Signature hint (free text — read by the fill agent, never executed)
        <textarea
          value={props.spec.signatureHint ?? ''}
          onChange={(e) => props.onChange({ signatureHint: e.target.value || null })}
        />
      </label>
    </>
  );
}

function DecisionFields(props: {
  decisionNodeId: string;
  note: string | null | undefined;
  branches: { match: string; targetNodeId: string }[];
  targetOptions: { id: string; title: string }[];
  onNoteChange: (note: string | null) => void;
  onAddBranch: (match: string, targetNodeId: string) => void;
  onRemoveBranch: (match: string) => void;
}) {
  const [newMatch, setNewMatch] = useState('');
  const [newTarget, setNewTarget] = useState(props.targetOptions[0]?.id ?? '');

  return (
    <>
      <div className="sb-fact27-callout" role="note">
        <strong>Data does not pass through this node.</strong> A branch target receives this
        node&apos;s own return value (the match value), not whatever flowed into it. If a branch
        target needs the original input, have this node write it to a state field and have the
        branch target read it.
      </div>

      <fieldset>
        <legend>Branches</legend>
        <ul className="sb-item-list">
          {props.branches.map((b) => (
            <li key={b.match}>
              <span>
                <code>{b.match}</code> →{' '}
                {props.targetOptions.find((o) => o.id === b.targetNodeId)?.title ?? b.targetNodeId}
              </span>
              <button className="sb-btn-danger sb-btn-sm" onClick={() => props.onRemoveBranch(b.match)}>
                Remove
              </button>
            </li>
          ))}
        </ul>
        <div className="sb-add-row">
          <input value={newMatch} onChange={(e) => setNewMatch(e.target.value)} placeholder="match value" />
          <select value={newTarget} onChange={(e) => setNewTarget(e.target.value)}>
            {props.targetOptions.map((opt) => (
              <option key={opt.id} value={opt.id}>
                {opt.title}
              </option>
            ))}
          </select>
          <button
            onClick={() => {
              if (!newMatch.trim() || !newTarget) return;
              props.onAddBranch(newMatch.trim(), newTarget);
              setNewMatch('');
            }}
          >
            Add branch
          </button>
        </div>
      </fieldset>

      <label>
        Note (rendered in the golden diagram)
        <textarea value={props.note ?? ''} onChange={(e) => props.onNoteChange(e.target.value || null)} />
      </label>
    </>
  );
}

function JoinFields(props: {
  spec: { reducer: ReducerId; initialFactory: 'list' | 'dict' | 'int' | null | undefined };
  onChange: (patch: Partial<{ reducer: ReducerId; initialFactory: 'list' | 'dict' | 'int' | null }>) => void;
}) {
  return (
    <>
      <label>
        Reducer
        <select value={props.spec.reducer} onChange={(e) => props.onChange({ reducer: e.target.value as ReducerId })}>
          {REDUCER_IDS.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </label>
      <label>
        Initial factory
        <select
          value={props.spec.initialFactory ?? ''}
          onChange={(e) =>
            props.onChange({ initialFactory: (e.target.value || null) as 'list' | 'dict' | 'int' | null })
          }
        >
          <option value="">(auto)</option>
          <option value="list">list</option>
          <option value="dict">dict</option>
          <option value="int">int</option>
        </select>
      </label>
    </>
  );
}

export default Inspector;

interface LastRunSectionProps {
  trace: NonNullable<ReturnType<typeof useGraphStore.getState>['run']['nodes'][string]>;
}

/** What the last run recorded for the selected node (RunPanel's trace, per node). */
function LastRunSection({ trace }: LastRunSectionProps) {
  const status = trace.status === 'started' ? 'running' : trace.status;
  return (
    <details className="sb-last-run" open>
      <summary>
        Last run: <span className={`sb-run-${trace.status}`}>{status}</span>
        {trace.durationMs !== null ? ` — ${trace.durationMs} ms` : ''}
      </summary>
      {trace.inputs !== undefined && (
        <>
          <div className="sb-hint">Inputs</div>
          <pre className="sb-run-output">{formatRunValue(trace.inputs)}</pre>
        </>
      )}
      {trace.output !== undefined && (
        <>
          <div className="sb-hint">Output</div>
          <pre className="sb-run-output">{formatRunValue(trace.output)}</pre>
        </>
      )}
      {trace.stateDelta && Object.keys(trace.stateDelta).length > 0 && (
        <>
          <div className="sb-hint">State written</div>
          <pre className="sb-run-output">{formatRunValue(trace.stateDelta)}</pre>
        </>
      )}
      {trace.error && <div className="sb-finding sb-finding-error">{trace.error}</div>}
      {trace.traceback && <pre className="sb-run-output sb-run-traceback">{trace.traceback}</pre>}
    </details>
  );
}
