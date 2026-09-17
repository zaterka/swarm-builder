#!/usr/bin/env python
"""Report whether the committed ``web/dist`` bundle is stale.

``web/dist`` is committed on purpose: the server serves the canvas from
it and shows a "frontend has not been built yet" message when it is
absent, so a clone without it cannot render the UI without Node and pnpm.
The cost of shipping a built artefact is that it can drift from
``web/src``, and a stale bundle fails in a way that looks like a UI bug.
This script is the guard.

Two consumers, deliberately different in severity:

* **Run directly** (``uv run python scripts/check_dist.py``) or from CI:
  exits **1** when stale, so a pre-commit hook or pipeline can enforce it.
* **Via the test suite** (``tests/test_web_dist.py``): reports the same
  finding as a ``pytest`` warning and passes. A stale bundle is a release
  hygiene problem, not a correctness one, so it must not turn the suite
  red for a contributor who only touched Python.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "web" / "dist"
SRC_DIR = REPO_ROOT / "web" / "src"
ENTRY_POINT = DIST_DIR / "index.html"

#: Source globs that, when newer than the bundle, mean the bundle predates
#: the code it is supposed to contain. Config files count: changing the
#: Vite config or package.json can change the built output.
SOURCE_GLOBS = ("**/*.ts", "**/*.tsx", "**/*.css", "**/*.html")
SOURCE_FILES = (
    REPO_ROOT / "web" / "package.json",
    REPO_ROOT / "web" / "vite.config.ts",
    REPO_ROOT / "web" / "tsconfig.json",
)


def newest_source_mtime() -> float | None:
    """Return the newest mtime across the frontend's sources.

    Returns:
        The newest modification time, or ``None`` when no source file
        exists (which means the check cannot be made).
    """
    newest: float | None = None
    for pattern in SOURCE_GLOBS:
        for path in SRC_DIR.glob(pattern):
            if path.is_file():
                mtime = path.stat().st_mtime
                newest = mtime if newest is None else max(newest, mtime)
    for path in SOURCE_FILES:
        if path.is_file():
            mtime = path.stat().st_mtime
            newest = mtime if newest is None else max(newest, mtime)
    return newest


def check() -> tuple[bool, str]:
    """Report whether the committed bundle is missing or stale.

    Returns:
        ``(is_stale, message)``. A missing bundle counts as stale, since
        the canvas cannot render without it.
    """
    if not ENTRY_POINT.is_file():
        return True, (
            f"{ENTRY_POINT.relative_to(REPO_ROOT)} is missing. The server cannot serve "
            "the canvas without it. Run `pnpm --dir web build` and commit web/dist."
        )

    newest = newest_source_mtime()
    if newest is None:
        return False, "no frontend sources found; skipping the staleness comparison"

    dist_mtime = ENTRY_POINT.stat().st_mtime
    if newest > dist_mtime:
        return True, (
            "web/dist is older than web/src, so the committed bundle does not match "
            "the sources. Run `pnpm --dir web build` and commit the result."
        )
    return False, "web/dist is up to date with web/src"


def main() -> int:
    """Print the check result and exit 1 when stale."""
    is_stale, message = check()
    print(("STALE: " if is_stale else "OK: ") + message)
    return 1 if is_stale else 0


if __name__ == "__main__":
    sys.exit(main())
