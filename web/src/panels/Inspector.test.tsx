import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import Inspector from './Inspector';
import { useGraphStore, starterPatch } from '../state/graphStore';
import { buildDatabaseStarterMap } from '../api/schema';
import { databaseStartersFixture } from '../../test/fixtures';

// The Inspector is where a database node's operation is declared, and the only
// place the graph document is edited field by field. Three properties matter,
// and each of them is a way to produce a document nothing can compile:
//
//   1. fields are gated by kind -- a database node never reaches the agent
//      fields (Template / Instructions / Tools / Delegates to), and an agent
//      node never reaches the database fields (a database node carrying a
//      `template` is `db_template_set`);
//   2. JSON and numeric inputs never write an unparsed value -- the document has
//      `extra="forbid"` and typed fields, so a half-typed literal must stay in
//      the textarea and out of the store;
//   3. the tools list offers the graph's read-only database nodes as
//      `<kind>:<node_id>` entries and never offers a write-mode node.

const TEMPLATE_CATALOG = [
  { id: 'chat', label: 'Chat', description: '', defaultTools: ['web_search'], requiredEnv: [] },
];

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

beforeEach(() => {
  // `Inspector` reads the *agent* template catalog for the default tool list.
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/templates')) return jsonResponse(TEMPLATE_CATALOG);
      return jsonResponse({});
    }),
  );

  useGraphStore.setState({
    graph: null,
    databaseStarters: buildDatabaseStarterMap(databaseStartersFixture()),
    databaseStartersStatus: 'ready',
    databaseStartersError: null,
  });
  useGraphStore.getState().newGraph('inspector-fixture', 'Inspector fixture');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function starters() {
  return buildDatabaseStarterMap(databaseStartersFixture())!;
}

/** Add a node of `kind` and select it, exactly as the palette does.
 *
 * Wrapped in `act`: a store update the Inspector subscribes to has to be
 * flushed before any assertion, or React would still be rendering the previous
 * selection (`fireEvent` does this for events; a direct store call does not). */
function addAndSelect(kind: 'agent' | 'sql' | 'nosql' | 'vector', title: string): string {
  let id = '';
  act(() => {
    const patch = kind === 'agent' ? undefined : starterPatch(kind, starters());
    id = useGraphStore.getState().addNode(kind, title, { x: 0, y: 0 }, patch);
    useGraphStore.getState().selectNode(id);
  });
  return id;
}

/**
 * Render the Inspector and let its agent-template read settle.
 *
 * The component fetches `GET /api/templates` in an effect (for the default tool
 * list), and that promise resolving *after* the render is exactly the update
 * React warns about when it is not wrapped in `act`. One macrotask is enough to
 * drain it, which also means no assertion ever races the catalog.
 */
function optionsOf(label: string): string[] {
  return [...(screen.getByLabelText(label) as HTMLSelectElement).options].map((o) => o.value);
}

/** Let any render triggered by a store update settle. */
async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function renderInspector() {
  const result = render(<Inspector />);
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  return result;
}

/** The node the Inspector is *not* showing: a graph always has `newGraph`'s
 * starter agent, so "the agent node" means the one this test added. */
function agentNode(id: string) {
  return useGraphStore.getState().graph!.nodes.find((n) => n.id === id)!;
}

function node(id: string) {
  return useGraphStore.getState().graph!.nodes.find((n) => n.id === id)!;
}

