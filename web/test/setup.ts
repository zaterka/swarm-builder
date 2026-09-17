// jsdom stubs xyflow/React Flow needs to actually render node content
// (PLAN.md Group 6 review finding C6): ResizeObserver and
// DOMMatrixReadOnly do not exist in jsdom, and @xyflow/react measures
// nodes via ResizeObserver before painting their content.
import '@testing-library/jest-dom/vitest';

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// jsdom has no ResizeObserver. This used to need a `@ts-expect-error`
// suppression, but with `noUncheckedIndexedAccess` enabled (see
// tsconfig.json) tsc no longer flags this assignment, so the directive
// was removed rather than left in as dead-but-tolerated code (an
// unused suppression comment fails the build under this project's
// strict settings).
globalThis.ResizeObserver = ResizeObserverStub;

if (typeof (globalThis as any).DOMMatrixReadOnly === 'undefined') {
  class DOMMatrixReadOnlyStub {
    m22 = 1;
    constructor(_transform?: string) {}
  }
  (globalThis as any).DOMMatrixReadOnly = DOMMatrixReadOnlyStub;
}
