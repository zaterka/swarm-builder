"""Generate ``web/src/types.ts`` from the FastAPI server's own OpenAPI
schema (PLAN.md: "the frontend's TypeScript types are generated from the
FastAPI OpenAPI schema rather than hand-maintained").

**Why this script exists before ``web/`` does.** Group 6 (the frontend)
has not landed yet, so ``web/src/`` does not exist in this checkout. This
script is nonetheless the seam Group 6 is expected to consume: it writes
``<repo_root>/web/src/types.ts``, creating the intervening directories on
demand, so running it once now (or at any point later, including after
Group 6 lands and starts editing files alongside it) produces a file at
exactly the path PLAN.md's repository layout names
(``web/src/types.ts``).

**One schema, two consumers (models.py's own module docstring).**
``models.py`` is the single source of truth for the graph document.
Hand-maintaining a parallel TypeScript shape would let the two drift;
generating the TypeScript from the server's own OpenAPI document (which
is itself generated from the same pydantic models FastAPI's routes
already return) removes that drift risk entirely. This script is the
*mechanism*, not a new source of truth.

**No new Python dependency.** The OpenAPI JSON comes from constructing
the app in-process (``swarm_builder.main.create_app()``) and calling its
own ``.openapi()`` method -- no server needs to be running, no network
call happens for that half. Converting that JSON into TypeScript uses
``npx openapi-typescript@7`` (Node, already available in this
environment; see the project's own toolchain, PLAN.md fact 11) rather
than adding a Python codegen dependency to ``pyproject.toml``, which
Group 3 does not own and must not edit for this.

**Best-effort by design.** If ``npx``/network access is unavailable in
whatever environment runs this script, it still writes the raw
``openapi.json`` snapshot next to the target path and reports the
``npx`` failure clearly, rather than crashing uninformatively -- Group 6
can always regenerate the TypeScript from that JSON later once network
access is available.

Usage::

    UV_CACHE_DIR=<...> uv run python scripts/generate_web_types.py

Regenerate this any time a route's request/response shape or
``models.py`` changes -- it is not committed-and-forgotten; it is meant
to be re-run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_SRC = REPO_ROOT / "web" / "src"
OPENAPI_JSON_PATH = WEB_SRC / "openapi.json"
TYPES_TS_PATH = WEB_SRC / "types.ts"

#: A project-local npm cache directory, set as `npm_config_cache` for the
#: `npx` subprocess below. Mirrors this project's own UV_CACHE_DIR
#: convention (fact 10: the user's default `~/.cache/uv` is not
#: writable in this environment) -- probed during this task and found
#: that the user's default `~/.npm` cache has root-owned files left
#: over from an old npm bug (`EPERM` on `~/.npm/_cacache/tmp/...`),
#: which fails `npx` the exact same way an unwritable `~/.cache/uv`
#: fails a bare `uv sync`. Never touches `~/.npm` -- points npm at a
#: directory this project already owns instead.
NPM_CACHE_DIR = REPO_ROOT / ".npm-cache"

#: Pinned major version, matching this project's convention of pinning
#: every dependency version explicitly (PLAN.md "Dependency versions").
#: openapi-typescript 7.x's CLI accepts a schema on stdin via `-`.
OPENAPI_TYPESCRIPT_SPEC = "openapi-typescript@7"


def _build_openapi_schema() -> dict:
    """Construct the FastAPI app in-process and return its OpenAPI
    document. No server needs to be running; no network call."""
    # Imported here, not at module top, so this script can be inspected
    # or partially reused without requiring the full server package to
    # already be importable (e.g. from a minimal checkout).
    from swarm_builder.main import create_app

    app = create_app()
    return app.openapi()


def main() -> int:
    WEB_SRC.mkdir(parents=True, exist_ok=True)

    schema = _build_openapi_schema()
    schema_text = json.dumps(schema, indent=2)
    OPENAPI_JSON_PATH.write_text(schema_text + "\n", encoding="utf-8")
    print(f"Wrote {OPENAPI_JSON_PATH.relative_to(REPO_ROOT)} ({len(schema['paths'])} paths).")

    try:
        result = subprocess.run(
            [
                "npx",
                "--yes",
                OPENAPI_TYPESCRIPT_SPEC,
                str(OPENAPI_JSON_PATH),
                "-o",
                str(TYPES_TS_PATH),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env={**os.environ, "npm_config_cache": str(NPM_CACHE_DIR)},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"npx openapi-typescript could not be run ({exc}). "
            f"The raw OpenAPI schema is still available at "
            f"{OPENAPI_JSON_PATH.relative_to(REPO_ROOT)} -- rerun this "
            f"script once npx/network access is available to produce "
            f"{TYPES_TS_PATH.relative_to(REPO_ROOT)}.",
            file=sys.stderr,
        )
        return 1

    if result.returncode != 0:
        print(
            f"npx openapi-typescript exited {result.returncode}:\n{result.stderr}\n"
            f"The raw OpenAPI schema is still available at "
            f"{OPENAPI_JSON_PATH.relative_to(REPO_ROOT)}.",
            file=sys.stderr,
        )
        return 1

    # `npx openapi-typescript ... -o <path>` already wrote TYPES_TS_PATH
    # directly -- nothing further to write here.
    print(f"Wrote {TYPES_TS_PATH.relative_to(REPO_ROOT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
