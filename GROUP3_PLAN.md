# Group 3 — Server Core — Implementation Plan

> Sub-plan for `PLAN.md`'s Group 3, written by the Group-3 subagent. No
> human interviewer was reachable in this delegated session
> (`ask_user_question` errored: "human interaction is unavailable while
> the calling agent is owned by another live agent"), so every open
> question below is resolved with a documented decision + rationale
> instead of being asked, and is re-reported at the end of the work for
> the delegating agent / developer to override if wrong. This mirrors
> the precedent set by Group 2's `GROUP2_PLAN.md` decision 8.

## Goal

Ship `main.py` (real FastAPI app), `routes/{health,graphs,templates,
llm_routes,export}.py`, `inherit/{settings,routes}.py`, `store/{graphs,
projects}.py`, small additive changes to `config.py` if genuinely needed
(currently: none needed — reviewed and confirmed `config.py` already
exposes everything Group 3 needs), the OpenAPI type-generation seam, and
the Group-3 test suite (`tests/test_inherit_settings.py`,
`tests/test_inherit_routes.py`, `tests/test_store.py`,
`tests/test_api_*.py`). Explicitly defined-but-not-implemented: compile
routes (`POST /api/graphs/:id/review`, `POST /api/compile` and friends) —
Group 4/2 own the pipeline; Group 3 only wires the HTTP seam with a
clear "not yet wired" response.

## Decisions (resolved without a human interview)

1. **`/api/models` "each route's source" (PLAN.md HTTP API table).**
   There is exactly one place routes are read from — the user's
   `settings.yaml` — so "each route's source" cannot mean "which file did
   this route come from." Read literally alongside the edge case "A
   harness route settings cannot enumerate... the dropdown shows the
   route with no models," the only information worth reporting per-route
   is whether its `models` list is explicit or absent (fact 21). Decision:
   - The top-level **resolved default** carries a `source` field: one of
     `"graph-override"`, `"settings-default"` (`agent-default-model`),
     `"env-fallback"` (`SWARM_MODEL`), `"bundle-default"` (no settings,
     no env — the `deepseek-official`/`deepseek-v4-flash` PLAN.md edge
     case). This is the literal, unambiguous "resolved pair plus its
     source" from acceptance criterion 2 and I5.
   - Each enumerated **route** carries `has_explicit_models: bool` (true
     iff its settings entry declared a `models:` list) rather than a
     redundant "source" string, since a route's provenance is always
     "read from settings.yaml" — reporting that per-route would be
     noise. Documented here as the resolution of an underspecified plan
     line; flagged again in the final report.

2. **OpenAPI-generated frontend types, with no `web/` yet.** Add
   `scripts/generate_web_types.py`: imports the FastAPI `app` from
   `swarm_builder.main`, dumps its OpenAPI schema to JSON, and (if `npx`
   is available) shells out to `npx --yes openapi-typescript@7 -` to
   produce TypeScript. No new Python dependency, no `pyproject.toml`
   edit. Since `web/src/` does not exist yet (Group 6), the script writes
   to `web/src/types.ts` by creating the directory if absent — this is
   exactly the "sensible path, documented" the task allows. The script is
   also runnable as `uv run python scripts/generate_web_types.py`, and
   its command is documented in a new `## Frontend types` README section.
   I will actually run it once during this task and commit the output
   (or report if `npx`/network makes that impossible in this sandbox).

3. **`inherit/routes.py` return-shape vs. Group 2's `ResolvedModel`.**
   Group 2's `compile/__init__.py` defines `ResolvedModel` as a *neutral
   leaf* dataclass (pre-rendered source fragments: `helper_source`,
   `default_factory_name`, `extra_imports`, `pyproject_extras`,
   `env_lines`, `readme_model_note`) that `scaffold.py` splices verbatim,
   never branching on provider/protocol. `inherit/routes.py` must
   produce **two** things per the task brief:
   (a) a **live model object/spec** for the in-process compile agent
       (Group 5's concern, not built here, but the shape must exist so
       Group 5 can consume it with no rework) — a `LiveModel` dataclass
       wrapping `model: Model | str` plus metadata (`pyproject_extras`,
       `source_description`) for the compile agent's own Deps-equivalent;
   (b) the **emission info** the scaffolder needs — a function
       `to_resolved_model(selected: EffectiveModel) -> ResolvedModel`
       that imports Group 2's `swarm_builder.compile.ResolvedModel` and
       constructs it (this is the one place Group 3 imports Group 2's
       neutral leaf; Group 2 never imports `inherit/`, preserving the
       one-directional seam GROUP2_PLAN.md decision 1 sets up).
   Both are exposed from `inherit/routes.py`; `main.py`/`routes/
   llm_routes.py` use (b) for `/api/models` reporting and will hand (b)'s
   output to Group 4's pipeline once it exists (documented as the
   seam). This satisfies "design `routes.py` to be adaptable to that
   shape and report the shape you produced."

