import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { GraphListError, GraphSummary } from '../api/schema';

export function GraphPicker(props: { onOpen: (id: string) => void; onCreateNew: () => void }) {
  const [graphs, setGraphs] = useState<GraphSummary[]>([]);
  const [errors, setErrors] = useState<GraphListError[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = () => {
    setLoading(true);
    api
      .listGraphs()
      .then((r) => {
        setGraphs(r.graphs);
        setErrors(r.errors);
      })
      .finally(() => setLoading(false));
  };

  useEffect(refresh, []);

  const handleDelete = async (id: string, withProject: boolean) => {
    await api.deleteGraph(id, { project: withProject });
    refresh();
  };

  return (
    <div className="sb-graph-picker">
      <h2>Swarm Builder</h2>
      <button onClick={props.onCreateNew}>New graph</button>
      {loading && <div>Loading…</div>}
      <ul>
        {graphs.map((g) => (
          <li key={g.id}>
            <button onClick={() => props.onOpen(g.id)}>{g.name}</button>
            <span className="sb-hint">
              {g.nodeCount} nodes, {g.edgeCount} edges
            </span>
            <button onClick={() => handleDelete(g.id, false)}>Delete</button>
            <button onClick={() => handleDelete(g.id, true)}>Delete + project</button>
          </li>
        ))}
      </ul>
      {errors.length > 0 && (
        <div className="sb-graph-list-errors">
          <h4>Some graphs could not be loaded</h4>
          <ul>
            {errors.map((e) => (
              <li key={e.id}>
                {e.id}: {e.detail}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default GraphPicker;
