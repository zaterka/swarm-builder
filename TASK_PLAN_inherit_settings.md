# Task plan — `inherit/settings.py` module and its tests

**Scope guard.** This task touches only:
- `src/swarm_builder/inherit/__init__.py` (new)
- `src/swarm_builder/inherit/settings.py` (new)
- `tests/test_inherit_settings.py` (new)
- `tests/fixtures/settings/*.yaml` (new)
- this plan file itself

No other file in the repo is read for editing purposes (only for context: `PLAN.md`, `config.py`, `models.py`, `pyproject.toml` are read-only references). `git commit` is never run.

## Goal

Implement the exact contract specified in the task prompt: `ModelInfo`,
`RouteConfig`, `AgentDefaultModel`, `Settings`, `read_settings`,
`EffectiveModel`, `resolve_effective_model` — matching PLAN.md facts 3, 4,
21 and the "Model inheritance" / edge-case prose about:
- no `agent-default-model` section being a normal state (bundle default
  applies),
- a route with no explicit `models:` enumerating nothing (empty tuple,
  not an error),
- unknown top-level/route keys being tolerated (plain `yaml.safe_load`,
  not a strict/forbid-extra pydantic model).

## Design decisions (filling gaps left open by the task spec)

**Revised after planReview (amazon-bedrock/opus-5) — see full critique in
session log; the numbered points below already fold in every accepted
finding.**

1. **Parsing style.** Use `yaml.safe_load` directly on the file text,
   producing plain `dict`/`list`/scalars. Build the frozen dataclasses by
   hand with explicit keyword construction pulled from `.get(...)` calls
   — **never `**mapping` splat** into a dataclass constructor. This
   matters concretely: the real `amazon-bedrock` route's models carry
   `contextWindow`/`maxTokens`, which `ModelInfo(**entry)` would reject
   with an unhandled `TypeError`. Only `id`/`name` are ever read off a
   model entry; every other key is ignored, which is also how "tolerate
   unknown keys" is satisfied (no schema library involved here, per
   fact 21's guidance for a dict-based parse).

2. **Scalar coercion helper.** All scalar fields that end up as `str |
   None` (route `key`, `api`, `base_url`, `api_key_env`, `aws_profile`,
   `aws_region`, `ModelInfo.id`/`.name`, `AgentDefaultModel.provider`/
   `.model`/`.reasoning_effort`) go through one helper:
   ```python
   def _coerce_str(value: object) -> str | None:
       if value is None or isinstance(value, bool):
           return None
       if isinstance(value, str | int | float):
           text = str(value).strip()
           return text or None
       return None
   ```
   `bool` is excluded *before* the `str | int | float` check because
   `isinstance(True, int)` is `True` in Python, and YAML 1.1 treats
   unquoted `on`/`off`/`yes`/`no`/`true`/`false` as booleans — a route
   literally named `on` cannot be recovered as the string `"on"`
   (`str(True) == "True"`), so it is dropped rather than silently
   misnamed. This closes the "YAML scalar coercion" gap the review
   raised (real risk: silent non-`str` fields, not a crash, since plain
   dataclasses do not type-check at construction).

3. **Route map (`llm-pi-ai.providers`) parsing.**
   - If the top-level document is not a `dict`, or `llm-pi-ai` is not a
     `dict`, or `providers` is not a `dict` -> zero routes, **not** an
     error (see point 9 below for why this is not conflated with a
     parse failure).
   - For each `(raw_key, raw_val)` in `providers.items()`: `key =
     _coerce_str(raw_key)`; skip the entire entry if `key is None`
     (covers a bool/null/empty-string key) or if `raw_val` is not a
     `dict` (covers a route value that is a scalar or list).
   - `api = _coerce_str(raw_val.get("api"))`; if that is `None`, check
     `awsProfile`/`awsRegion`: if `_coerce_str(raw_val.get("awsProfile"))
     is not None or _coerce_str(raw_val.get("awsRegion")) is not None`
     -> `api = "bedrock-converse-stream"`; else `api = None`. An
     explicit-`null` `awsProfile:` key therefore does **not** count as
     present (coerces to `None`), matching the review's point 9 — only a
     non-empty value counts.
   - `base_url = _coerce_str(raw_val.get("baseURL"))`, `api_key_env =
     _coerce_str(raw_val.get("apiKeyEnv"))`, `aws_profile =
     _coerce_str(raw_val.get("awsProfile"))`, `aws_region =
     _coerce_str(raw_val.get("awsRegion"))`.
   - `models`: if `raw_val.get("models")` is not a `list`, use `()`.
     Otherwise, for each entry: skip if not a `dict`; `id =
     _coerce_str(entry.get("id"))`, skip the entry if `id is None`;
     `name = _coerce_str(entry.get("name"))` (optional, may be `None`);
     append `ModelInfo(id=id, name=name)`. A non-dict list entry (bare
     string, `null`, etc.) is skipped, not fatal.

