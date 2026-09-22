import { useCallback, useMemo, useRef, useState } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useReactFlow,
  type Node,
  type Edge,
  type NodeChange,
  type Connection,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useGraphStore } from '../state/graphStore';
import type { NodeKind, SwarmNode } from '../api/schema';
import AgentNode from './nodes/AgentNode';
import ProgrammaticNode from './nodes/ProgrammaticNode';
import DecisionNode from './nodes/DecisionNode';
import JoinNode from './nodes/JoinNode';
import type { SwarmNodeData } from './nodes/AgentNode';
import { edgeTypes } from './edges';
import { deriveRunDisplayStatuses } from '../state/runState';
import './theme.css';

const nodeTypes = {
  agent: AgentNode,
  programmatic: ProgrammaticNode,
  decision: DecisionNode,
  join: JoinNode,
};

interface PendingConnection {
  connection: Connection;
  sourceKind: SwarmNode['kind'];
  /** true when the source already has an outgoing seq/branch successor,
   * so a plain second successor must become a fanout pair instead of a
   * silently-lossy second seq edge (review.py's fanout_without_join
   * check; GROUP6_PLAN.md review finding D3). */
  needsFanoutChoice: boolean;
}

/**
 * React Flow is a PROJECTION of the zustand store (PLAN.md "State"):
 * `toFlowNodes`/`toFlowEdges` are the only path from `graph` to what
 * React Flow renders, and the only write-back path is
 * `updateNodePosition` (position changes with dragging===false) --
 * select/dimensions changes are ignored entirely, and `remove` changes
 * route to the semantic `removeNode` action (review findings A6/A7).
 */
export function Canvas() {
  const graph = useGraphStore((s) => s.graph);
  const run = useGraphStore((s) => s.run);
  const selectedNodeIds = useGraphStore((s) => s.selectedNodeIds);
  const selectedEdgeId = useGraphStore((s) => s.selectedEdgeId);
  const selectNode = useGraphStore((s) => s.selectNode);
  const selectEdge = useGraphStore((s) => s.selectEdge);
  const updateNodePosition = useGraphStore((s) => s.updateNodePosition);
  const removeNode = useGraphStore((s) => s.removeNode);
  const addEdge = useGraphStore((s) => s.addEdge);
  const addNode = useGraphStore((s) => s.addNode);
  const addDecisionBranch = useGraphStore((s) => s.addDecisionBranch);
  const { screenToFlowPosition } = useReactFlow();
  const paneRef = useRef<HTMLDivElement>(null);

  const [pending, setPending] = useState<PendingConnection | null>(null);

  const flowNodes: Node<SwarmNodeData>[] = useMemo(() => {
    if (!graph) return [];
    const selected = new Set(selectedNodeIds);
    const runStatuses = deriveRunDisplayStatuses(run, graph.nodes, graph.edges);
    return graph.nodes.map((node) => ({
      id: node.id,
      type: node.kind,
      position: node.position,
      selected: selected.has(node.id),
      data: {
        node,
        isEntry: node.id === graph.entryNodeId,
        isExit: node.id === graph.exitNodeId,
        runStatus: runStatuses[node.id] ?? 'idle',
      },
    }));
  }, [graph, selectedNodeIds, run]);

  const flowEdges: Edge[] = useMemo(() => {
    if (!graph) return [];
    return graph.edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: edge.kind,
      selected: edge.id === selectedEdgeId,
      data:
        edge.kind === 'seq'
          ? { label: edge.label }
          : edge.kind === 'branch'
            ? { match: edge.match }
            : edge.kind === 'fanout'
              ? { joinNodeId: edge.joinNodeId }
              : undefined,
    }));
  }, [graph, selectedEdgeId]);

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      for (const change of changes) {
        if (change.type === 'position' && change.position && change.dragging === false) {
          updateNodePosition(change.id, change.position);
        } else if (change.type === 'remove') {
          removeNode(change.id);
        }
        // 'select' and 'dimensions' changes are intentionally ignored:
        // neither should mark the graph dirty or write anything back
        // (review finding A7).
      }
    },
    [updateNodePosition, removeNode],
  );

  const onNodeClick = useCallback(
    (_: unknown, node: Node) => {
      selectNode(node.id);
    },
    [selectNode],
  );

  const onEdgeClick = useCallback(
    (_: unknown, edge: Edge) => {
      selectEdge(edge.id);
    },
    [selectEdge],
  );

  const onPaneClick = useCallback(() => {
    selectNode(null);
    selectEdge(null);
  }, [selectNode, selectEdge]);

  // Palette drop coordinates need screenToFlowPosition -- Position is
  // in flow space, a drop event gives client (screen) coordinates
  // (GROUP6_PLAN.md review finding E7). Getting this wrong puts every
  // dropped node off-viewport.
  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      const kind = event.dataTransfer.getData('application/swarm-builder-node-kind') as NodeKind;
      if (!kind) return;
      const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
      addNode(kind, kind.charAt(0).toUpperCase() + kind.slice(1), position);
    },
    [screenToFlowPosition, addNode],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!graph || !connection.source || !connection.target) return;
      const sourceNode = graph.nodes.find((n) => n.id === connection.source);
      if (!sourceNode) return;

      if (sourceNode.kind === 'decision') {
        // Decision sources always create a branch -- ask for the match
        // expression via the popover (review finding D3).
        setPending({ connection, sourceKind: sourceNode.kind, needsFanoutChoice: false });
        return;
      }

      const existingOutgoing = graph.edges.filter(
        (e) => e.source === connection.source && (e.kind === 'seq' || e.kind === 'fanout'),
      );
      const needsFanoutChoice = existingOutgoing.length > 0;
      setPending({ connection, sourceKind: sourceNode.kind, needsFanoutChoice });
    },
    [graph],
  );

  const resolvePendingAsSeq = useCallback(() => {
    if (!pending) return;
    const { connection } = pending;
    addEdge('seq', connection.source!, connection.target!, { kind: 'seq' });
    setPending(null);
  }, [pending, addEdge]);

  const resolvePendingAsDelegate = useCallback(() => {
    if (!pending) return;
    const { connection } = pending;
    addEdge('delegate', connection.source!, connection.target!, { kind: 'delegate' });
    setPending(null);
  }, [pending, addEdge]);

  const resolvePendingAsBranch = useCallback(
    (match: string) => {
      if (!pending) return;
      const { connection } = pending;
      if (!match.trim()) return;
      addDecisionBranch(connection.source!, match.trim(), connection.target!);
      setPending(null);
    },
    [pending, addDecisionBranch],
  );

  const resolvePendingAsFanout = useCallback(
    (joinNodeId: string) => {
      if (!pending || !graph) return;
      const { connection } = pending;
      // Both the new arm and the pre-existing arm(s) from this source
      // become fanout edges converging on the chosen join node -- a
      // plain second seq/fanout pair is exactly what review.py's
      // fanout_without_join check rejects (review finding D3).
      const existingOutgoing = graph.edges.filter(
        (e) => e.source === connection.source && (e.kind === 'seq' || e.kind === 'fanout'),
      );
      for (const e of existingOutgoing) {
        if (e.kind === 'seq') {
          useGraphStore.getState().removeEdge(e.id);
          addEdge('fanout', e.source, e.target, { kind: 'fanout', joinNodeId });
          addEdge('join', e.target, joinNodeId, { kind: 'join' });
        }
      }
      addEdge('fanout', connection.source!, connection.target!, { kind: 'fanout', joinNodeId });
      addEdge('join', connection.target!, joinNodeId, { kind: 'join' });
      setPending(null);
    },
    [pending, graph, addEdge],
  );

  if (!graph) {
    return <div className="sb-canvas-empty">No graph loaded.</div>;
  }

  const joinNodeOptions = graph.nodes.filter((n) => n.kind === 'join');

  return (
    <div
      ref={paneRef}
      style={{ width: '100%', height: '100%', position: 'relative' }}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onNodeClick={onNodeClick}
        onEdgeClick={onEdgeClick}
        onPaneClick={onPaneClick}
        onConnect={onConnect}
        className="sb-canvas-pane"
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={20} size={1.15} color="#d8d1c1" />
        <Controls />
        <MiniMap />
      </ReactFlow>

      {pending && (
        <EdgeKindPopover
          sourceKind={pending.sourceKind}
          needsFanoutChoice={pending.needsFanoutChoice}
          joinNodeOptions={joinNodeOptions.map((n) => ({ id: n.id, title: n.title }))}
          onSeq={resolvePendingAsSeq}
          onDelegate={resolvePendingAsDelegate}
          onBranch={resolvePendingAsBranch}
          onFanout={resolvePendingAsFanout}
          onCancel={() => setPending(null)}
        />
      )}
    </div>
  );
}

