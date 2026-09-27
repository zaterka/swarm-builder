import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import Palette from './Palette';
import { useGraphStore } from '../state/graphStore';
import { buildDatabaseStarterMap } from '../api/schema';
import { databaseStartersFixture } from '../../test/fixtures';

// The palette is where a node kind either becomes creatable or does not. The
// database kinds are the interesting half: their entry is only usable once the
// starter catalog has loaded, because the catalog is what supplies the default
// operation, the example data and the mandatory I/O pair. These tests hold both
// halves of that rule -- three disabled entries while it is unavailable, three
// enabled entries that copy the starter into the new node once it is.

const DATABASE_LABELS = ['SQL database', 'NoSQL database', 'Vector store'];

beforeEach(() => {
  useGraphStore.setState({
    graph: null,
    databaseStarters: null,
    databaseStartersStatus: 'idle',
    databaseStartersError: null,
  });
  useGraphStore.getState().newGraph('palette-fixture', 'Palette fixture');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function entry(label: string): HTMLElement {
  return screen.getByText(label).closest('.sb-palette-item') as HTMLElement;
}

describe('Palette', () => {
  it('renders three disabled database entries while the catalog is unavailable', () => {
    useGraphStore.setState({ databaseStartersStatus: 'unavailable', databaseStartersError: '503' });
    render(<Palette />);

    for (const label of DATABASE_LABELS) {
      const item = entry(label);
      expect(item.getAttribute('aria-disabled')).toBe('true');
      expect(item.getAttribute('draggable')).toBe('false');
      expect(item.className).toContain('sb-palette-item-disabled');
    }

    // The hint says what is wrong and offers the retry, in the palette itself.
    expect(screen.getByRole('status').textContent).toContain('unavailable');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('creates no node when a disabled entry is clicked', () => {
    useGraphStore.setState({ databaseStartersStatus: 'unavailable' });
    render(<Palette />);

    const before = useGraphStore.getState().graph!.nodes.length;
    fireEvent.click(entry('SQL database'));

    expect(useGraphStore.getState().graph!.nodes).toHaveLength(before);
  });

  it('renders three enabled entries once the catalog is ready', () => {
    useGraphStore.setState({
      databaseStarters: buildDatabaseStarterMap(databaseStartersFixture()),
      databaseStartersStatus: 'ready',
    });
    render(<Palette />);

    for (const label of DATABASE_LABELS) {
      const item = entry(label);
      expect(item.getAttribute('aria-disabled')).toBeNull();
      expect(item.getAttribute('draggable')).toBe('true');
      expect(item.className).not.toContain('sb-palette-item-disabled');
    }
    // The server's own starter label is shown next to the kind name, so the
    // entry is concrete without any starter literal living in the bundle.
    expect(screen.getByText('SQL (fixture widgets)')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
  });

  it('copies the starter spec and io into the created node', () => {
    const starters = buildDatabaseStarterMap(databaseStartersFixture())!;
    useGraphStore.setState({ databaseStarters: starters, databaseStartersStatus: 'ready' });
    render(<Palette />);

    fireEvent.click(entry('NoSQL database'));

    const nodes = useGraphStore.getState().graph!.nodes;
    const created = nodes.find((n) => n.kind === 'nosql')!;
    expect(created).toBeDefined();
    expect(created.title).toBe('NoSQL database');
    expect(created.nosql).toEqual(starters.nosql!.spec);
    expect(created.io).toEqual(starters.nosql!.io);
    expect(created.template).toBeNull();
    // Nothing of the other kinds leaked in.
    expect(created.sql).toBeNull();
    expect(created.vector).toBeNull();
  });

  it('offers the four agent-side kinds with or without a catalog', () => {
    render(<Palette />);
    for (const label of ['Agent', 'Programmatic', 'Decision', 'Join']) {
      const item = entry(label);
      expect(item.getAttribute('aria-disabled')).toBeNull();
    }

    fireEvent.click(entry('Agent'));
    const created = useGraphStore
      .getState()
      .graph!.nodes.find((n) => n.kind === 'agent' && n.title === 'Agent')!;
    expect(created).toBeDefined();
    expect(created.agent).toBeTruthy();
  });
});