describe('Inspector field gating', () => {
  it('hides every agent field for a database node and shows the database fields', async () => {
    addAndSelect('sql', 'Widget lookup');
    await renderInspector();

    expect(screen.queryByText('Template')).toBeNull();
    expect(screen.queryByText('Instructions')).toBeNull();
    expect(screen.queryByText('Delegates to (called as tools, not graph steps)')).toBeNull();
    expect(screen.queryByText('Tools')).toBeNull();

    expect(screen.getByLabelText('sql query')).toBeTruthy();
    expect(screen.getByLabelText('sql seed')).toBeTruthy();
    expect(screen.getByText('SQL database')).toBeTruthy();
  });

  it('hides the database fields for an agent node', async () => {
    addAndSelect('agent', 'Plain agent');
    await renderInspector();

    expect(screen.queryByLabelText('sql query')).toBeNull();
    expect(screen.queryByLabelText('nosql filter')).toBeNull();
    expect(screen.queryByLabelText('vector seed')).toBeNull();
    expect(screen.getByText('Template')).toBeTruthy();
    expect(screen.getByText('Tools')).toBeTruthy();
  });

  it('shows each database kind only its own fields', async () => {
    const first = await renderInspector();
    addAndSelect('nosql', 'Ticket lookup');
    expect(screen.getByLabelText('nosql collection')).toBeTruthy();
    expect(screen.getByLabelText('nosql filter')).toBeTruthy();
    expect(screen.getByLabelText('nosql limit')).toBeTruthy();
    expect(screen.getByLabelText('nosql seed')).toBeTruthy();
    expect(screen.queryByLabelText('sql query')).toBeNull();
    first.unmount();

    const second = await renderInspector();
    addAndSelect('vector', 'Doc search');
    expect(screen.getByLabelText('vector top k')).toBeTruthy();
    expect(screen.getByLabelText('vector min score')).toBeTruthy();
    expect(screen.getByLabelText('vector seed')).toBeTruthy();
    expect(screen.queryByLabelText('nosql filter')).toBeNull();
    second.unmount();

    await renderInspector();
    addAndSelect('sql', 'Widget lookup');
    expect(screen.getByLabelText('sql query')).toBeTruthy();
    expect(screen.queryByLabelText('vector seed')).toBeNull();
  });

  it('gives a database node no way to declare a template', async () => {
    const id = addAndSelect('vector', 'Doc search');
    await renderInspector();

    expect(node(id).template).toBeNull();
    expect(screen.queryByText('Template')).toBeNull();
  });

  it('offers only the port types each database kind may declare', async () => {
    const first = await renderInspector();
    addAndSelect('sql', 'Widget lookup');
    await flush();
    expect(optionsOf('Input type')).toEqual(['str', 'json', 'list[str]']);
    expect(optionsOf('Output type')).toEqual(['list[json]']);
    first.unmount();

    // A vector search's parameter is the query text, so `json` is not a pair
    // this kind can bind (review.py's `_BINDABLE_INPUT_TYPES`).
    const second = await renderInspector();
    addAndSelect('vector', 'Doc search');
    await flush();
    expect(optionsOf('Input type')).toEqual(['str']);
    second.unmount();

    // A document that already declares something outside the set keeps
    // showing its own value -- it is never silently rewritten.
    const third = await renderInspector();
    const id = addAndSelect('sql', 'Legacy lookup');
    act(() => {
      useGraphStore.getState().updateNode(id, { io: { inputType: 'list[str]', outputType: 'str' } });
    });
    await flush();
    expect(optionsOf('Input type')).toEqual(['str', 'json', 'list[str]']);
    expect(optionsOf('Output type')).toEqual(['list[json]', 'str']);
    third.unmount();
  });
});

