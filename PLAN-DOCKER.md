# Swarm Builder — Docker/Compose plan (v1)

Goal: make Swarm Builder easy for someone else to run, in two parts, in this order — because they solve different halves of the problem and the cheaper one fixes the friction that actually exists today.

## Why two parts

The current quickstart requires `uv` **and** Node **and** pnpm before anything renders, because `web/dist` is gitignored (`git check-ignore -v web/dist` → `.gitignore:2:dist/`). The server only mounts the UI when `web/dist/index.html` exists (`main.py:86-88`); otherwise `/` returns a "frontend has not been built yet" message. So a newcomer meets a JSON error page unless they build the frontend.

Docker cannot fix that for anyone not using Docker. Shipping `dist` can, and it is a fraction of the work.

Docker's real value is a zero-toolchain path: no Node, no pnpm, no `uv`, one command.

## Explicitly NOT a plan violation

`PLAN.md` assumption 10 and the "out of scope" section say *no Docker is generated* — that is about **generated projects** (the per-graph exports), not about Swarm Builder itself. Containerizing the app does not contradict the plan. Worth stating so a reviewer does not flag it.

---

## Part A — ship `web/dist`

**A1. Narrow `.gitignore` so `web/dist` ships.** Replace the blanket `dist/` with a rule that ignores build output *except* the shipped frontend bundle, e.g. keep `dist/` and add `!web/dist/`, or scope it to `/web/dist` explicitly. Verified need: the repo is on `main` with **zero commits** and nothing tracked, so `.gitignore` is exactly what decides this.

**A2. Ignore the build artifacts that would otherwise be committed.** `.cache/` (135 MB) and `.pnpm-store/` (241 MB) are currently untracked and unignored, produced by the documented project-local npm-cache workaround. Committing either would be a serious mistake. Add them, plus `.mypy_cache/`, `.pytest_cache/` (partly present).

**A3. Document the maintainer rebuild path.** `pnpm --dir web build`, and state in the README that `dist` is committed so end users never need it.

**A3b. Resolve committed-vs-in-image `dist` explicitly, so the two cannot diverge confusingly.** Both are produced by the same command (`pnpm --dir web build`), but from different source snapshots at different times:

- **inside the container**, the image's own build (B3) is authoritative — it is always consistent with the source it was built from;
- **outside the container**, the committed `web/dist` is authoritative, and A4's staleness check is what keeps it honest.

They can differ only if someone edits `web/src` and commits without rebuilding `dist` — which is exactly what A4 warns about. Stating this in the README removes the ambiguity; the alternative (have the image copy the committed `dist`) would trade a known staleness signal for an unknown one.

**A4. Guard staleness cheaply.** Add a test or a `scripts/check_dist.py` that warns when `web/dist` is older than the newest file under `web/src`. Rationale: a shipped, committed bundle that silently drifts from source is the one real cost of A1, and a staleness check buys back most of it. (Decision: warn, do not fail the suite — a stale bundle is a release-hygiene problem, not a correctness one.)

Part A is small: no runtime behavior changes.

---

## Part B — `docker-compose` for the app

**B1. One required code change: a configurable bind host.** A container bound to loopback is unreachable through a published port. Verified call sites (grepped; these are **all** of them):

| Site | Change |
|---|---|
| `config.py:52` | docstring only — add a `get_swarm_host()` beside `get_port()` |
| `main.py:126` | the actual bind: `uvicorn.run(app, host=...)` |
| `main.py:124` | the printed URL, which hardcodes `127.0.0.1` |
| `main.py:129` | the bind-failure message, which hardcodes `127.0.0.1` and names only `PORT` as the fix |

Default `SWARM_HOST=127.0.0.1`, **preserving today's behavior exactly** for every existing user. Two non-issues found while checking: `web/vite.config.ts:15`'s dev-proxy target (`http://127.0.0.1:8420`) is host-side dev only and is unaffected, and **no test asserts loopback**, so this change cannot break the suite. Prose mentions in `README.md`, `docs/architecture.md`, `docs/api.md` need a light touch-up.

