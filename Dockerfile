# Swarm Builder in a container.
#
# Two stages, because the app is a Python server that serves a Node-built
# frontend. Building the bundle *here* rather than copying the committed
# `web/dist` means the image can never ship a bundle that predates the
# source it was built from; Docker's layer cache makes the node stage a
# no-op when `web/` is unchanged.
#
# Runs as a non-root user with an explicit HOME. That is not cosmetic:
# boto3 resolves `~/.aws`, Swarm Builder resolves `$DSH_HOME`, and `uv`
# keeps managed interpreters under `~/.local/share/uv`. Get HOME wrong and
# the container finds no settings and no credentials -- *silently*, with
# no error at boot.
#
# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 -- build the frontend bundle
# ---------------------------------------------------------------------------
FROM node:24-bookworm-slim AS web

# Vite 8 needs Node ^20.19 || >=22.12. The local toolchain is Node 26, but
# 24 is the current LTS and is what a container should pin.
WORKDIR /build/web

# Install dependencies against the lockfile alone, so this layer is cached
# until the lockfile actually changes. pnpm is pinned to the same version
# the lockfile was authored with. Note: do NOT also run `corepack enable`
# — it creates a `pnpx` shim that the global install then collides with
# (`EEXIST: file already exists: /usr/local/bin/pnpx`).
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN npm install -g pnpm@11.25.0 && pnpm install --frozen-lockfile

COPY web/ ./
RUN pnpm build

# ---------------------------------------------------------------------------
# Stage 2 -- runtime
# ---------------------------------------------------------------------------
# The astral image ships `uv` *and* a Python, which this image needs at
# runtime and not merely at build time: the compile pipeline spawns
# `uv sync` inside each generated project (Phase 5's validation gate).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS runtime

# The uv bundled with this base image (0.9.30 at the time of writing) is
# too old to satisfy this repo's interpreter pin, and that is fatal rather
# than cosmetic. `.python-version` is 3.14.5; the image ships 3.14.2, and
# the bundled uv can only download up to 3.14.3, so it reports
# "No download found for request: cpython-3.14.5-linux-aarch64-gnu" and
# every generated project -- which inherits the repo's pin -- would fail
# Phase 5's `uv sync`. `uv self update` refuses to run here, since the
# binary came from the image's package manager.
#
# So: install a current uv from the official installer, pinned, and
# pre-install the pinned interpreter into a shared, world-readable
# location. Verified: a current uv does publish 3.14.5 for linux-aarch64.
ARG UV_VERSION=0.12.15
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /usr/local/bin/uv /usr/local/bin/uvx \
    && curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin UV_VERSION="${UV_VERSION}" sh \
    && uv --version \
    && uv python install 3.14.5 \
    && chmod -R a+rX /opt/uv-python

# A non-root user with a real HOME. The uid/gid can be overridden at
# runtime on Linux (`user:` in compose) to match the host and avoid
# root-owned files in a bind-mounted workspace.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first, from the lockfile alone, so the expensive layer is
# cached independently of source edits.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# Application source and the bundle built in stage 1.
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY docs/ ./docs/
# Read at *compile* time, not build time: `scaffold.py` copies this pin
# into every generated project so `uv run` there cannot pick a different
# interpreter. Omitting it makes the scaffold phase fail with
# `FileNotFoundError: '/app/.python-version'` — the only repo-root file the
# app reads at runtime that is not source.
COPY .python-version ./
COPY --from=web /build/web/dist ./web/dist/
RUN uv sync --frozen --no-dev

# Runtime configuration. See README.md's "Run with Docker" section.
ENV \
    # A loopback bind inside a container is unreachable through a
    # published port. The compose file restores the outer restriction by
    # publishing to host loopback only (127.0.0.1:8420:8420), preserving
    # the single-user trust model this app assumes.
    SWARM_HOST=0.0.0.0 \
    PORT=8420 \
    # Without this the startup URL and the SSE stream can sit in a buffer
    # instead of appearing in `docker compose logs`, which makes a working
    # container look hung.
    PYTHONUNBUFFERED=1 \
    HOME=/home/app \
    DSH_HOME=/home/app/.dsh \
    SWARM_WORKSPACE=/workspace \
    UV_CACHE_DIR=/uv-cache \
    # Keep `uv run` from warning about a foreign VIRTUAL_ENV when the
    # generated project's gate runs.
    UV_PROJECT_ENVIRONMENT=/venvs/env

# `/workspace` is where graphs and generated projects land, and the uv
# cache and the `.venv` of every generated project must never be shared
# with the host: wheels and interpreters are platform-specific, so a
# Linux venv on a macOS bind mount half-works (pure-Python deps import,
# compiled ones die with `bad CPU type`). Keeping both as named volumes
# means the shared workspace holds only portable artefacts.
VOLUME ["/workspace", "/uv-cache", "/venvs"]

# `/workspace`, `/uv-cache` and `/venvs` must be owned by `app` so the
# named volumes inherit that ownership on first use. `/app` needs it too,
# and not for writing: source files checked out on a macOS host can carry
# restrictive modes (this repo's are `-rw-------`), and a root-owned copy
# of a mode-600 file is unreadable by the non-root user — the container
# then fails at startup with `PermissionError: '/app/src/.../__init__.py'`.
RUN mkdir -p /workspace /uv-cache /venvs \
    && chown -R app:app /app /workspace /uv-cache /venvs /home/app

USER app

EXPOSE 8420

# Python-based probe, so the image needs no curl.
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/api/health', timeout=4).status == 200 else 1)"]

# Exec form, and the console script directly rather than through `uv run`:
# the server must be PID 1's child with no shell or `uv` wrapper in
# between, or it never receives SIGTERM and the lifespan hook that cancels
# live compile jobs will not run on `docker compose down`.
CMD ["/app/.venv/bin/swarm-builder"]
