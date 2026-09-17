# swarm-workflow (spike: linear, custom-baseURL model path)

Hand-written oracle project for Swarm Builder's Group 0 codegen spike.

Identical wiring to `spike/linear/`, but exercises the **other** model
emission path from PLAN.md fact 22: a custom `baseURL` route that is not
a `KnownModelName`, built structurally as `OpenAIChatModel(id,
provider=OpenAIProvider(base_url=..., api_key=...))` instead of a plain
prefixed string. See `src/swarm_workflow/deps.py`.

Linear workflow: `intake` -> `research` (agent, built from `ctx.deps.model`)
-> `summarize` -> end.

## Run the validation gate

```bash
export UV_CACHE_DIR=/Users/pedro.zaterka/factored-projects/swarm-builder/.uv-cache
uv sync
uv run python -c "import swarm_workflow.graph"
uv run python validate/dry_run.py
```

All three must succeed with **no API key and no AWS credentials present**.
