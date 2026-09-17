import { useEffect, useState } from 'react';
import { useGraphStore } from '../state/graphStore';
import { inferTemplate } from '../infer';
import {
  PORT_TYPES,
  REDUCER_IDS,
  TEMPLATE_IDS,
  type PortType,
  type ReducerId,
  type TemplateId,
} from '../api/schema';
import { api, TemplatesUnavailableError } from '../api/client';
import type { TemplateEntryOut } from '../api/schema';
import StateFieldsPanel from './StateFieldsPanel';

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
            {PORT_TYPES.map((t) => (
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
            {PORT_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>

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
      </div>
    </div>
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
        <ul>
          {props.spec.needs.map((need) => (
            <li key={need}>
              {need}{' '}
              <button onClick={() => props.onChange({ needs: props.spec.needs.filter((n) => n !== need) })}>
                Remove
              </button>
            </li>
          ))}
        </ul>
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
        <ul>
          {props.branches.map((b) => (
            <li key={b.match}>
              <code>{b.match}</code> → {props.targetOptions.find((o) => o.id === b.targetNodeId)?.title ?? b.targetNodeId}{' '}
              <button onClick={() => props.onRemoveBranch(b.match)}>Remove</button>
            </li>
          ))}
        </ul>
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
