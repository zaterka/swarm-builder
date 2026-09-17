# swarm-workflow (spike: branching)

Hand-written oracle project for Swarm Builder's Group 0 codegen spike.

Branching workflow using a `decision` node with `builder.match(...).to(...)`
branches (PLAN.md fact 14). Proves the emission-order rule: the complete
`.branch(...)` chain must be built and rebound (Decision is immutable)
**before** the `add_edge` that targets that decision, or `build()` fails
with `GraphValidationError: The following nodes have no outgoing edges`.

`classify` -> `decision` -> (`big` | `small`) -> end.

## Run the validation gate

```bash
export UV_CACHE_DIR=/Users/pedro.zaterka/factored-projects/swarm-builder/.uv-cache
uv sync
uv run python -c "import swarm_workflow.graph"
uv run python validate/dry_run.py
```

All three must succeed with **no API key and no AWS credentials present**
(this project has no agent step, so the point is moot here, but the gate
is run identically to the other spikes for consistency).