describe('Inspector database fields write only valid values', () => {
  it('refuses to write invalid JSON, and says why', async () => {
    const id = addAndSelect('nosql', 'Ticket lookup');
    await renderInspector();

    const filter = screen.getByLabelText('nosql filter') as HTMLTextAreaElement;
    fireEvent.change(filter, { target: { value: '{"status": ' } });

    // Inline error, and the document keeps the value the starter gave it.
    expect(screen.getByRole('alert').textContent).toContain('Not valid JSON');
    expect(node(id).nosql!.filter).toEqual({ topic: '$input' });

    // The textarea keeps what was typed, so the literal can be finished.
    expect(filter.value).toBe('{"status": ');
  });

  it('writes a valid JSON filter as an object, not as a string', async () => {
    const id = addAndSelect('nosql', 'Ticket lookup');
    await renderInspector();

    fireEvent.change(screen.getByLabelText('nosql filter'), {
      target: { value: '{"status": "$input", "priority": {"$gte": 2}}' },
    });

    expect(screen.queryByRole('alert')).toBeNull();
    expect(node(id).nosql!.filter).toEqual({ status: '$input', priority: { $gte: 2 } });
  });

  it('requires a JSON array of objects for a seed, and a document shape for vector', async () => {
    const nosqlId = addAndSelect('nosql', 'Ticket lookup');
    const first = await renderInspector();

    fireEvent.change(screen.getByLabelText('nosql seed'), { target: { value: '{"_id": "n-1"}' } });
    expect(screen.getByRole('alert').textContent).toContain('Must be a JSON array of objects');
    expect(node(nosqlId).nosql!.seed).toEqual([{ _id: 'n-1', topic: 'alpha' }]);

    fireEvent.change(screen.getByLabelText('nosql seed'), { target: { value: '[{"_id": "n-2"}]' } });
    expect(node(nosqlId).nosql!.seed).toEqual([{ _id: 'n-2' }]);
    first.unmount();

    const vectorId = addAndSelect('vector', 'Doc search');
    await renderInspector();

    fireEvent.change(screen.getByLabelText('vector seed'), { target: { value: '[{"id": "d-2"}]' } });
    expect(screen.getByRole('alert').textContent).toContain('"id" and a string "text"');
    expect(node(vectorId).vector!.seed).toHaveLength(1);

    fireEvent.change(screen.getByLabelText('vector seed'), {
      target: { value: '[{"id": "d-2", "text": "Second doc.", "metadata": {"section": "intro"}}]' },
    });
    expect(node(vectorId).vector!.seed).toEqual([
      { id: 'd-2', text: 'Second doc.', metadata: { section: 'intro' } },
    ]);
  });

  it('refuses to write a non-numeric limit', async () => {
    const id = addAndSelect('nosql', 'Ticket lookup');
    await renderInspector();

    fireEvent.change(screen.getByLabelText('nosql limit'), { target: { value: 'twenty' } });
    expect(screen.getByRole('alert').textContent).toContain('Must be a number');
    expect(node(id).nosql!.limit).toBe(5);

    fireEvent.change(screen.getByLabelText('nosql limit'), { target: { value: '25' } });
    expect(node(id).nosql!.limit).toBe(25);
  });

  it('writes plain text fields directly (a query is not JSON)', async () => {
    const id = addAndSelect('sql', 'Widget lookup');
    await renderInspector();

    fireEvent.change(screen.getByLabelText('sql query'), {
      target: { value: 'SELECT id FROM widgets WHERE name = :input' },
    });
    fireEvent.change(screen.getByLabelText('sql seed'), {
      target: { value: 'CREATE TABLE t (i INT);' },
    });

    expect(node(id).sql!.query).toBe('SELECT id FROM widgets WHERE name = :input');
    expect(node(id).sql!.seedSql).toBe('CREATE TABLE t (i INT);');
  });

  it('warns explicitly that a write node cannot be an agent tool', async () => {
    addAndSelect('sql', 'Widget update');
    await renderInspector();

    expect(screen.queryByRole('note')).toBeNull();
    fireEvent.click(screen.getByLabelText(/Write mode/));
    expect(screen.getByRole('note').textContent).toContain('cannot be used as an agent tool');
  });
});