4. **`agent-default-model` parsing.** If the section is missing or not a
   `dict` -> `None` (a normal state per PLAN.md's "No agent-default-model
   section at all" edge case). Otherwise `provider = _coerce_str(...get
   ("provider"))`, `model = _coerce_str(...get("model"))`; both must be
   non-`None` for the section to count as present, else treated as
   absent (`None`) rather than an error — coercing first means a
   YAML-numeric `model: 3.5` still resolves to `"3.5"` instead of being
   silently discarded (the review's point 11: discarding a *configured*
   default and silently falling to the bundle default would violate
   PLAN.md I5, "never silently routed to a model they did not
   configure"). `reasoning_effort = _coerce_str(...get
   ("reasoningEffort"))`.

5. **`read_settings` return contract, exact exception handling and
   ordering.**
   ```python
   path = Path(dsh_home) / "settings.yaml"
   try:
       text = path.read_text(encoding="utf-8")
   except FileNotFoundError:
       return None
   except (OSError, UnicodeDecodeError) as exc:
       return Settings(routes=(), agent_default_model=None,
                        error=f"failed to read {path}: {exc}")
   try:
       doc = yaml.safe_load(text)
   except yaml.YAMLError as exc:
       return Settings(routes=(), agent_default_model=None,
                        error=f"failed to parse {path}: {exc}")
   mapping = doc if isinstance(doc, dict) else {}
   return Settings(routes=_parse_routes(mapping),
                    agent_default_model=_parse_agent_default_model(mapping),
                    error=None)
   ```
   Key points the review surfaced and that this now encodes explicitly:
   - `FileNotFoundError` is caught **before** the general `OSError`
     branch (it is a subclass; ordering is load-bearing) -> `None`.
   - `UnicodeDecodeError` (not an `OSError` subclass) is caught
     alongside `OSError` on the *read* so a non-UTF-8 file reports
     `.error` instead of escaping the function.
   - `encoding="utf-8"` is passed explicitly rather than inheriting the
     platform locale default.
   - An empty file (`yaml.safe_load("")` -> `None`) or a file containing
     only a scalar/list (`"hello"` -> `str`; `"- 1\n- 2"` -> `list`)
     parses **without** a `YAMLError` -- this is the "exists, parses,
     but is semantically empty/wrong-shaped" case and returns
     `Settings(routes=(), agent_default_model=None, error=None)`,
     **distinct** from the file-absent `None` case and **distinct** from
     the malformed-YAML `.error` case. All three are separately tested
     (see the updated test list).
   - `dsh_home` pointing at a path where a component is actually a file
     (`NotADirectoryError`) or at a directory named `settings.yaml`
     (`IsADirectoryError`) are both `OSError` subclasses and therefore
     surface as `.error`, deliberately: a broken/misconfigured
     `DSH_HOME` is a different, worth-surfacing state from "no harness
     installed at all" (which is a clean `FileNotFoundError`).
   - Never caches: `path.read_text()` happens fresh inside the function
     body on every call; no module-level state, no `lru_cache`.
   - `dsh_home` is used as `Path(dsh_home) / "settings.yaml"` (accepts a
     `Path`, defensively tolerates a raw `str` too since the coercion is
     free).

6. **`resolve_effective_model` route lookup.** `read_settings(dsh_home)`
   is called exactly once per `resolve_effective_model` call (never
   cached, satisfying the "re-reads fresh every call" requirement
   transitively) and its `routes` (or `()` if it returned `None` or had
   `.error` set) are searched with `next((r for r in routes if r.key ==
   provider), None)` — case-sensitive exact match. This lookup is used
   for **both** `graph-override` and `settings-default`. Two outcomes
   the review flagged as previously-unstated:
   - **`graph_override` naming a provider with no matching route still
     wins** — the override's `(provider, model, reasoning_effort)` is
     used verbatim regardless of whether a route matches; only `route`
     (and, see point 7, `base_url`/`api_key_env`) come up empty. It must
     never fall through to `settings-default`.
   - **A `read_settings` call that returned `.error` set (or `None`) is
     treated as "no settings-default available"** — resolution falls
     through to the `SWARM_MODEL` branch, not to the bundle default
     directly and not by raising. This is tested explicitly: malformed
     settings.yaml + `SWARM_MODEL` set -> `source="env-fallback"`.

7. **`base_url`/`api_key_env` on `EffectiveModel` for the
   `graph-override`/`settings-default` cases.** The exact contract
   states these fields but does not spell out their source for these
   two cases (only for `env-fallback`, where they come from
   `config.get_swarm_base_url()`/`get_swarm_api_key_env()`). Decision:
   when a matching `route` is found, `base_url = route.base_url` and
   `api_key_env = route.api_key_env`; when no route matches (including
   both env-fallback and bundle-default, and an unmatched
   graph-override), both are `None`. This is the only sensible reading
   given `inherit/routes.py` (a later group, out of scope here) needs a
   custom route's `baseURL` to construct an `OpenAIChatModel` — the
   `EffectiveModel` is the seam that must carry it.

8. **`SWARM_MODEL` split rule, corrected.** Read once via
   `config.get_swarm_model()`, which already normalizes an unset **or
   empty-string** env var to `None` (`os.environ.get(...) or None`) —
   so the "empty env var" edge case the review raised is already closed
   by the existing `config.py` helper and needs no extra handling here.
   Given a non-`None` `raw`:
   - if `":" not in raw` -> `provider = "env"`, `model = raw`.
   - else `provider, model = raw.split(":", 1)`; if the resulting
     `provider` is an empty string (e.g. `raw == ":foo"`), replace it
     with `"env"` (an empty provider name is not useful to report).
   - `config.get_swarm_base_url()` similarly already collapses `""` to
     `None`; when it is not `None`, `provider = "custom"` and `model =
     raw` **unsplit** (the raw `SWARM_MODEL` value verbatim), per the
     exact contract.
   - No `.strip()` beyond what `_coerce_str`-style helpers already do;
     `SWARM_MODEL`/`SWARM_BASE_URL` are taken verbatim from
     `config.py`'s existing `or None` normalization.

9. **The OSError-vs-shape distinction (kept, now justified explicitly).**
   A document that `yaml.safe_load` accepted did not "fail to parse" in
   the contract's sense (`yaml.YAMLError`/`OSError`), so a mis-shaped
   section (e.g. `providers:` given as a YAML list instead of a mapping)
   is reported as **no routes** — the fact-4 "no providers configured"
   failure mode PLAN.md already documents as a normal, health-check-
   reportable state — never as `.error`. This boundary is now pinned by
   an explicit test (`providers` given as a list -> `routes == ()` AND
   `error is None`, not conflated with the malformed-YAML case).

10. **`RouteConfig` ordering.** Route iteration order follows YAML
    document order (Python dict insertion order, which is what
    `yaml.safe_load` produces); duplicate top-level provider keys in the
    source YAML resolve YAML-spec-standard "last one wins" with no error
    surfaced (this is `yaml.safe_load`'s native behavior, not something
    this module adds logic for). Tests locate a route by `key` (e.g.
    `next(r for r in s.routes if r.key == "amazon-bedrock")`) rather than
    by list position, so this ordering detail is never load-bearing for
    a test assertion.

11. **Fixtures under `tests/fixtures/settings/`.** Four files (one more
    than originally planned, per the review's point 17):
    - `full.yaml` — mirrors the real file's shape closely: multiple
      unrelated top-level sections (`ui-onboarding`, `agent-presets`,
      `ui-theme`, `dev-mode-pipeline`), an `llm-pi-ai.providers` map with
      (a) a `kornerstone`-style route with an explicit `models:` list
      whose entries carry `id`+`name` **plus** an unknown key each
      (mirrors nothing extra needed, kept minimal) and (b) an
      `amazon-bedrock`-style route with `awsProfile`+`awsRegion`, no
      `api`, and models carrying `contextWindow`/`maxTokens` (unknown
      keys that must be tolerated, not splatted), and an
      `agent-default-model` section pointing at the bedrock route.
    - `no_explicit_models.yaml` — a single route with no `models:` key
      at all (fact-21 case) and no `agent-default-model` section at all
      (the other named edge case).
    - `malformed.yaml` — deliberately broken YAML syntax (unbalanced
      brackets / bad indentation) to exercise the `yaml.YAMLError` path.
    - `odd_shapes.yaml` — `llm-pi-ai.providers` given as a YAML **list**
      (not a mapping) to pin point 9's boundary, used by the
      "mis-shaped section is not an error" test.
    All are read via `(FIXTURES_DIR / "name.yaml").read_text()` and
    written into a `tmp_path`-based `dsh_home / "settings.yaml"` by the
    test — tests never point at `tests/fixtures/settings/` directly as a
    fake `DSH_HOME`. This also lets the "per-call re-read" tests
    overwrite the tmp file's content freely. No `__init__.py` is added
    under `tests/fixtures/settings/` (it holds only data files, unlike
    the existing `tests/fixtures/` Python package).

12. **Error-message assertions in tests.** `.error` is asserted as a
    non-empty `str` containing the settings file's path name (e.g.
    `"settings.yaml" in s.error` or `str(path) in s.error`), never
    compared for exact equality — exact PyYAML/Python error text is not
    a stable contract across versions.

13. **Docstring tone.** Match `config.py`/`models.py`: module docstring
    explains *why* (fact 21, hot-reload/no-caching rationale, dict-based
    tolerant parsing rationale, the never-splat/coercion rules and what
    they protect against); each public function/class gets a
    multi-sentence docstring citing the relevant PLAN.md facts.

## Files to create

- `src/swarm_builder/inherit/__init__.py` — package docstring only,
  `from __future__ import annotations` not needed in an otherwise-empty
  file but harmless; will include it for consistency, docstring:
  "Model-route inheritance: reading harness settings.yaml (facts 3-4,
  21) and resolving the effective compile model."
- `src/swarm_builder/inherit/settings.py` — the full contract as
  specified in the task prompt, implemented per decisions above.
- `tests/test_inherit_settings.py` — the test scenarios below, using
  `tmp_path`, `monkeypatch.setenv/delenv`, and the four fixture files.
- `tests/fixtures/settings/full.yaml`
- `tests/fixtures/settings/no_explicit_models.yaml`
- `tests/fixtures/settings/malformed.yaml`
- `tests/fixtures/settings/odd_shapes.yaml`

## Test list (mirrors task prompt's 9 items plus review-driven additions)

1. `test_route_with_explicit_models_list` (uses `full.yaml`)
2. `test_route_without_models_key_is_empty_tuple` (uses
   `no_explicit_models.yaml`)
3. `test_route_infers_bedrock_api_from_aws_fields` (uses `full.yaml`'s
   aws-profile route)
4. `test_route_with_no_api_and_no_aws_fields_is_none` (uses
   `no_explicit_models.yaml`'s bare route)
5. `test_resolve_effective_model_precedence_*` — split into several
   focused test functions for clarity:
   - `test_graph_override_wins_over_everything`
   - `test_settings_default_wins_over_env`
   - `test_env_fallback_used_when_no_settings_file`
   - `test_bundle_default_used_when_nothing_else_set`
6. `test_env_fallback_with_base_url_reports_custom_provider`
7. `test_read_settings_returns_none_when_file_absent`
8. `test_read_settings_returns_error_on_malformed_yaml`
9. `test_read_settings_rereads_on_every_call` and
   `test_resolve_effective_model_rereads_on_every_call`

Review-driven additions (design decisions 2-9 above):
10. `test_read_settings_empty_file_is_not_an_error` — zero-byte
    `settings.yaml` -> `Settings(routes=(), agent_default_model=None,
    error=None)`, asserted distinctly from both the absent-file `None`
    case and the malformed-YAML `.error` case.
11. `test_read_settings_mis_shaped_providers_is_not_an_error` (uses
    `odd_shapes.yaml`) — `providers` given as a YAML list -> `routes ==
    ()` and `error is None`, pinning design decision 9's boundary.
12. `test_model_entry_with_extra_unknown_keys_is_tolerated` — a model
    entry carrying `contextWindow`/`maxTokens` (mirrors the real
    `amazon-bedrock` route) parses without a `TypeError`, confirming the
    never-splat rule (design decision 1).
13. `test_graph_override_with_unmatched_provider_still_wins` —
    `graph_override` names a provider with no matching route ->
    `source="graph-override"`, the override's model is used verbatim,
    `route is None`.
14. `test_malformed_settings_falls_through_to_env_fallback` — malformed
    `settings.yaml` + `SWARM_MODEL` set -> `source="env-fallback"`, not a
    crash and not silently `bundle-default`.
15. `test_error_message_names_the_settings_path` — asserts `.error` is a
    non-empty string containing the settings file's name, not an exact
    string match (design decision 12).

## Verification

```
cd /Users/pedro.zaterka/factored-projects/swarm-builder
export UV_CACHE_DIR=/Users/pedro.zaterka/factored-projects/swarm-builder/.uv-cache
uv run pytest tests/test_inherit_settings.py -v
uv run ruff check src/swarm_builder/inherit/ tests/test_inherit_settings.py
```

Both must report zero failures / zero lint errors before this task is
considered done.
