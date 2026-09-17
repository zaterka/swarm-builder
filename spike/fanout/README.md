# swarm-workflow (spike: fanout)

Hand-written oracle project for Swarm Builder's Group 0 codegen spike.

Proves fact 13: plain multi-successor fan-out is **silently lossy**
(returns one arbitrary branch), and real fan-in requires an explicit
`builder.join(reduce_list_append, initial_factory=list)`.

`split` fans out to `left` and `right`, both of which feed an explicit
`join` node using `reduce_list_append`. `build()` also injects a
synthetic `split_broadcast_fork` node -- recorded here and in
`FINDINGS.md` -- so `graph.nodes` keys are **not** exactly the canvas
node set.

`split` -> (fork) -> `left`, `right` -> `join` -> end

## Run the validation gate

```bash
export UV_CACHE_DIR=/Users/pedro.zaterka/factored-projects/swarm-builder/.uv-cache
uv sync
uv run python -c "import swarm_workflow.graph"
uv run python validate/dry_run.py
```

All three must succeed with **no API key and no AWS credentials present**.
