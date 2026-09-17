import { useState } from 'react';
import { useGraphStore } from '../state/graphStore';
import { PORT_TYPES, type PortType, type StateField } from '../api/schema';

/**
 * Graph-scoped state-field editor (PLAN.md doesn't specify where
 * `SwarmGraph.stateFields` is authored -- GROUP6_PLAN.md decision 2).
 * Shown in the Inspector's slot when no node is selected, and reachable
 * as a pinned tab of the Inspector while a node IS selected, so adding
 * a field while wiring a node's reads/writes needs no navigation
 * round-trip (review finding C4).
 *
 * `StateField.default` is documented as literal Python source, not a
 * plain value (`'""'`, `"0"`, never `""`, `0`) -- the editor exposes a
 * type-driven helper rather than an unannotated free-text field
 * (review finding C4).
 */
export function StateFieldsPanel() {
  const stateFields = useGraphStore((s) => s.graph?.stateFields ?? []);
  const setStateFields = useGraphStore((s) => s.setStateFields);

  const [newName, setNewName] = useState('');
  const [newType, setNewType] = useState<PortType>('str');

  const addField = () => {
    if (!newName.trim()) return;
    const field: StateField = { name: newName.trim(), type: newType, default: null, description: null };
    setStateFields([...stateFields, field]);
    setNewName('');
  };

  const removeField = (name: string) => {
    setStateFields(stateFields.filter((f) => f.name !== name));
  };

  const updateField = (name: string, patch: Partial<StateField>) => {
    setStateFields(stateFields.map((f) => (f.name === name ? { ...f, ...patch } : f)));
  };

  const defaultHint = (type: PortType): string => {
    switch (type) {
      case 'str':
        return 'literal Python source, e.g. \'""\' or "\'hello\'"';
      case 'list[str]':
        return 'literal Python source, e.g. "[]"';
      case 'json':
        return 'literal Python source, e.g. "{}" or "0"';
    }
  };

  return (
    <div className="sb-state-fields-panel">
      <h3>State fields</h3>
      <p className="sb-hint">
        Becomes the generated workflow&apos;s <code>@dataclass State</code>. <code>default</code> is
        literal Python source, not a plain value.
      </p>
      <ul>
        {stateFields.map((field) => (
          <li key={field.name} className="sb-state-field-row">
            <input
              aria-label={`state field name ${field.name}`}
              value={field.name}
              onChange={(e) => updateField(field.name, { name: e.target.value })}
            />
            <select
              aria-label={`state field type ${field.name}`}
              value={field.type}
              onChange={(e) => updateField(field.name, { type: e.target.value as PortType })}
            >
              {PORT_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            <input
              aria-label={`state field default ${field.name}`}
              placeholder={defaultHint(field.type)}
              value={field.default ?? ''}
              onChange={(e) => updateField(field.name, { default: e.target.value || null })}
            />
            <button onClick={() => removeField(field.name)}>Remove</button>
          </li>
        ))}
      </ul>
      <div className="sb-state-field-add">
        <input placeholder="field name" value={newName} onChange={(e) => setNewName(e.target.value)} />
        <select value={newType} onChange={(e) => setNewType(e.target.value as PortType)}>
          {PORT_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <button onClick={addField}>Add field</button>
      </div>
    </div>
  );
}

export default StateFieldsPanel;