function EdgeKindPopover(props: {
  sourceKind: SwarmNode['kind'];
  needsFanoutChoice: boolean;
  joinNodeOptions: { id: string; title: string }[];
  onSeq: () => void;
  onDelegate: () => void;
  onBranch: (match: string) => void;
  onFanout: (joinNodeId: string) => void;
  onCancel: () => void;
}) {
  const [match, setMatch] = useState('');
  const [joinNodeId, setJoinNodeId] = useState(props.joinNodeOptions[0]?.id ?? '');

  return (
    <div role="dialog" aria-label="Choose edge kind" className="sb-edge-popover">
      <div className="sb-edge-popover-title">What kind of connection is this?</div>

      {props.sourceKind === 'decision' && (
        <div>
          <label>
            Match expression
            <input value={match} onChange={(e) => setMatch(e.target.value)} autoFocus />
          </label>
          <div className="sb-edge-popover-actions">
            <button className="sb-btn-primary" onClick={() => props.onBranch(match)}>
              Add branch
            </button>
            <button onClick={props.onCancel}>Cancel</button>
          </div>
        </div>
      )}

      {props.sourceKind !== 'decision' && !props.needsFanoutChoice && (
        <div className="sb-edge-popover-actions">
          <button className="sb-btn-primary" onClick={props.onSeq}>
            Sequential (seq)
          </button>
          {props.sourceKind === 'agent' && <button onClick={props.onDelegate}>Delegate (tool call)</button>}
          <button onClick={props.onCancel}>Cancel</button>
        </div>
      )}

      {props.sourceKind !== 'decision' && props.needsFanoutChoice && (
        <div>
          <div className="sb-edge-popover-note">
            This node already has an outgoing connection. A second successor must fan out to an
            explicit join node, or it will silently lose data.
          </div>
          <label>
            Join node
            <select value={joinNodeId} onChange={(e) => setJoinNodeId(e.target.value)}>
              {props.joinNodeOptions.length === 0 && <option value="">(create a join node first)</option>}
              {props.joinNodeOptions.map((opt) => (
                <option key={opt.id} value={opt.id}>
                  {opt.title}
                </option>
              ))}
            </select>
          </label>
          <div className="sb-edge-popover-actions">
            <button className="sb-btn-primary" disabled={!joinNodeId} onClick={() => props.onFanout(joinNodeId)}>
              Fan out to join
            </button>
            <button onClick={props.onCancel}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

export default Canvas;
