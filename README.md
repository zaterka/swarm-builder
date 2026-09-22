<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img src="assets/logo.svg" alt="Swarm Builder" width="360">
  </picture>
</p>

<h3 align="center">Build applications, not PoCs.</h3>

<p align="center">
  A visual builder for agent workflows that <strong>compiles to a Python project you own</strong> —
  on <a href="https://ai.pydantic.dev/graph/">pydantic-graph</a> / PydanticAI, or
  <a href="https://docs.langchain.com/oss/python/langgraph/overview">LangGraph</a> —
  validated end to end before you ever see it.
</p>

<p align="center">
  <a href="https://zaterka.github.io/swarm-builder/">Website</a> ·
  <a href="https://zaterka.github.io/swarm-builder/#example">Interactive example</a> ·
  <a href="docs/guide.md">Guide</a> ·
  <a href="docs/api.md">HTTP API</a> ·
  <a href="docs/architecture.md">Architecture</a>
</p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-2b6098">
  <img alt="pydantic-graph 2.43" src="https://img.shields.io/badge/pydantic--graph-2.43-2b6098">
  <img alt="LangGraph 1.2" src="https://img.shields.io/badge/langgraph-1.2-1c6b3a">
  <img alt="Local-first" src="https://img.shields.io/badge/runs-locally-8a5a08">
</p>

---

Most visual builders make you a tenant of their runtime: you draw a flow and get a JSON
document only that product can execute. Swarm Builder inverts this. The canvas is an
**editor for a Python project**. The graph document is validated against a schema the server
owns, the project is emitted from templates, a coding model fills only the marked body
regions, and a boundary check proves it touched nothing else. The canvas is where you think;
the repository is what you ship.

<p align="center">
  <a href="https://zaterka.github.io/swarm-builder/#example">
    <img src="assets/canvas.png" alt="A support-triage workflow on the canvas after a run: classifier, decision, two agents, join and reply, with the taken branch marked as run" width="900">
  </a>
  <br>
  <sub>The triage graph from the interactive example, after one real run. Click through it on the <a href="https://zaterka.github.io/swarm-builder/#example">website</a>.</sub>
</p>

## What you get

| | |
|---|---|
| **Describe → workflow** | Write a paragraph. The model drafts nodes and edges; deterministic code derives ids, decision branches, fan-out wiring, state fields and layout; the same reviewer that gates a compile checks the draft and feeds errors back. You land on an editable canvas. |
| **Run on the canvas** | Execute the compiled project with your credentials and watch it node by node: live status on each node, per-node inputs and outputs, final state, run history. Edit and run again; a stale project recompiles first, in the same stream. |
| **Compile to PydanticAI** | Five phases: review → scaffold → fill → boundary → validate. Nothing is written until review passes. The output is a project with `pyproject.toml`, pinned dependencies, and its own validation gate. |
| **Export to LangGraph** | Four more phases convert the *validated* project into a LangGraph export: pure LangGraph orchestration, LangChain only for model calls, messages and tools. Verified the same way. |
| **Validated before you see it** | Every compile passes `uv sync`, a keyless import, a rendered-diagram golden, a node-set assertion, and a dry run with a test model, with every credential variable stripped. |
| **Nothing to import back** | The export has no dependency on Swarm Builder. Delete the builder; the project still syncs, imports and runs. |

The generated project, in full:

```
workspace/projects/<graphId>/
  pyproject.toml            pinned pydantic-ai-slim[...]==2.43.0, pydantic-graph==2.43.0
  .python-version
  .env.example              the inherited model route, never the key
  README.md
  src/swarm_workflow/
    state.py                @dataclass State from your stateFields
    deps.py                 the model seam, resolved from env
    graph.py                GraphBuilder wiring — generated, never model-written
    steps/<id>.py           one step per node; the model wrote only the marked body
    agents/<id>.py          one agent factory per agent node
  validate/dry_run.py       the project's own gate
  run/stream_run.py         the per-node tracer the Run button executes
```

## Quickstart

Requirements: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). The frontend bundle is
committed, so there is no build step.