describe('Inspector "Reset to example"', () => {
  it('restores both the spec and the io from the starter', async () => {
    const id = addAndSelect('sql', 'Widget lookup');
    const starter = starters().sql!;
    const first = await renderInspector();

    // Drift the node: a different query, and a different input port.
    fireEvent.change(screen.getByLabelText('sql query'), { target: { value: 'SELECT 1' } });
    fireEvent.change(screen.getByLabelText('Input type'), { target: { value: 'json' } });
    expect(node(id).sql!.query).toBe('SELECT 1');
    expect(node(id).io.inputType).toBe('json');
    first.unmount();

    await renderInspector();
    fireEvent.click(screen.getByRole('button', { name: 'Reset to example' }));

    expect(node(id).sql).toEqual(starter.spec);
    expect(node(id).io).toEqual(starter.io);
  });

  it('names the env vars and the extra from the catalog, never from this bundle', async () => {
    addAndSelect('vector', 'Doc search');
    await renderInspector();

    const starter = starters().vector!;
    const hint = screen.getByText(/The mock runs with no configuration/).parentElement!;
    for (const name of starter.envVars) {
      expect(hint.textContent).toContain(name);
    }
    expect(hint.textContent).toContain(`uv sync --extra ${starter.liveExtra}`);
  });

  it('names the missing spec of a database node that never got one, and can fill it in', async () => {
    // The §6 edge case: a hand-written or legacy document with
    // `kind: 'sql'` and `sql: null`. Phase 1 reports `db_empty_operation`;
    // the Inspector has to say the same thing and offer the one action that
    // fixes it.
    const sqlId = addAndSelect('sql', 'Spec-less lookup');
    act(() => {
      useGraphStore.getState().updateNode(sqlId, { sql: null });
    });
    await renderInspector();

    expect(screen.getByRole('note').textContent).toContain('db_empty_operation');
    expect(screen.queryByLabelText('sql query')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Reset to example' }));

    expect(node(sqlId).sql).toEqual(starters().sql!.spec);
    expect(screen.getByLabelText('sql query')).toBeTruthy();
  });

  it('is disabled with an explanation while the catalog is unavailable', async () => {
    useGraphStore.setState({ databaseStarters: null, databaseStartersStatus: 'unavailable' });
    addAndSelect('sql', 'Widget lookup');
    await renderInspector();

    expect(screen.getByRole('button', { name: 'Reset to example' })).toBeDisabled();
    expect(screen.getByText(/starter catalog is unavailable/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  });
});

describe('Inspector tools list', () => {
  it('offers one read-only entry per database node, spelled <kind>:<node_id>', async () => {
    const nosqlId = addAndSelect('nosql', 'Ticket lookup');
    const vectorId = addAndSelect('vector', 'Doc search');
    const agentId = addAndSelect('agent', 'Answer the user');
    await renderInspector();

    const nosqlTool = screen.getByRole('checkbox', {
      name: new RegExp(`nosql:${nosqlId}`),
    }) as HTMLInputElement;
    expect(nosqlTool.checked).toBe(false);
    // The label says both what it is and that the agent cannot write with it.
    fireEvent.click(nosqlTool);

    expect(agentNode(agentId).agent!.tools).toContain(`nosql:${nosqlId}`);
    expect(
      screen.getByRole('checkbox', { name: new RegExp(`vector:${vectorId}`) }),
    ).toBeTruthy();
    // Both database nodes are offered, each labelled as read-only.
    expect(screen.getAllByRole('checkbox', { name: /read-only repository tool/ })).toHaveLength(2);
  });

  it('removes the entry again when the checkbox is cleared', async () => {
    const nosqlId = addAndSelect('nosql', 'Ticket lookup');
    const agentId = addAndSelect('agent', 'Answer the user');
    await renderInspector();

    const tool = () =>
      screen.getByRole('checkbox', { name: new RegExp(`nosql:${nosqlId}`) }) as HTMLInputElement;

    fireEvent.click(tool());
    expect(agentNode(agentId).agent!.tools).toContain(`nosql:${nosqlId}`);

    fireEvent.click(tool());
    expect(agentNode(agentId).agent!.tools).not.toContain(`nosql:${nosqlId}`);
  });

  it('does not offer a write-mode database node, and names it instead', async () => {
    const writeId = addAndSelect('sql', 'Widget update');
    const readId = addAndSelect('sql', 'Widget lookup');
    act(() => {
      useGraphStore.getState().updateNode(writeId, {
        sql: { query: 'UPDATE widgets SET name = :x', seedSql: 's', write: true, note: null },
      });
    });
    addAndSelect('agent', 'Answer the user');
    await renderInspector();

    expect(screen.queryByRole('checkbox', { name: new RegExp(`sql:${writeId}`) })).toBeNull();
    expect(screen.getByRole('checkbox', { name: new RegExp(`sql:${readId}`) })).toBeTruthy();
    expect(screen.getByText(/Not offered: Widget update/)).toBeTruthy();
  });

  it('offers a node created with no catalog, because a read node with no operation yet is still readable', async () => {
    let id = '';
    let agentId = '';
    act(() => {
      id = useGraphStore.getState().addNode('nosql', 'Empty lookup', { x: 0, y: 0 });
      agentId = useGraphStore.getState().addNode('agent', 'Answer the user', { x: 10, y: 10 });
      useGraphStore.getState().selectNode(agentId);
    });
    await renderInspector();

    expect(screen.getByRole('checkbox', { name: new RegExp(`nosql:${id}`) })).toBeTruthy();
  });
});
