import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { ReactFlowProvider } from '@xyflow/react';
import Canvas from './Canvas';
import { useGraphStore } from '../state/graphStore';
import { renderOnlyFixtureGraph } from '../../test/fixtures';

// Required frontend smoke test #2 (PLAN.md "Tests" -> "Frontend
// smoke"): a render test that the canvas mounts with a fixture graph.
// jsdom stubs (ResizeObserver, DOMMatrixReadOnly) are installed by
// test/setup.ts -- without them @xyflow/react 12 never renders node
// content under jsdom and this test would be silently vacuous
// (GROUP6_PLAN.md review finding C6).

describe('Canvas render smoke test', () => {
  it('mounts with a fixture graph and renders one DOM node per fixture node', () => {
    useGraphStore.getState().loadGraph(renderOnlyFixtureGraph());

    const { container } = render(
      <div style={{ width: 800, height: 600 }}>
        <ReactFlowProvider>
          <Canvas />
        </ReactFlowProvider>
      </div>,
    );

    expect(container.querySelector('.react-flow')).toBeTruthy();

    const fixture = renderOnlyFixtureGraph();
    for (const node of fixture.nodes ?? []) {
      const el = container.querySelector(`[data-testid="node-${node.id}"]`);
      expect(el, `expected a DOM node for fixture node ${node.id}`).toBeTruthy();
      expect(el!.getAttribute('data-node-kind')).toBe(node.kind);
    }
  });
});