**B2. Publish to host loopback only: `127.0.0.1:8420:8420`.** This is a deliberate security decision, not a default. `PLAN.md` assumption 8 states the server has **no auth** and is trusted-input-only because it binds loopback as a single-user tool. Binding `0.0.0.0` inside the container is required for the port to work at all, so the loopback restriction must be re-established at the publish layer. Publishing `0.0.0.0:8420` would silently expose an unauthenticated app to the local network, which the plan does not sanction.

**B3. Multi-stage `Dockerfile`.**
- Stage 1 (`node`): `pnpm install` + `pnpm --dir web build` → `web/dist`.
- Stage 2 (runtime): an image carrying `uv` + Python 3.14, `uv sync --frozen` against the committed root `uv.lock` (218 KB, exists), then copy the app and the stage-1 `dist`.
- Rationale for building the frontend in-image rather than copying the committed `dist`: the image can never ship a stale bundle, and Docker layer caching makes the node stage a no-op when `web/` is unchanged. Costs a slightly larger build graph; worth it for the guarantee.

**B4. `uv` must exist in the runtime image.** The compile pipeline spawns `uv sync` inside each generated project (`validate.py`), so the container needs `uv` and network access to PyPI for generated-project dependencies. This is easy to miss and would break Phase 5 only at compile time.

**B5. Never share a host-side Python artifact with the container.** Use **named volumes**, not bind mounts, for the uv cache and for generated-project virtualenvs. Two distinct instances of one rule — the container is Linux, the host is macOS:

- *uv cache* — holds platform- and interpreter-specific wheels. A host arm64 cache mounted into a Linux container produces confusing resolution failures.
- *generated `.venv` directories* — the sharper trap, and the reason B6 below is not "just bind-mount `./workspace`". A venv is **not portable across platforms**. Verified locally: `.venv/bin/python` is an absolute symlink to `/Users/pedro.zaterka/.local/share/uv/python/cpython-3.14.5-macos-aarch64-none/bin/python3.14`, and `.venv/pyvenv.cfg` bakes `home = /Users/pedro.zaterka/.local/share/uv/python/cpython-3.14.5-macos-aarch64-none/bin`. Console scripts under `.venv/bin/` carry the same absolute path in their shebang. So a venv built by the container's Linux `uv` is unusable on the host: the symlink dangles, the shebangs point at nonexistent paths, and any compiled dependency is a Linux ELF (`bad CPU type` / `incompatible architecture`). Pure-Python deps would survive, which makes this fail *partially and confusingly* rather than cleanly.

**`uv.lock` is portable** and is not a problem — it records resolution metadata plus per-platform wheel lists, so the host's `uv` re-selects macOS wheels. Only `.venv` must never cross the boundary.

**B6. Volumes and env.**

| Mount | Mode | Why |
|---|---|---|
| `~/.dsh` | rw | Settings inheritance (`settings.yaml`) — read per compile. Also written by nothing in the app, but rw as chosen. |
| `~/.aws` | rw | Bedrock auth via boto3. **rw is required**: AWS SSO token refresh writes `~/.aws/sso/cache`. Read-only mounts break real compiles — we already observed `Operation not permitted: ~/.aws/sso/cache/tmpnqehy7ar.tmp`. |
| `./workspace` | bind rw | Generated project **sources** — so a user can read, copy, and run an export from the host. |
| uv cache | named volume | See B5. |
| `UV_PROJECT_ENVIRONMENT` (per compile) | container-local path, **outside** `/workspace` | Keeps `.venv` off the shared volume — see B5 and below. |