```bash
git clone https://github.com/zaterka/swarm-builder
cd swarm-builder
export UV_CACHE_DIR="$(pwd)/.uv-cache"   # a writable uv cache; the compile gate inherits it
uv sync
uv run swarm-builder
# Swarm Builder listening at http://127.0.0.1:8420
```

**Model route.** Swarm Builder reads a [DeepSeek Harness](https://github.com/deepseek-ai)
`settings.yaml` when present, or `SWARM_MODEL` (a PydanticAI known name such as
`openai:<model>`, `anthropic:<model>`, `deepseek:<model>`, `bedrock:<model>`, or a bare id plus
`SWARM_BASE_URL` for an OpenAI-compatible endpoint). Put it and your provider key in a `.env`
at the repo root; it is loaded at startup. Copy `.env.example` to begin.

**No credentials yet?** `SWARM_FAKE_FILL=1 SWARM_FAKE_GENERATE=1 SWARM_RUN_TEST_MODEL=1`
runs the entire pipeline — generate, compile, both targets, run — with deterministic stubs.

Docker: `docker compose up --build` (see the [guide](docs/guide.md#run-with-docker)).

## Open source and managed

| | Open source (this repository) | Swarm Builder Cloud |
|---|---|---|
| Status | Available now | **Planned.** [Tell us you're interested](https://github.com/zaterka/swarm-builder/issues/new?title=Interested%20in%20Swarm%20Builder%20Cloud&labels=cloud) |
| Where it runs | Your machine or your infrastructure, bound to loopback by default | Hosted canvas and compile service |
| Model routes | Bring your own keys; harness settings inherited | Bring your own keys, or managed routes |
| Exports | PydanticAI and LangGraph projects, identical in both | Same exports; the code you get is never different |
| Team features | — | Shared graphs, run history across a team, review workflow, CI hooks for exports |
| Support | GitHub issues | Support with response times |

The promise that does not change between the two: **what you export is a plain Python
project on open frameworks, with no dependency on us at run time.** A managed service hosts
the *builder*, never your workflow.

## How it works, briefly

```
paragraph ──► draft (model) ──► materialize + review (deterministic) ──► canvas
canvas ──► review ──► scaffold ──► fill (model, marker regions only) ──► boundary ──► validate ──► project
project ──► [langgraph] lg_scaffold ──► lg_convert (model) ──► lg_boundary ──► lg_validate ──► export
project ──► run (subprocess, your credentials) ──► per-node trace on the canvas
```

Two structural properties drive the design. **Compilation is template-first:** the server
emits every structural file itself and asks the model only for step bodies, so a model
mistake cannot corrupt graph structure. **The model never executes code:** its tools read
files, write one marker region, and parse; the only thing that runs generated code is the
validation gate in a subprocess, keyless — or the Run button, with your consent and your
keys. Details: [architecture](docs/architecture.md).

## Documentation

| Document | What it covers |
|---|---|
| [`docs/guide.md`](docs/guide.md) | The full manual: model configuration, every compile phase, Run, Describe, the LangGraph target, guarantees, troubleshooting |
| [`docs/api.md`](docs/api.md) | The HTTP API and the SSE stream contract |
| [`docs/architecture.md`](docs/architecture.md) | How the pieces fit, and why |
| [`PLAN.md`](PLAN.md) · [`PLAN-V2-FEATURES.md`](PLAN-V2-FEATURES.md) | Design records: probe facts, rejected alternatives, acceptance criteria |

## Contributing

```bash
uv run pytest -m "not slow"     # fast suite
uv run pytest                   # includes real uv sync + dry runs of generated projects
uv run ruff check src tests
pnpm --dir web install && pnpm --dir web test && pnpm --dir web build   # only when editing web/src
```

`web/dist` is committed; `uv run python scripts/check_dist.py` warns when it is stale.

## Status and license

Swarm Builder is early software under active development; the graph document is versioned
(`version: 1`) so future shape changes ship as migrations. A `LICENSE` file has not been added
yet; until it lands, no license is granted. Not affiliated with Pydantic, LangChain, n8n or
Langflow.