4. **Settings read-per-call, no caching (hard requirement, explicit
   test).** `inherit/settings.py` exposes `read_settings(dsh_home: Path)
   -> Settings | None`, called fresh on every invocation — no
   module-level cache, no `functools.lru_cache`. `main.py`/route handlers
   call `get_dsh_home()` (from `config.py`, unchanged) then
   `read_settings(...)` per request. This is what the "mutate the
   fixture between calls" test exercises directly.

5. **Route/settings schema modeled with pydantic, mirroring `models.py`'s
   convention** (own small module-local models in `inherit/settings.py`,
   not reusing `SwarmBaseModel` since these are not part of the graph
   document and must tolerate **unknown top-level keys** in
   `settings.yaml` — real files have unrelated sections like
   `ui-onboarding`, `dev-mode-pipeline`, etc., per the real file read
   during planning). Decision: `extra="ignore"` for the settings-parsing
   models (opposite of `models.py`'s `extra="forbid"`), because this is
   parsing a document Swarm Builder does not own and must not reject for
   sections irrelevant to model inheritance (real `~/.dsh/settings.yaml`
   confirmed to have `ui-onboarding`, `agent-presets`, `ui-theme`,
   `dev-mode-pipeline` alongside `llm-pi-ai`/`agent-default-model`).

6. **Bundle default value.** PLAN.md edge cases and PLAN.md's Group-2
   `ResolvedModel`/`default_scaffold_model()` disagree on the literal
   fallback: PLAN.md's edge-case prose says `deepseek-official` /
   `deepseek-v4-flash` when there is no `agent-default-model` section at
   all; Group 2's own `default_scaffold_model()` (built before Group 3
   existed, explicitly "a reasonable literal fallback... though the real
   pipeline's choice of fallback is Group 3/4's call, not fixed there")
   hardcodes `bedrock:us.anthropic.claude-opus-5`. Decision: Group 3's
   `inherit/settings.py` follows **PLAN.md's written text** exactly
   (`deepseek-official` / `deepseek-v4-flash`, PydanticAI known-name
   `deepseek:deepseek-v4-flash`, requiring the `[openai]` extra per fact
   23's "deepseek-official... needs `pydantic-ai-slim[openai]`" — actually
   checking fact 22's table: `deepseek-official / deepseek-v4-flash` row
   states requirement `pydantic-ai-slim[openai]` + API key). This
   supersedes Group 2's own placeholder for the real pipeline, exactly as
   Group 2 anticipated ("Group 3/4's call, not fixed here"). Flagged in
   the final report since it is a genuine PLAN.md internal inconsistency,
   not something I should silently paper over.

7. **`amazon-bedrock`/no explicit `api` key → `bedrock-converse-stream`.**
   Per the task brief's explicit instruction: a route with no `api` key
   but with `awsProfile`/`awsRegion` present is inferred as protocol
   `bedrock-converse-stream`. Implemented as an explicit rule in
   `inherit/settings.py`'s route parsing, not a silent default — a route
   with neither `api` nor `awsProfile`/`awsRegion` is a genuine "cannot
   determine protocol" case, reported as `api=None` (never guessed) so
   `inherit/routes.py`'s refusal path is honest about why it refused.

8. **`anthropic-messages` needs the `anthropic` extra to even *import*
   the mapping class** (re-probed: `from pydantic_ai.models.anthropic
   import AnthropicModel` raises `ImportError` with no `anthropic`
   package installed — this project's own `pyproject.toml` only declares
   `[bedrock,openai]`). Consequence: `inherit/routes.py`'s "live model
   object" path for an `anthropic-messages` route **lazily imports**
   `AnthropicModel` only when actually resolving that specific route
   (never at module import time), so every other route keeps working
   with `swarm-builder`'s own installed extras; if the import itself
   fails, that surfaces as the *same* "route has no usable PydanticAI
   counterpart" refusal message as an unmappable protocol, naming the
   route and explaining the missing extra is on the **server's own**
   dependency set, not the generated project's. This is a real
   consequence of fact 30/23 the plan does not spell out explicitly (see
   final report "underspecified" section) — reported clearly rather than
   silently degraded.

9. **`env_lines` for the known-name and bundle-default paths.** Every
   spike `deps.py` example has an **empty** `.env.example` — the
   convention is that `.env.example` documents the override variables
   generically (already done once, project-wide, by `scaffold.py`'s own
   emission — checked: `scaffold.py`'s `_render_env_example` just joins
   `resolved_model.env_lines`, and no spike ever populates them). Decision:
   `inherit/routes.py` emits `env_lines=()` for the known-name path
   (matches `spike/linear`), and a **populated** `env_lines` tuple only
   for the custom-`baseURL` path documenting `SWARM_BASE_URL`/
   `SWARM_API_KEY_ENV` (matches nothing existing today, but is a genuine
   improvement over the spike since the custom-baseURL project's
   `.env.example` in the spike is also empty — flagged as a spike gap in
   final report; I will follow the *documented convention* — README
   already documents these vars generically — and leave `env_lines=()`
   for parity with what Group 2's scaffold contract expects, to avoid
   inventing a per-route emission convention Group 2 never tested against).
   Net decision: `env_lines=()` always; the README's existing generic
   `.env.example`-adjacent doc (Configuration table) already covers this,
   and `_render_readme`'s `readme_model_note` is where the specific route
   is named.

10. **Health-check `uv` availability check.** `shutil.which("uv")` — a
    presence check only, not a version or writability check (PLAN.md
    "Edge cases": "`uv` missing from `PATH`" is the exact failure mode
    named; writability of `UV_CACHE_DIR` is a *different*, separately
    reportable field `uv_cache_writable: bool`, checked with a cheap
    `os.access(dir, os.W_OK)` after `mkdir(parents=True, exist_ok=True)`
    inside a `try`, since PLAN.md's failure-mode table also separately
    lists "Disk full / unwritable workspace."

11. **`DELETE /api/graphs/:id?project=1`.** "remove graph (keeps
    generated project unless `?project=1`)" — implemented as a FastAPI
    query parameter `project: bool = False`; when true, also removes
    `workspace/projects/<graphId>/` via `store/projects.py`'s lifecycle
    API. Deleting a graph whose project does not exist is not an error
    (idempotent).

12. **Static frontend serving degradation.** `web/dist/` does not exist
    yet (Group 6). `main.py` checks for it at **request time** (not
    import time, so the server itself doesn't crash before Group 6 lands
    and so a `pnpm build` run later takes effect without a server
    restart requirement beyond normal static-file semantics) via
    FastAPI's `StaticFiles` mounted conditionally, plus a root `GET /`
    fallback that returns a small helpful JSON/HTML message ("frontend
    not built yet; run `pnpm --dir web build`, or start the dev server
    separately") when `web/dist/index.html` is absent. Decision: mount
    `StaticFiles(directory=web/dist, html=True)` at `/` only if the
    directory exists at server-start time (checked once at app
    construction, which is fine — Group 6 landing requires a server
    restart to pick up the new directory, which is normal for any static
    file server and does not violate "printed URL works after refresh"
    since Group 6 will restart the server as part of its own work). If
    absent, mount nothing at `/` and instead serve the helpful message
    there, so `/api/*` keeps working unconditionally either way.

13. **Live-settings end-to-end proof.** Task's Definition of Done requires
    actually starting the server and curling `/api/health` +
    `/api/models` against the user's **real** `~/.dsh/settings.yaml`
    (`DSH_HOME` unset → defaults to `~/.dsh` per `config.py`). This is
    read-only; no write path in this module ever touches
    `~/.dsh/settings.yaml`. I will bind `127.0.0.1` on an ephemeral or
    fixed high port, curl both endpoints, capture output verbatim in the
    final report, then shut the server down (background job + `job_kill`,
    or a short-lived foreground run with a timeout).

14. **Concurrency with Group 2.** Group 2's files
    (`templates/`, `compile/`) are read-only inputs to my work
    (`routes/templates.py` imports `templates.registry` **lazily inside
    the handler function**, per the task's explicit instruction, so a
    transient Group-2-in-progress state never breaks server import).
    `routes/graphs.py`/`store/graphs.py` never import anything from
    `compile/`. I re-ran the full existing suite before starting (76
    passed, 3 failed — the 3 failures are Group 2's own known-in-progress
    `joinless_fanout` negative-fixture gap, entirely inside `review.py`
    logic I do not touch) and will re-run it again at the end to confirm
    I introduced zero regressions and that Group 2's own suite state is
    unaffected by my additions.

## Revisions after `planReview` (NO-GO → addressed below, then proceeding)

The review subagent found 12 blockers and 11 nits, all concrete and
correct (it re-probed against the real `~/.dsh/settings.yaml` and the
installed venv). Resolutions, each keyed to the finding number:

**1+2 (route-prefix table + baseURL-wins precedence, fixing a real
mis-route).** Added an explicit static table and a fixed decision
procedure to `inherit/routes.py`, replacing decisions 3/6/7's hand-wave:

```python
_KNOWN_ROUTE_PREFIXES = {
    "amazon-bedrock": "bedrock", "bedrock": "bedrock",
    "deepseek-official": "deepseek", "deepseek": "deepseek",
    "anthropic": "anthropic", "openai": "openai",
}
_PROTOCOL_EXTRAS = {
    "openai-completions": ("openai",), "openai-responses": ("openai",),
    "anthropic-messages": ("anthropic",), "bedrock-converse-stream": ("bedrock",),
}
_PREFIX_EXTRAS_FALLBACK = {  # used only when no `api` is known (env/bundle path)
    "bedrock": ("bedrock",), "deepseek": ("openai",),
    "anthropic": ("anthropic",), "openai": ("openai",),
}

def build_live_model(effective: EffectiveModel) -> LiveModel:
    base_url = effective.base_url or (effective.route.base_url if effective.route else None)
    if base_url is not None:
        # baseURL ALWAYS wins over known-name membership (finding 2: the
        # real kornerstone route's model id "deepseek-v4-flash" IS a
        # KnownModelName, but this is a self-hosted endpoint, not the
        # official DeepSeek API -- id-based dispatch would silently
        # mis-route it).
        api = effective.route.api if effective.route else None
        if api is not None and api not in ("openai-completions", "openai-responses"):
            raise UnmappableRouteError(effective.provider, api)
        ... construct OpenAIChatModel(effective.model, provider=OpenAIProvider(base_url=base_url, api_key=...))
        return LiveModel(..., pyproject_extras=("openai",))
    prefix = _KNOWN_ROUTE_PREFIXES.get(effective.provider)
    if prefix is None:
        raise UnmappableRouteError(effective.provider, effective.route.api if effective.route else None)
    api = effective.route.api if effective.route else None
    extras = _PROTOCOL_EXTRAS.get(api, _PREFIX_EXTRAS_FALLBACK.get(prefix, ("openai",)))
    return LiveModel(model=f"{prefix}:{effective.model}", pyproject_extras=extras, ...)
```

Route resolution is therefore **provider-key-scoped, never id-scoped**
(review's own framing), and a route declaring `baseURL` with a
non-openai `api` is refused rather than guessed.

**3+4 (env-fallback base_url carrier + route-less behavior).**
`EffectiveModel` gains first-class `base_url: str | None` and
`api_key_env: str | None` fields (populated from `SWARM_BASE_URL`/
`SWARM_API_KEY_ENV` for `source="env-fallback"`, or from the matched
`RouteConfig` for `"settings-default"`/`"graph-override"`, or left `None`
for `"bundle-default"`). `SWARM_MODEL` splitting rule, stated explicitly:
if `SWARM_BASE_URL` is set, `SWARM_MODEL` is treated as a **bare model
id** (provider displayed as `"custom"` for reporting); otherwise
`SWARM_MODEL` is split on the **first** `:` into `provider`/`model` for
*display* purposes only (`"bedrock:us.anthropic.claude-sonnet-5"` ->
`provider="bedrock"`) — the compile-agent path never re-joins these,
it hands the original `SWARM_MODEL` string straight to `build_live_model`
via the same `_KNOWN_ROUTE_PREFIXES` self-mapping entries (`"bedrock":
"bedrock"`, etc.), so a prefix nobody in `_KNOWN_ROUTE_PREFIXES`
recognizes (e.g. `gateway/bedrock:...`, per the review's own example)
correctly raises `UnmappableRouteError` rather than being silently
mishandled. Both `build_live_model` and `to_resolved_model` now handle
`route is None` explicitly (env-fallback and bundle-default paths both
exercised by dedicated tests).

**5 (health must express "no route resolvable" even though the bundle
default always resolves *something*).** `/api/health` adds
`compile_ready: bool` and `blockers: list[str]`. `source ==
"bundle-default"` is unconditionally a blocker (message names both
remediations: configure `$DSH_HOME/settings.yaml`'s `agent-default-model`,
or set `SWARM_MODEL`) — this is deliberately stricter than "resolution
technically succeeded," because PLAN.md's failure-mode row 1 and the "No
providers configured" edge case both want *this exact state* surfaced as
not-ready, not silently compile-eligible. Additional blockers: `uv`
absent; `workspace_writable` false; `settings_error` present (see
finding 11).

**6 (bundle-default divergence with Group 2's own placeholder).**
Confirmed: Group 2's `default_scaffold_model()` hardcodes
`bedrock:us.anthropic.claude-opus-5` and is asserted by Group 2's own
tests (untouchable). Group 3's `inherit/` produces
`deepseek:deepseek-v4-flash` / `pyproject_extras=("openai",)` for
`source="bundle-default"`, per PLAN.md's literal edge-case text — a
**documented, tested divergence**, not an oversight; flagged again in
the final report for the orchestrator/developer to reconcile (likely by
updating Group 2's placeholder's docstring, since Group 2 already
anticipated "Group 3/4's call, not fixed here").

**7 (path-safety for graph/project ids).** New `store/_ids.py`:
`GRAPH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")`,
`validate_graph_id(id) -> None` (raises `InvalidGraphIdError`, mapped to
HTTP 422 in `routes/graphs.py`), used at the **top of every** `store/
graphs.py` and `store/projects.py` function — not only at the route
layer — plus a defense-in-depth `Path.resolve()` +
`is_relative_to(workspace_root)` guard immediately before any write,
read, or `rmtree`. `?project=1` deletion goes through this same guard
before ever calling `shutil.rmtree`.

**8 (atomic-write contract, spelled out).** Temp file created via
`tempfile.NamedTemporaryFile(dir=graphs_dir, delete=False)` (same
directory as the destination, so `os.replace` never crosses a
filesystem boundary even when `SWARM_WORKSPACE` points elsewhere);
`f.flush(); os.fsync(f.fileno())` before `close()`; `os.replace(tmp,
dest)`; temp path unlinked in a `finally` on any exception before
`replace` runs. `OSError` (disk full, permission denied) during any
store operation -> HTTP 500 with `detail` naming the resolved path
(matches the failure-mode table's exact wording). Concurrency posture
stated: last-writer-wins via atomic replace is the accepted 800 ms
autosave semantics (matches PLAN.md's frontend section; no locking
needed for a single-user local tool per Assumption 8).

**9 (on-disk serialization contract).** All graph JSON writes use
`graph.model_dump(mode="json", by_alias=True)` -> `json.dumps(...,
indent=2)`, matching `models.py`'s explicit "serialization always emits
the camelCase alias" contract. The CRUD round-trip test asserts the
on-disk file's top-level keys are camelCase (`entryNodeId`, not
`entry_node_id`), not merely that re-validation succeeds.

**10 (PUT semantics).** Path `id` is authoritative: a body `id` that
disagrees with the path parameter is rejected with 422 naming both
values (never silently overwritten or silently accepted). `updated_at`
is **always server-stamped** to `datetime.now(UTC)` on every successful
`PUT`, overwriting whatever the client sent (the client can send any
placeholder; `models.py` still requires the field to be present and
valid). The round-trip test's assertion is restated precisely:
"preserves every field except `updatedAt`, which the server stamps."

**11 (malformed/unreadable settings.yaml).** `read_settings` catches
`yaml.YAMLError` and `OSError` (permission denied) separately from
"file does not exist" (`FileNotFoundError`, the only case that returns
plain `None`). A parse/read failure returns `Settings` with empty
`routes`, `agent_default_model=None`, **and** a new
`Settings.error: str | None` field naming the path and the underlying
exception message — surfaced verbatim in both `/api/health`
(`settings_error`) and `/api/models`. Dedicated test: a fixture with a
YAML syntax error produces a distinct, reportable state from "no file at
all."

**12 (main.py entry-point specifics).** `run()`: prints
`f"Swarm Builder listening at http://127.0.0.1:{port}"` **before**
calling `uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")`
(uvicorn blocks, so the print must precede it). `OSError` from a bound
port (`EADDRINUSE`) is caught around the call and re-raised after
printing a message naming the `PORT` env var as the fix — no
auto-increment (deterministic beats clever).

**13 (per-route `/api/models` fields, beyond `has_explicit_models`).**
Each enumerated route additionally reports `emission: "known-name" |
"structural" | "unmappable"`, `required_extra: str | None` (first of
`pyproject_extras`, or `None`), and `unmappable_reason: str | None` —
computed by a new `classify_route(route: RouteConfig) -> RouteEmission`
in `inherit/routes.py` that runs the same baseURL/prefix decision
procedure at the *route* level (independent of any specific model id
within it), so the picker can warn "this route needs the `anthropic`
extra" or "this route is unmappable" before a compile is attempted —
satisfying facts 21 **and** 22 **and** 23 together, as the review
recommended, not just fact 21.

**14 (settings-read-per-call test, made HTTP-level).** Added: a
`TestClient`-level test that calls `GET /api/models`, rewrites the
fixture `settings.yaml` on disk, calls `GET /api/models` again, and
asserts the payload changed — this is the test that would actually catch
an `app.state` snapshot or a module-level cache, which a direct
`read_settings()` unit test cannot. A second test flips `DSH_HOME` via
`monkeypatch.setenv` between two requests to the same running app and
asserts the response follows. **Explicit prohibition, enforced by code
review during implementation**: no settings value is ever stored on
`app.state` or in a module-level global; `get_dsh_home()` +
`read_settings()`/`resolve_effective_model()` are called fresh inside
every request handler.

**15 (compile-seam response contract).** Every compile-pipeline route
(`POST /api/graphs/:id/review` is **excluded** — see finding 6/11 below,
it is implemented for real) returns HTTP `501` with a single shared body
shape `{"detail": "<message>", "not_yet_wired": true, "owner": "Group 4"}`
declared via `responses={501: {...}}` in the route decorator and **no**
success `response_model`, so Group 6's OpenAPI-generated types never see
a placeholder success schema that would break when Group 4 lands. One
test per compile-seam path asserts the 501 + body shape.

**6/11 (implement `POST /api/graphs/:id/review` for real).** Confirmed
Group 2 already ships `compile/review.py`'s `review(graph) ->
ReviewResult`, and its own docstring names this exact route as an
intended caller. This is pure static validation, not "the pipeline" —
implemented in `routes/graphs.py` via a **lazy** `from
swarm_builder.compile.review import review` inside the handler (matches
the isolation posture already used for `templates.registry`), returning
`{"errors": [...], "warnings": [...], "ok": bool}` (mirroring
`ReviewResult`'s own shape via a small response model). Only the
job-oriented `/api/compile*` family (which needs Group 4's
`pipeline.py`/`jobs.py`) stays behind the 501 seam.

**16 (bundle-default test).** Added: a dedicated test asserting Group
3's own `to_resolved_model()` for `source="bundle-default"` produces
`deepseek:deepseek-v4-flash` / `("openai",)` — pinning the documented
divergence from Group 2's placeholder so a future edit notices if either
side drifts.

**17 (Group 3 → Group 4 contract for fact-30's static extras-sufficiency
assertion).** Documented, not coded (would require editing Group
2's `compile/__init__.py`, off-limits): Group 4's `pipeline.py`, when it
resolves a model via `inherit/routes.py`, must keep the `EffectiveModel`/
matched `RouteConfig` object itself (not just the `ResolvedModel` it
hands to `scaffold.py`), because `RouteConfig.api` is what
`validate.py`'s static "does the emitted `pyproject.toml` declare the
right extra" assertion (fact 30) needs independently of whatever
`scaffold.py` already emitted. Restated in the final report as an
explicit seam for Group 4.

**18 (`reasoning_effort` reachability).** Added
`reasoning_effort: str | None = None` to `LiveModel` (threaded through
from `EffectiveModel`), documented as reported-only for v1 — Group 5
decides how/whether to apply it (e.g. via PydanticAI `ModelSettings`).

**19 (undefined response shapes).** Specified now:
- `GET /api/graphs` -> `{"graphs": [GraphSummary...], "errors":
  [{"id": str, "detail": str}]}` where `GraphSummary` is `{id, name,
  updatedAt, nodeCount, edgeCount}` (camelCase via `SwarmBaseModel`
  subclassing — a new class in a Group-3-owned file, not an edit to
  `models.py`). A corrupt/unparseable graph file is **skipped** from
  `graphs` and reported in `errors`, so one bad file never 500s the
  whole listing.
- `GET /api/templates` -> `[{id, label, description, defaultTools,
  requiredEnv}]`, exactly PLAN.md's five named fields — `deps`/
  `file_manifest` (Group 2's internal scaffolding concerns) are not
  exposed.
- `GET /api/graphs/:id/export` -> 404 naming the expected project path
  when the project was never compiled; on success, `{"projectPath": str,
  "runCommand": str}` where `runCommand` is a single copy-pasteable line
  including the explicit `UV_CACHE_DIR=<resolved>` prefix (fact 10):
  `f"cd {path} && UV_CACHE_DIR={uv_cache_dir} uv sync && UV_CACHE_DIR={uv_cache_dir} uv run python validate/dry_run.py"`.
- All error responses use FastAPI's standard `{"detail": ...}` shape
  (via `HTTPException`) uniformly, except the compile-seam's extended
  501 body (finding 15), so Group 6's client has exactly two error
  shapes to handle, both documented here.

**20 (`workspace_writable`).** Added `workspace_dir: str` and
`workspace_writable: bool` to `/api/health`, checked by `mkdir(parents=
True, exist_ok=True)` + `os.access(dir, os.W_OK)` inside a `try`, mapped
into `blockers` per finding 5.

**21 (hermetic OpenAPI schema test).** Added a test (no `npx`/network
needed) asserting `app.openapi()` builds successfully and that its
`SwarmGraph` component schema's properties are camelCase (`entryNodeId`,
`stateFields`, `updatedAt`, ...) — a real regression guard on the
"one schema, two consumers" invariant that does not depend on Node
tooling being present. The `npx openapi-typescript@7` generation step
stays as a best-effort script run once during this task (documented,
version-pinned in the README command), with its result reported either
way.

**22 (CORS).** `main.py` adds `CORSMiddleware(allow_origin_regex=
r"http://(localhost|127\.0\.0\.1):\d+", allow_methods=["*"],
allow_headers=["*"])` so a separately-run Vite dev server (any port) can
call the API before Group 6's build is wired into static serving.

**23 (stale rationales).** Corrected: the lazy import of
`templates.registry` is kept for module-boundary decoupling (matching
the task's explicit instruction to import Group-2-owned catalogs lazily
and degrade clearly if absent), not because Group 2 is mid-flight — Group
2 has landed and its suite passes. `/api/health` deliberately omits
"`dsh` launch mode" (fact 2 / I6) because fact 2 is `[superseded]` and
must not be implemented — stated explicitly here so it reads as a
decision, not an omission.

## Files to create

```
src/swarm_builder/inherit/__init__.py
src/swarm_builder/inherit/settings.py
src/swarm_builder/inherit/routes.py
src/swarm_builder/store/__init__.py
src/swarm_builder/store/graphs.py
src/swarm_builder/store/projects.py
src/swarm_builder/routes/__init__.py
src/swarm_builder/routes/health.py
src/swarm_builder/routes/graphs.py
src/swarm_builder/routes/templates.py
src/swarm_builder/routes/llm_routes.py
src/swarm_builder/routes/export.py
src/swarm_builder/routes/compile.py          # seam only, not implemented
scripts/generate_web_types.py
tests/test_inherit_settings.py
tests/test_inherit_routes.py
tests/test_store_graphs.py
tests/test_store_projects.py
tests/test_api_health.py
tests/test_api_graphs.py
tests/test_api_models.py
tests/test_api_export.py
tests/fixtures/settings/                     # fixture settings.yaml files
```

Files edited: `src/swarm_builder/main.py` (full replace of the stub).
`config.py` is NOT edited (confirmed everything Group 3 needs already
exists there — `get_dsh_home`, `get_workspace_dir`, `get_port`,
`get_uv_cache_dir`, `get_swarm_model`, `get_swarm_base_url`,
`get_swarm_api_key_env`).

## API surface (module-level, for Group 4/5/6 to build against)

```python
# inherit/settings.py
@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str | None

@dataclass(frozen=True)
class RouteConfig:
    key: str                     # e.g. "amazon-bedrock", "kornerstone"
    api: str | None              # protocol; None if undeterminable
    base_url: str | None
    api_key_env: str | None
    aws_profile: str | None
    aws_region: str | None
    models: tuple[ModelInfo, ...]      # empty when no explicit list (fact 21)

@dataclass(frozen=True)
class AgentDefaultModel:
    provider: str
    model: str
    reasoning_effort: str | None

@dataclass(frozen=True)
class Settings:
    routes: tuple[RouteConfig, ...]
    agent_default_model: AgentDefaultModel | None
    error: str | None = None     # set when settings.yaml exists but failed
                                  # to parse/read (finding 11) -- distinct
                                  # from "file absent" (plain None return)

def read_settings(dsh_home: Path) -> Settings | None: ...
    # None ONLY when settings.yaml is absent entirely (FileNotFoundError).
    # A parse/read failure (YAMLError/OSError) returns Settings(routes=(),
    # agent_default_model=None, error="<path>: <message>") instead of
    # raising or silently returning None. Never caches.

@dataclass(frozen=True)
class EffectiveModel:
    provider: str
    model: str
    reasoning_effort: str | None
    base_url: str | None           # None unless a custom endpoint applies
    api_key_env: str | None
    source: Literal["graph-override", "settings-default", "env-fallback", "bundle-default"]
    route: RouteConfig | None       # the matching RouteConfig; None for
                                    # env-fallback and bundle-default

def resolve_effective_model(
    dsh_home: Path,
    graph_override: ModelSelection | None = None,
) -> EffectiveModel: ...
    # graph override -> agent-default-model -> SWARM_MODEL(+SWARM_BASE_URL)
    # -> bundle default (deepseek:deepseek-v4-flash). Read fresh from disk
    # every call -- no module-level or app.state caching anywhere.

# inherit/routes.py
@dataclass(frozen=True)
class LiveModel:
    model: Model | str            # what compile/agent.py (Group 5) hands to Agent(...)
    pyproject_extras: tuple[str, ...]
    reasoning_effort: str | None
    source_description: str

def build_live_model(effective: EffectiveModel) -> LiveModel: ...
    # baseURL (from route or env) ALWAYS wins over known-name membership
    # (finding 2). Explicitly handles route=None (env-fallback,
    # bundle-default). Raises UnmappableRouteError when api has no
    # PydanticAI counterpart, the provider has no known prefix mapping,
    # or a required extra fails to import (e.g. anthropic-messages
    # without the `anthropic` package -- finding 8/17).

def to_resolved_model(effective: EffectiveModel) -> "swarm_builder.compile.ResolvedModel": ...
    # Group 2 seam: constructs ResolvedModel (helper_source etc.) for
    # scaffold.py. Same baseURL-wins/route=None handling as build_live_model.

@dataclass(frozen=True)
class RouteEmission:
    emission: Literal["known-name", "structural", "unmappable"]
    required_extra: str | None
    unmappable_reason: str | None

def classify_route(route: RouteConfig) -> RouteEmission: ...
    # Route-level (not model-id-level) classification for /api/models's
    # picker (finding 13) -- runs the same baseURL/prefix decision
    # procedure as build_live_model, independent of any specific model id.

class UnmappableRouteError(ValueError):
    def __init__(self, provider: str, api: str | None) -> None: ...
```

## `/api/health` response shape

```json
{
  "version": "0.1.0",
  "dsh_home": "/Users/pedro.zaterka/.dsh",
  "settings_error": null,
  "resolved_model": {"provider": "amazon-bedrock", "model": "us.anthropic.claude-opus-5",
                       "source": "settings-default"},
  "uv_available": true,
  "uv_cache_dir": "/Users/.../.uv-cache",
  "uv_cache_writable": true,
  "workspace_dir": "/Users/.../workspace",
  "workspace_writable": true,
  "web_dist_present": false,
  "compile_ready": true,
  "blockers": []
}
```

`compile_ready` is `false` (with a human-readable entry in `blockers`)
whenever: `resolved_model.source == "bundle-default"` (finding 5 — the
bundle default resolves to *something* but nothing the user actually
configured, and the deepseek fallback needs a key the real environment
does not have — finding 16b); `uv_available` is `false`;
`workspace_writable` is `false`; or `settings_error` is non-null.

## `/api/models` response shape

```json
{
  "routes": [
    {"key": "amazon-bedrock", "api": "bedrock-converse-stream",
     "has_explicit_models": true,
     "emission": "known-name", "required_extra": "bedrock", "unmappable_reason": null,
     "models": [{"id": "us.anthropic.claude-opus-5", "name": "Claude Opus 5 (US)"}, ...]},
    {"key": "kornerstone", "api": "openai-completions", "has_explicit_models": true,
     "emission": "structural", "required_extra": "openai", "unmappable_reason": null,
     "models": [...]}
  ],
  "resolved_default": {"provider": "amazon-bedrock", "model": "us.anthropic.claude-opus-5",
                         "source": "settings-default"},
  "settings_error": null
}
```

## Other route response shapes (finding 19)

- `GET /api/graphs` -> `{"graphs": [{id, name, updatedAt, nodeCount,
  edgeCount}], "errors": [{"id": str, "detail": str}]}` — a corrupt graph
  file is skipped from `graphs` and reported in `errors`, never a 500 for
  the whole listing.
- `GET /api/templates` -> `[{id, label, description, defaultTools,
  requiredEnv}]` — exactly PLAN.md's five named fields; `deps`/
  `file_manifest` are Group-2-internal and not exposed.
- `GET /api/graphs/:id/export` -> 404 (`detail` names the expected,
  never-created project path) when uncompiled; on success
  `{"projectPath": str, "runCommand": str}` where `runCommand` embeds
  the resolved `UV_CACHE_DIR` explicitly (fact 10).
- `POST /api/graphs/:id/review` -> `{"ok": bool, "errors": [...],
  "warnings": [...]}` mirroring `compile.review.ReviewResult` — real
  implementation via lazy import, not part of the not-yet-wired seam.
- Every `/api/compile*` path -> HTTP 501, body `{"detail": str,
  "not_yet_wired": true, "owner": "Group 4"}`, no success
  `response_model` registered.
- All other errors use FastAPI's standard `{"detail": ...}` shape.

## Tests (mirrors task's "Tests" section, expanded per review findings)

- `test_inherit_settings.py`: fixture with/without explicit `models:`;
  precedence chain (override > settings default > env-fallback >
  bundle-default); no-file-at-all (`FileNotFoundError` -> `None`);
  malformed YAML -> `Settings.error` set, distinct from "no file"
  (finding 11); direct-call per-invocation re-read (mutate fixture
  between two `read_settings()` calls).
- `test_inherit_routes.py`: known-name route -> plain string; custom
  `baseURL` -> structural `OpenAIChatModel`/`OpenAIProvider`; **baseURL
  wins over known-name id membership** even when the model id is itself
  a `KnownModelName` (the real `kornerstone`/`deepseek-v4-flash` trap,
  finding 2); each `api` -> correct extra; unmappable protocol/provider
  -> `UnmappableRouteError` naming the route; `route=None` paths
  (env-fallback with/without `SWARM_BASE_URL`, bundle-default) each
  produce a defined `LiveModel`/`ResolvedModel`; bundle-default pins
  `deepseek:deepseek-v4-flash` / `("openai",)` (finding 16);
  `anthropic-messages` route surfaces the missing-extra `ImportError` as
  `UnmappableRouteError`, not a crash (finding 8/17).
- `test_store_graphs.py`: atomic write (temp-in-same-dir + fsync +
  replace, verified via a monkeypatched failure between temp-write and
  replace leaving no partial file); on-disk keys are camelCase (finding
  9); round-trip through `models.py` validation; invalid graph id
  rejected before any filesystem access (finding 7); `OSError` mapped to
  a message naming the resolved path.
- `test_store_projects.py`: create/clear/path-resolution/existence;
  invalid project id rejected before `rmtree`; `rmtree` refuses a
  resolved path outside the workspace root.
- `test_api_health.py`: resolved pair + source for graph-override /
  settings-default / env-fallback / bundle-default; `compile_ready`
  false + blockers populated for bundle-default and for a malformed
  settings fixture; `uv_available`/`workspace_writable` fields present.
- `test_api_models.py`: lists fixture routes with `has_explicit_models`,
  `emission`, `required_extra`; **HTTP-level** no-caching test (finding
  14): `GET /api/models`, rewrite `settings.yaml` on disk, `GET
  /api/models` again, assert the payload changed; second test flips
  `DSH_HOME` via `monkeypatch.setenv` between two requests to the same
  `TestClient` and asserts the response follows.
- `test_api_graphs.py`: CRUD round-trip preserves every field except
  server-stamped `updatedAt`; path/body id mismatch on `PUT` -> 422;
  `DELETE ?project=1` removes the project dir, plain `DELETE` keeps it;
  `POST .../review` returns real `ReviewResult`-shaped output for both a
  valid and an invalid fixture graph; path-traversal graph ids (`..`,
  `../../etc/passwd`-style, null bytes) rejected with 422 before touching
  the filesystem (finding 7).
- `test_api_export.py`: 404 naming the expected path pre-compile; after
  a manual scaffold-equivalent write, reports an existing `projectPath`
  and a `runCommand` string containing `UV_CACHE_DIR=`.
- `test_api_compile_seam.py`: every `/api/compile*` path returns 501 with
  the documented body shape (finding 15).
- `test_openapi_schema.py`: hermetic (no network) — `app.openapi()`
  builds, and `SwarmGraph`'s component schema properties are camelCase
  (finding 21).

  All API tests point `DSH_HOME`/`SWARM_WORKSPACE` at pytest `tmp_path`,
  never the user's real `~/.dsh` or repo `workspace/`.

## Acceptance mapped to task's Definition of Done

- `UV_CACHE_DIR=... uv run pytest` passes (full suite, including Group
  2's — I will not weaken any Group-2 test to make mine pass).
- `UV_CACHE_DIR=... uv run ruff check` clean.
- Real server start + curl against real `~/.dsh/settings.yaml`,
  captured verbatim, server shut down afterward.