**The `./workspace` mount is only safe with `UV_PROJECT_ENVIRONMENT` set.** Binding `./workspace` read-write is what makes exports inspectable, but as written it would also hand the host a Linux `.venv` that silently half-works. So the container sets `UV_PROJECT_ENVIRONMENT` to a container-local path, and the shared volume holds only portable artifacts: sources, `uv.lock`, `validate/golden_render.txt`, and the generated project's own config. The host then creates its own environment with an ordinary `uv sync`, and the README says so explicitly (`rm -rf .venv` first if one exists). This is the correction that makes B6 sound rather than naive.

Note `validate.py:524` runs plain `uv sync` (not `--frozen`), so the lock is advisory during a compile; that is pre-existing behavior and unchanged here.

- `SWARM_HOST=0.0.0.0`, `PORT`, `DSH_HOME`, `SWARM_WORKSPACE=/workspace`, `UV_CACHE_DIR`.
- Pass `AWS_PROFILE` / `AWS_REGION` through from the host environment (or `.env`), since the container cannot read the host shell.
- Healthcheck: `GET /api/health`, which already reports `compileReady` and blockers — the right probe, and it makes `docker compose ps` meaningful.

**B6b. Container runtime details that will otherwise bite.**

| Concern | Decision |
|---|---|
| `HOME` | Must be set explicitly and the credential mounts must land *on it*. boto3 resolves `~/.aws`, Swarm Builder resolves `~/.dsh`, and `uv` manages interpreters under `~/.local/share/uv`. Run as a non-root user `app` with `HOME=/home/app`, mounting `~/.dsh` → `/home/app/.dsh` and `~/.aws` → `/home/app/.aws`. Getting this wrong does not error at boot — it silently finds no settings and no credentials. |
| uid/gid vs. the bind mount | On Linux, a root container writing `./workspace` leaves root-owned files the host user cannot delete. Document a `user: "${UID}:${GID}"` override for Linux. On macOS, Docker Desktop's file sharing maps ownership, so the default is fine — the two platforms genuinely differ, and the README should say which caveat applies where. |
| `PYTHONUNBUFFERED=1` | Without it the startup URL line and the SSE stream can sit in a buffer instead of appearing in `docker compose logs`, which makes a working container look hung. |
| Signal handling / `docker compose down` | The process must be `uvicorn` in **exec form** (no shell wrapper), or it never receives `SIGTERM` and `main.py`'s lifespan shutdown hook — which cancels live compile jobs — will not run. Pair with a `stop_grace_period` long enough for an in-flight compile to unwind. This is the difference between cancelling a compile and orphaning it. |
| Interpreter pin | `.python-version` is `3.14.5`; the runtime image ships its own Python. `uv` may download the exact patch version into `HOME` on first use, so the image needs `HOME` writable and network access. Confirmed during implementation. |
| Image size | `uv sync --frozen` against the root `uv.lock`, a slim base, and a `.dockerignore` that excludes `node_modules`/`.pnpm-store`/`.cache`/`spike` keep this reasonable. Not an optimization goal, but worth not regressing. |

**B7. `.dockerignore` is mandatory.** Without it the build context includes `.pnpm-store` (241 MB), `.cache` (135 MB), `node_modules`, `.venv`, `.uv-cache`, `workspace/`, `.git`, and `spike/`. Exclude all of them, plus `web/dist` (rebuilt in stage 1).

**B8. Default to the real path; document the credential-free one.** The developer chose rw credential mounts, so `docker compose up` targets real compiles. Also document `SWARM_FAKE_FILL=1` (no model, no credentials, no `$DSH_HOME`) as the way to try a full five-phase compile in-container — it is the fastest way to confirm the container works before wiring credentials.

**B9. Docs.** A README "Run with Docker" section: prerequisites (Docker Desktop **running**), the one command, where `workspace/` lands, how to set `AWS_PROFILE`, the `SWARM_FAKE_FILL=1` trial, **the `rm -rf .venv && uv sync` step for running an export on the host** (B6), and an honest note that the container holds **write** access to `~/.aws` and `~/.dsh` — a real property of this setup, not a detail to bury.

---

## Failure modes to handle

