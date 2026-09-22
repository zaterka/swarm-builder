"""Server entry point: the FastAPI app and console-script hook.

Owns route registration, CORS for a separately-run Vite dev server,
static serving of the built frontend when present, and the
``swarm-builder`` console-script entry point.

**Nothing in this module ever calls a settings/env-resolution
function.** ``create_app()`` only *registers* routes; every
``config.get_*``/``inherit.settings`` call happens per-request, inside
each route handler (see ``routes/__init__.py``'s module docstring).
Constructing the module-level ``app`` below is therefore safe with
respect to the no-caching requirement -- it never itself resolves
``DSH_HOME``, a workspace directory, or a model route at import time.

The app carries one lifespan hook (:func:`_lifespan`), which shuts the
compile subsystem down on the way out and does nothing on the way in --
see its docstring.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from swarm_builder import __version__
from swarm_builder.config import REPO_ROOT, get_host, get_port, load_env_file
from swarm_builder.routes import compile as compile_routes
from swarm_builder.routes import export, generate, graphs, health, llm_routes, runs, templates


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Shut the compile subsystem down cleanly when the server stops.

    A compile is an in-flight asyncio task that writes into a project
    directory, so exiting while one is mid-step would leave a
    half-scaffolded project behind. ``JobRegistry.shutdown()`` cancels
    every live job and *awaits* it before this returns. It is called only
    on the way out -- there is nothing to set up, and the registry is
    created lazily by ``routes/compile.py`` on first use.

    Args:
        app: The app being served (unused; the lifespan protocol's
            signature).

    Yields:
        Control back to the server for the duration of its lifetime.
    """
    del app
    try:
        yield
    finally:
        await compile_routes.shutdown_jobs()


def create_app() -> FastAPI:
    """Build a fresh :class:`FastAPI` app with every route registered.

    Callable repeatedly (each call returns an independent app instance
    with its own route table) -- tests construct a fresh app per test
    via this factory so state from one test's app object can never leak
    into another's, and each call re-checks ``web/dist/index.html`` at
    call time so a build produced between two calls is picked up.
    """
    app = FastAPI(title="Swarm Builder", version=__version__, lifespan=_lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix="/api")
    app.include_router(graphs.router, prefix="/api")
    app.include_router(templates.router, prefix="/api")
    app.include_router(llm_routes.router, prefix="/api")
    app.include_router(export.router, prefix="/api")
    app.include_router(compile_routes.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(generate.router, prefix="/api")

    web_dist = REPO_ROOT / "web" / "dist"
    if (web_dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    else:

        @app.get("/")
        def _frontend_not_built() -> dict[str, str]:
            """Explain how to get a frontend, when none is built yet.

            Registered only when ``web/dist/index.html`` is absent.
            Deliberately decided once per ``create_app()`` call rather
            than per request: whether the static build exists is a
            filesystem fact about this app's own routing table, not a
            per-request setting.
            """
            return {
                "message": (
                    "Swarm Builder's frontend has not been built yet. "
                    "Run `pnpm --dir web build`, or start a separate "
                    "Vite dev server and use the API directly at /api/*."
                )
            }

    return app


app = create_app()


def run() -> None:
    """Console-script entry point (``swarm-builder``).

    Prints the URL BEFORE calling ``uvicorn.run`` (which blocks), and
    re-raises an ``OSError`` from a failed bind (e.g. the port already
    in use) after printing a message naming ``PORT`` as the fix --
    deliberately no auto-increment fallback.

    Loads a repo-root ``.env`` first (shell values win; see
    :func:`swarm_builder.config.load_env_file`), so an API key placed there
    reaches the fill agent, the generator and every run subprocess.

    Binds ``SWARM_HOST``, which defaults to loopback and is set to
    ``0.0.0.0`` only by the container image, where a loopback bind would
    be unreachable through a published port.
    """
    loaded = load_env_file()
    if loaded:
        print(f"Loaded {len(loaded)} variable(s) from .env: {', '.join(loaded)}")
    host = get_host()
    port = get_port()
    print(f"Swarm Builder listening at http://{host}:{port}")
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except OSError as exc:
        print(
            f"swarm-builder: failed to bind {host}:{port} ({exc}). "
            f"Set the PORT environment variable to use a different port."
        )
        raise


if __name__ == "__main__":
    run()
