import SeqEdgeComponent from './SeqEdge';
import BranchEdgeComponent from './BranchEdge';
import FanoutEdgeComponent from './FanoutEdge';
import JoinEdgeComponent from './JoinEdge';
import DelegateEdgeComponent from './DelegateEdge';

export const edgeTypes = {
  seq: SeqEdgeComponent,
  branch: BranchEdgeComponent,
  fanout: FanoutEdgeComponent,
  join: JoinEdgeComponent,
  delegate: DelegateEdgeComponent,
};

export {
  SeqEdgeComponent,
  BranchEdgeComponent,
  FanoutEdgeComponent,
  JoinEdgeComponent,
  DelegateEdgeComponent,
};