| Failure | Behavior |
|---|---|
| Docker daemon not running | `docker compose up` prints Docker's own error; README names "start Docker Desktop" as the fix. **This is the current state on this machine** (`docker info` hangs; socket present but idle) — so a compose file can be written but **not verified** until the daemon starts. |
| No credentials mounted | `/api/health` reports `compileReady: false` with blockers, exactly as the harness-absent path already does. Must not crash on boot. |
| Expired AWS SSO token | Provider error surfaces in the compile log naming the route; `aws sso login` on the **host**, since the container shares `~/.aws`. |
| `~/.dsh` absent | Route resolution falls back to `SWARM_MODEL`, then bundle default, with the source reported. Already implemented. |
| PyPI unreachable | `uv sync` in a generated project fails Phase 5 with the stderr tail; the project stays on disk in the bind-mounted `workspace/`. Already implemented. |
| Port 8420 already in use on host | Bind failure names `PORT`; README notes the compose port mapping can be changed on the host side only. |
| ARM64 Mac | Linux containers run under a VM; `uv`/Python are architecture-consistent inside it because the cache is a separate volume (B5). |

## Verification (must be executed, not asserted)

0. **Daemon-free, runnable now:** `docker compose config` (validates and resolves the compose file), `docker compose config --services`, and a YAML/lint pass over the Dockerfile — these catch most syntax, interpolation, and mount-path errors without a daemon.
1. `docker info` responds — **blocked until Docker Desktop is started.**
2. `docker compose build` succeeds from a clean context.
3. `docker compose up -d`, then `curl 127.0.0.1:8420/api/health` → `compileReady` true with credentials mounted.
4. The canvas actually renders (built assets served, not the "not built yet" message).
5. A full compile via HTTP with **`SWARM_FAKE_FILL=1`** reaching `succeeded`, all five phases.
6. A **real** model-backed compile with `AWS_PROFILE` passthrough — this is the path the rw mounts exist for. Expect it to need a currently-valid SSO token.
7. The generated project in `./workspace/projects/<id>` is reachable from the host and passes `uv run python validate/dry_run.py`.
8. `docker compose down` leaves no stray containers.
9. Full existing suites still pass: 323 Python, 62 Vitest, `ruff` clean.
10. `SWARM_HOST` default preserves current behavior: `uv run swarm-builder` still binds `127.0.0.1` and prints `127.0.0.1`.
11. **No Linux `.venv` ends up in `./workspace`** — after a container compile, assert `./workspace/projects/<id>/.venv` does not exist (or is absent from the shared volume), and that `uv sync` on the host then succeeds and produces a working `.venv`.

## Acceptance criteria

1. A fresh clone that has never run `pnpm` serves the canvas from `uv run swarm-builder` alone (Part A).
2. `docker compose up` on a machine with only Docker gives a working canvas at `http://127.0.0.1:8420`.
3. Compose publishes to **loopback only**; nothing binds `0.0.0.0` on the host.
4. Real (non-fake) compiles work in-container with `AWS_PROFILE` set; `SWARM_FAKE_FILL=1` works with no credentials at all.
5. Existing behavior is unchanged for non-Docker users: same default host, same port, 323 + 62 tests still green.
6. `.dockerignore` keeps the build context free of `node_modules`, `.pnpm-store`, `.cache`, `.venv`, `.uv-cache`, `workspace/`, and `.git`.

## Verification results (all executed)

Everything below was run against this checkout. Docker 28.0.1 / Compose v2.33.1, Linux containers on Apple Silicon.

