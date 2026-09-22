// Interactive example: the real triage graph, drawn with the app's node
// styling, with an inspector fed by the real generated code and a replay of a
// real run. Nothing here calls a model; every value shown was produced by the
// compile/run recorded in demo-data.js. Plain DOM + SVG, no dependencies.
(function () {
  "use strict";
  const data = window.SWARM_DEMO;
  const root = document.getElementById("demo");
  if (!data || !root) return;

  const NODE_W = 210, NODE_H = 96, SCALE = 0.86;
  // Room around each card inside its foreignObject, so the corner tags and the
  // run-status ring are not clipped at the box edge.
  const PAD = 8, TAG = 10;
  const nodes = data.graph.nodes;
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));

  // ---- layout: use the generated positions, scaled, with padding ----------
  const xs = nodes.map((n) => n.position.x), ys = nodes.map((n) => n.position.y);
  const minX = Math.min(...xs), minY = Math.min(...ys);
  const pos = (n) => ({ x: (n.position.x - minX) * SCALE + 16, y: (n.position.y - minY) * SCALE + 16 });
  const width = Math.max(...nodes.map((n) => pos(n).x)) + NODE_W + 16;
  const height = Math.max(...nodes.map((n) => pos(n).y)) + NODE_H + 24;

  const svgNS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs, parent) => {
    const e = document.createElementNS(svgNS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  };

  const canvas = root.querySelector(".demo-canvas");
  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Workflow canvas" }, canvas);
  const defs = el("defs", {}, svg);
  const marker = el("marker", { id: "demo-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "8", markerHeight: "8", orient: "auto-start-reverse" }, defs);
  el("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "currentColor" }, marker);
  const edgeLayer = el("g", { class: "demo-edges" }, svg);
  const nodeLayer = el("g", { class: "demo-nodes" }, svg);

  // ---- edges ---------------------------------------------------------------
  const anchor = (n, side) => {
    const p = pos(n);
    return side === "out" ? { x: p.x + NODE_W, y: p.y + NODE_H / 2 } : { x: p.x, y: p.y + NODE_H / 2 };
  };
  for (const e of data.graph.edges) {
    const a = anchor(byId[e.source], "out"), b = anchor(byId[e.target], "in");
    const dx = Math.max(40, (b.x - a.x) / 2);
    const d = `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
    const g = el("g", { class: `demo-edge demo-edge-${e.kind}`, "data-edge": e.id }, edgeLayer);
    el("path", { d, "marker-end": "url(#demo-arrow)" }, g);
    if (e.kind === "branch") {
      const t = el("text", { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 8, "text-anchor": "middle" }, g);
      t.textContent = e.match;
    }
  }

  // ---- nodes (HTML buttons inside foreignObject so they scale with the SVG) --
  const BADGE = {
    agent: (n) => "AGENT" + (n.template ? " · " + n.template : ""),
    programmatic: () => "PYTHON",
    decision: (n) => `IF · ${(n.decision?.branches || []).length} branches`,
    join: (n) => `JOIN · ${n.join?.reducer || ""}`,
  };
  const buttons = {};
  for (const n of nodes) {
    const p = pos(n);
    const fo = el("foreignObject", {
      x: p.x - PAD, y: p.y - TAG - PAD, width: NODE_W + 2 * PAD, height: NODE_H + TAG + 2 * PAD,
    }, nodeLayer);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `sb-node sb-kind-${n.kind}`;
    btn.style.cssText = `margin:${TAG + PAD}px ${PAD}px ${PAD}px; width:${NODE_W}px; height:${NODE_H}px;`;
    btn.dataset.node = n.id;
    btn.dataset.runStatus = "idle";
    btn.setAttribute("aria-pressed", "false");
    btn.innerHTML =
      (n.id === data.graph.entryNodeId ? '<span class="sb-node-tag">START</span>' : "") +
      (n.id === data.graph.exitNodeId ? '<span class="sb-node-tag sb-node-tag-end">END</span>' : "") +
      `<span class="sb-node-badge">${BADGE[n.kind](n)}</span>` +
      `<span class="sb-node-title">${escapeHtml(n.title)}</span>` +
      `<span class="sb-node-intent">${escapeHtml(n.intent)}</span>`;
    btn.addEventListener("click", () => select(n.id));
    fo.appendChild(btn);
    buttons[n.id] = btn;
  }

  // ---- inspector -----------------------------------------------------------
  const inspector = root.querySelector(".demo-inspector");
  let selected = null;
  let tab = "node";

  function select(id) {
    selected = id;
    for (const k in buttons) buttons[k].setAttribute("aria-pressed", String(k === id));
    render();
  }

  function render() {
    const n = byId[selected];
    if (!n) {
      inspector.innerHTML = `<p class="demo-hint">Click a node to inspect it. Everything shown is real output from one session: the graph a paragraph produced, the code the compile emitted, and the trace of one run.</p>`;
      return;
    }
    const tabs = [["node", "Node"], ...Object.keys(n.code).map((k) => [k, CODE_LABEL[k]])];
    if (n.trace) tabs.push(["trace", "Last run"]);
    if (!tabs.some(([k]) => k === tab)) tab = "node";
    const tabHtml = `<div class="demo-tabs" role="tablist">${tabs
      .map(([k, label]) => `<button type="button" role="tab" aria-selected="${k === tab}" data-tab="${k}">${escapeHtml(label.replace("…", n.id + "."))}</button>`)
      .join("")}</div>`;
    let body = "";
    if (tab === "node") body = renderNode(n);
    else if (tab === "trace") body = renderTrace(n);
    else body = renderCode(n.code[tab]);
    inspector.innerHTML = `<div class="demo-inspector-head"><span class="sb-node-badge sb-kind-${n.kind}">${BADGE[n.kind](n)}</span><strong>${escapeHtml(n.title)}</strong><code>${n.id}</code></div>${tabHtml}<div class="demo-panel">${body}</div>`;
    for (const b of inspector.querySelectorAll("[data-tab]")) b.addEventListener("click", () => { tab = b.dataset.tab; render(); });
  }

  const CODE_LABEL = {
    pydanticStep: "steps/…py",
    pydanticAgent: "agents/…py",
    pydanticWiring: "graph.py",
    langgraphNode: "LangGraph node",
  };

  function renderNode(n) {
    const rows = [
      ["Intent", escapeHtml(n.intent)],
      ["Ports", `<code>${n.io.inputType}</code> → <code>${n.io.outputType}</code>`],
    ];
    if (n.reads.length) rows.push(["Reads state", n.reads.map((f) => `<code>${f}</code>`).join(", ")]);
    if (n.writes.length) rows.push(["Writes state", n.writes.map((f) => `<code>${f}</code>`).join(", ")]);
    if (n.agent) rows.push(["Instructions", `<span class="demo-quote">${escapeHtml(n.agent.instructions)}</span>`]);
    if (n.decision) rows.push(["Branches", n.decision.branches.map((b) => `<code>${escapeHtml(b.match)}</code> → ${b.targetNodeId}`).join("<br>")]);
    if (n.join) rows.push(["Reducer", `<code>${n.join.reducer}</code>`]);
    if (n.kind === "decision") rows.push(["Note", "No data passes through a decision: the branch target receives the match value the previous step returned."]);
    return `<dl class="demo-props">${rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>`;
  }

  function renderCode(c) {
    return `<div class="demo-path">${escapeHtml(c.path)}</div><pre><code>${escapeHtml(c.text)}</code></pre>`;
  }

  function renderTrace(n) {
    const t = n.trace;
    const out = typeof t.output === "string" ? t.output : JSON.stringify(t.output, null, 2);
    return `<dl class="demo-props"><div><dt>Status</dt><dd><span class="demo-status demo-status-${t.status}">${t.status}</span>${t.durationMs != null ? ` · ${t.durationMs} ms` : ""}</dd></div><div><dt>Output</dt><dd><pre class="demo-output">${escapeHtml(out)}</pre></dd></div></dl>`;
  }

  // ---- replay of the real run ----------------------------------------------
  const replayBtn = root.querySelector(".demo-replay");
  const status = root.querySelector(".demo-run-status");
  const inputEl = root.querySelector(".demo-run-input");
  inputEl.textContent = data.run.input;
  let timer = null;

  function setStatuses(value) {
    for (const k in buttons) buttons[k].dataset.runStatus = value;
  }

  function replay() {
    if (timer) { clearTimeout(timer); timer = null; }
    setStatuses("idle");
    const seq = data.run.sequence;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const stepMs = reduced ? 0 : 700;
    status.textContent = "Running…";
    replayBtn.disabled = true;
    let i = 0;
    const tick = () => {
      if (i > 0) buttons[seq[i - 1]].dataset.runStatus = "succeeded";
      if (i < seq.length) {
        buttons[seq[i]].dataset.runStatus = "running";
        select(seq[i]);
        tab = byId[seq[i]].trace ? "trace" : "node";
        render();
        i += 1;
        timer = setTimeout(tick, stepMs);
      } else {
        status.textContent = `Done in ${(data.run.durationMs / 1000).toFixed(1)} s on ${data.run.model || "the configured model"}. Nodes not on the taken branch stay idle.`;
        replayBtn.disabled = false;
        timer = null;
      }
    };
    tick();
  }
  replayBtn.addEventListener("click", replay);

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  render();
})();
