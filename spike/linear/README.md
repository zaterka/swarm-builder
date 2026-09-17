# swarm-workflow (spike: linear)

Hand-written oracle project for Swarm Builder's Group 0 codegen spike.

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