| # | Check | Result |
|---|---|---|
| 0 | `docker compose config` | resolves; `host_ip: 127.0.0.1` confirmed on the published port |
| 1 | `docker compose build` | succeeds; installs pinned `uv` 0.12.15 and CPython 3.14.5 |
| 2 | `docker compose up -d` | container **Up (healthy)** |
| 3 | canvas from the host | `GET /` → 200 `text/html`; a 449 KB JS asset → 200 |
| 4 | `/api/health` from the host | `dshHome: /home/app/.dsh`, route inherited from the mounted settings, `webDistPresent: true`, `compileReady: true` |
| 5 | **fake-fill compile in-container** | all five phases succeeded; `validationSteps: [uv_sync, keyless_import, dry_run]` |
| 6 | **real Bedrock compile in-container** | all five phases succeeded; resolved `amazon-bedrock:us.anthropic.claude-sonnet-5` with `source=graph-override`; the live model wrote a correct body inside the markers |
| 7 | **no Linux `.venv` in `./workspace`** | confirmed — the share holds only source, `uv.lock`, `.python-version`, `.env.example`, `README.md`, `validate/` (B5/B6 working) |
| 8 | **export runs on the macOS host** | copied out, `uv sync` + `validate/dry_run.py` with a fresh cache and no credentials → `ALL CHECKS PASSED` |
| 9 | `docker compose down` / `stop` | graceful in ~3s; logs show `Shutting down` → `Application shutdown complete`, so live compiles are cancelled, not orphaned |
| 10 | container write access to mounted `~/.aws` | **writable** — confirms AWS SSO token refresh works in-container, validating the rw mount choice |
| 11 | host suites unaffected | 327 Python (was 323; +4 new), 62 Vitest, `ruff` clean |

Three real defects were found and fixed **during** this verification, each of which would have shipped broken:

1. `corepack enable` + `npm install -g pnpm` collided on `/usr/local/bin/pnpx` (`EEXIST`). Fixed by pinning `pnpm@11.25.0` without corepack.
2. The container died at startup with `PermissionError: '/app/src/swarm_builder/__init__.py'` — macOS checkouts here carry mode `-rw-------`, so root-owned copies were unreadable by the non-root user. Fixed with `chown -R app:app /app`.
3. `.python-version` is read at *compile* time by `scaffold.py`, but was never copied into the image. Adding it then exposed a second problem: the base image's `uv` 0.9.30 cannot download 3.14.5 at all (`No download found for request: cpython-3.14.5-linux-aarch64-gnu`, though the registry does publish it for linux-aarch64). Fixed by installing a pinned current `uv` and pre-installing the interpreter.

## Review status (be aware)

The independent plan-review stage was attempted **four times** and failed each time on the configured `planReview` role model (`amazon-bedrock` / `us.anthropic.claude-opus-5`) — the spawn aborted after its first sentence, with no findings. A direct probe of that same model route succeeded (`Agent(BedrockConverseModel('us.anthropic.claude-opus-5')).run_sync(...)` → `OK`), and a scratch workflow on `deepseek-official` also returned null, so this looks like **spawn flakiness rather than a credential problem** — but it is unresolved.

What that means for this plan:

- **One focused independent review was obtained**, on the highest-risk question (B5/B6, the shared-volume venv trap). It confirmed the trap and supplied the concrete failure modes; B5/B6 above are rewritten around it.
- **The remaining questions were checked by direct inspection instead**: every `127.0.0.1` call site was grepped and enumerated (B1), and the venv-portability mechanism was verified on a real venv in this repo rather than assumed.
- **This plan has therefore NOT had a full independent critique.** Treat that as a real gap when approving it, not a formality.

## Assumptions

1. Docker Desktop (or another daemon) is running for build/verify; it is currently not, so items 2-6 of Verification depend on the developer starting it.
2. `~/.aws` and `~/.dsh` existing on the host is expected; their absence degrades to the documented fallbacks rather than erroring.
3. The committed `uv.lock` stays the source of truth for in-image server deps (`uv sync --frozen`).
4. Node is pinned to an LTS major tag in stage 1 (local dev uses 26.x; the image must not chase a non-LTS tag). Exact tag confirmed during implementation.
5. Compose is for local single-user use, matching assumption 8's trust model — not a multi-user deployment. No TLS, no auth, no reverse proxy.
