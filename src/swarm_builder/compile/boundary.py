"""Phase-4 boundary check (PLAN.md "The compile pipeline" Phase 4).

Two-tier, because Phase 3 legitimately rewrites the permitted files, so
a whole-file hash cannot cover them (I3):

1. **Forbidden files** -- every scaffolded file that is *not* a
   marker-bearing ``steps/*.py`` or ``agents/*.py`` module (this
   includes, but is not limited to, the PLAN.md-named ``graph.py``,
   ``state.py``, ``deps.py``, ``pyproject.toml``, and everything under
   ``validate/``) -- must stay byte-identical to the Phase-2 baseline.
   Compared by whole-file SHA-256.
2. **Permitted files** -- ``steps/*.py`` and ``agents/*.py`` -- are
   parsed into their ``imports``/``begin``/``end`` marker regions.
   Every marker must still exist, the body region must be non-empty,
   and the concatenation of everything *outside* both regions is
   hashed and compared against the baseline, so the fill agent (or a
   template/emitter bug) cannot edit around its own sandbox.

Any file present now that was not part of the Phase-2 baseline is a
third finding: a file created outside the scaffolded set.

:func:`capture_baseline` snapshots a freshly scaffolded project
directory; :func:`check_boundary` re-walks the same directory later and
raises :class:`BoundaryViolationError` naming every violation found --
never just the first -- so a caller cannot silently proceed on a
partially-broken project. A clean run returns a
:class:`BoundaryCheckResult` with no violations instead of raising.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from swarm_builder.compile import (
    body_marker_begin,
    body_marker_end,
    imports_marker_begin,
    imports_marker_end,
)

#: Relative (POSIX-style) directories whose marker-bearing modules are
#: the ONLY files the fill agent may edit (PLAN.md Phase 3 tool table:
#: "the editable regions ... inside steps/*.py and agents/*.py").
_STEPS_DIR = PurePosixPath("src/swarm_workflow/steps")
_AGENTS_DIR = PurePosixPath("src/swarm_workflow/agents")

#: Directory names never treated as part of the scaffolded set: both are
#: produced by *running* Python against the project (the golden-render
#: subprocess scaffold.py itself shells out to, and anything Phase 5's
#: `uv sync`/import steps would add), never by scaffold.py's own writes,
#: so tracking them would make every check fail on incidental bytecode
#: caching rather than a real boundary violation.
_EXCLUDED_DIR_NAMES = frozenset({"__pycache__", ".venv", ".git"})
_EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo")

#: File names excluded for the same reason as the directories above:
#: Phase 5's ``uv sync`` writes a lockfile into the project directory,
#: and the fill agent has no tool that could create one.
#:
#: Omitting this made the documented fill retry structurally dead. The
#: retry re-runs Phase 3 -> 4 -> 5 against the baseline captured at the
#: end of Phase 2, so on the second pass the lockfile Phase 5 had just
#: written registered as ``unexpected_new_file`` and the compile failed
#: in Phase 4 -- reporting a boundary violation naming a file the model
#: never touched, instead of the dry-run failure that triggered the
#: retry. Every phase-5-failure retry, which is the case the retry
#: exists for, ended that way.
_EXCLUDED_FILE_NAMES = frozenset({"uv.lock"})


class BoundaryScaffoldError(Exception):
    """Raised by :func:`capture_baseline` when a file under ``steps/`` or
    ``agents/`` is missing one of its four required markers.

    This signals a bug in ``scaffold.py`` or a template ``.tmpl`` file,
    not a fill-time violation -- Phase 2's own output must already be
    well-formed before there is any baseline to check against.
    """

    def __init__(self, path: PurePosixPath, missing: tuple[str, ...]) -> None:
        self.path = path
        self.missing = missing
        super().__init__(
            f"scaffolded file {path} is missing required marker(s): {', '.join(missing)}"
        )


class BoundaryViolationError(Exception):
    """Raised by :func:`check_boundary` whenever one or more violations
    are found, carrying the complete :class:`BoundaryCheckResult` so no
    caller can react to only the first problem.
    """

    def __init__(self, result: BoundaryCheckResult) -> None:
        self.result = result
        summary = "; ".join(f"{v.code} ({v.path})" if v.path else v.code for v in result.violations)
        super().__init__(f"boundary check found {len(result.violations)} violation(s): {summary}")


@dataclass(frozen=True)
class Violation:
    """One boundary violation.

    Attributes:
        code: Machine-readable violation kind (e.g.
            ``"forbidden_file_changed"``).
        message: Human-readable explanation.
        path: The scaffolded-project-relative POSIX path involved, or
            ``None`` for a project-wide finding.
    """

    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class BoundaryCheckResult:
    """Every violation found by one :func:`check_boundary` run."""

    violations: tuple[Violation, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class RegionSnapshot:
    """Baseline structure of one marker-bearing ``steps/``/``agents/``
    module, captured at the end of Phase 2.

    Attributes:
        outside_hash: SHA-256 of every character outside both the
            ``imports`` and ``body`` marker regions (the marker lines
            themselves count as "outside": they are scaffolding, never
            rewritten by ``write_region``).
    """

    outside_hash: str


@dataclass(frozen=True)
class ProjectBaseline:
    """Snapshot of a scaffolded project directory, taken at the end of
    Phase 2, that :func:`check_boundary` later compares against.

    Attributes:
        project_dir: The project directory this baseline describes.
        forbidden_hashes: Relative POSIX path -> whole-file SHA-256, for
            every scaffolded file that is not a permitted marker file.
        permitted_regions: Relative POSIX path -> :class:`RegionSnapshot`,
            for every ``steps/*.py``/``agents/*.py`` module.
        scaffolded_paths: Every relative POSIX path present at capture
            time, forbidden and permitted alike -- the reference set for
            detecting a file created outside the scaffolded set.
    """

    project_dir: Path
    forbidden_hashes: dict[str, str] = field(default_factory=dict)
    permitted_regions: dict[str, RegionSnapshot] = field(default_factory=dict)
    scaffolded_paths: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class _MarkerLine:
    """Character offsets of one located marker line, including its
    trailing newline (or end-of-file, for a marker on the last line)."""

    start: int
    end: int


@dataclass(frozen=True)
class _ParsedRegions:
    """The four marker lines located in one file's text, any of which
    may be absent (``None``) if the fill agent removed one."""

    imports_begin: _MarkerLine | None
    imports_end: _MarkerLine | None
    body_begin: _MarkerLine | None
    body_end: _MarkerLine | None

    def missing_marker_names(self) -> tuple[str, ...]:
        names = []
        if self.imports_begin is None:
            names.append("imports-begin")
        if self.imports_end is None:
            names.append("imports-end")
        if self.body_begin is None:
            names.append("begin")
        if self.body_end is None:
            names.append("end")
        return tuple(names)


def _is_permitted_marker_path(rel_path: PurePosixPath) -> bool:
    """True for a ``steps/*.py``/``agents/*.py`` node module.

    Excludes each directory's own ``__init__.py`` (a literal file with
    no node id and no markers, per ``scaffold.py``'s
    ``_render_package_init``), which therefore belongs in the
    whole-file-hash tier like any other non-marker scaffolded file.
    """
    if rel_path.suffix != ".py" or rel_path.name == "__init__.py":
        return False
    return rel_path.parent in (_STEPS_DIR, _AGENTS_DIR)


def _iter_project_files(project_dir: Path) -> Iterator[Path]:
    """Yield every regular file under ``project_dir``, deterministically
    ordered, excluding files and directories produced by running the
    project rather than by scaffolding it (see
    :data:`_EXCLUDED_DIR_NAMES` and :data:`_EXCLUDED_FILE_NAMES`)."""
    for path in sorted(project_dir.rglob("*")):
        if path.is_dir():
            continue
        relative_parts = path.relative_to(project_dir).parts[:-1]
        if any(part in _EXCLUDED_DIR_NAMES for part in relative_parts):
            continue
        if path.suffix in _EXCLUDED_FILE_SUFFIXES:
            continue
        if path.name in _EXCLUDED_FILE_NAMES:
            continue
        yield path


def _find_marker_line(text: str, marker_text: str) -> _MarkerLine | None:
    """Locate the first line whose content, ignoring leading/trailing
    whitespace, equals ``marker_text`` (body markers are indented one
    level inside a function body, per PLAN.md Phase 4)."""
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.strip() == marker_text:
            return _MarkerLine(start=offset, end=offset + len(line))
        offset += len(line)
    return None


def _parse_regions(text: str, node_id: str) -> _ParsedRegions:
    """Locate all four markers for ``node_id`` in a module's ``text``."""
    return _ParsedRegions(
        imports_begin=_find_marker_line(text, imports_marker_begin(node_id)),
        imports_end=_find_marker_line(text, imports_marker_end(node_id)),
        body_begin=_find_marker_line(text, body_marker_begin(node_id)),
        body_end=_find_marker_line(text, body_marker_end(node_id)),
    )


def _outside_marker_text(text: str, regions: _ParsedRegions) -> str:
    """Concatenate everything in ``text`` outside both marker regions.

    Requires all four markers in ``regions`` to be present; callers must
    check :meth:`_ParsedRegions.missing_marker_names` first.
    """
    assert regions.imports_begin is not None
    assert regions.imports_end is not None
    assert regions.body_begin is not None
    assert regions.body_end is not None
    return (
        text[: regions.imports_begin.end]
        + text[regions.imports_end.start : regions.body_begin.end]
        + text[regions.body_end.start :]
    )


def _body_region_is_empty(text: str, regions: _ParsedRegions) -> bool:
    """True when the text strictly between the body markers has no
    non-whitespace content."""
    assert regions.body_begin is not None
    assert regions.body_end is not None
    body_text = text[regions.body_begin.end : regions.body_end.start]
    return body_text.strip() == ""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(data: str) -> str:
    return _sha256_bytes(data.encode("utf-8"))


def capture_baseline(project_dir: Path) -> ProjectBaseline:
    """Snapshot ``project_dir`` at the end of Phase 2, for later
    comparison by :func:`check_boundary`.

    Args:
        project_dir: A freshly scaffolded project directory (no `uv
            sync`, import, or fill run has touched it yet, beyond the
            golden-render subprocess ``scaffold.py`` itself invokes,
            whose ``__pycache__`` output is excluded).

    Returns:
        The baseline snapshot: whole-file hashes for every forbidden
        (non-marker) file, region structure for every permitted
        ``steps/*.py``/``agents/*.py`` module, and the complete set of
        scaffolded relative paths.

    Raises:
        BoundaryScaffoldError: If a ``steps/*.py``/``agents/*.py``
            module is missing one of its four required markers -- a
            scaffolding bug, since Phase 2 must always emit all four.
    """
    resolved_root = project_dir.resolve()
    forbidden_hashes: dict[str, str] = {}
    permitted_regions: dict[str, RegionSnapshot] = {}
    scaffolded_paths: set[str] = set()

    for path in _iter_project_files(resolved_root):
        rel_path = PurePosixPath(path.relative_to(resolved_root).as_posix())
        scaffolded_paths.add(str(rel_path))

        if _is_permitted_marker_path(rel_path):
            node_id = rel_path.stem
            text = path.read_text(encoding="utf-8")
            regions = _parse_regions(text, node_id)
            missing = regions.missing_marker_names()
            if missing:
                raise BoundaryScaffoldError(rel_path, missing)
            permitted_regions[str(rel_path)] = RegionSnapshot(
                outside_hash=_sha256_text(_outside_marker_text(text, regions))
            )
        else:
            forbidden_hashes[str(rel_path)] = _sha256_bytes(path.read_bytes())

    return ProjectBaseline(
        project_dir=resolved_root,
        forbidden_hashes=forbidden_hashes,
        permitted_regions=permitted_regions,
        scaffolded_paths=frozenset(scaffolded_paths),
    )


def check_boundary(project_dir: Path, baseline: ProjectBaseline) -> BoundaryCheckResult:
    """Compare ``project_dir`` against ``baseline`` and report every
    boundary violation found.

    Checks, in order, and never stopping at the first violation:

    - a changed forbidden (non-marker) file, named by path;
    - a scaffolded file that disappeared entirely;
    - a missing ``imports``/``begin``/``end`` marker in a permitted
      file;
    - an empty body region in a permitted file;
    - changed text outside a permitted file's marker regions;
    - a file created outside the scaffolded set.

    Args:
        project_dir: The project directory to check, normally after
            Phase 3's fill run.
        baseline: The snapshot :func:`capture_baseline` took at the end
            of Phase 2.

    Returns:
        A :class:`BoundaryCheckResult` with no violations. Only
        returned when the project is clean -- otherwise this function
        raises instead, so a violation can never be silently dropped.

    Raises:
        BoundaryViolationError: If one or more violations are found;
            the exception carries the full :class:`BoundaryCheckResult`
            listing every one of them.
    """
    resolved_root = project_dir.resolve()
    violations: list[Violation] = []

    current_paths = {
        str(PurePosixPath(path.relative_to(resolved_root).as_posix()))
        for path in _iter_project_files(resolved_root)
    }

    for rel_path, expected_hash in baseline.forbidden_hashes.items():
        full_path = resolved_root / rel_path
        if rel_path not in current_paths:
            violations.append(
                Violation(
                    code="missing_scaffolded_file",
                    message=f"forbidden file {rel_path} no longer exists",
                    path=rel_path,
                )
            )
            continue
        actual_hash = _sha256_bytes(full_path.read_bytes())
        if actual_hash != expected_hash:
            violations.append(
                Violation(
                    code="forbidden_file_changed",
                    message=f"forbidden file {rel_path} was modified",
                    path=rel_path,
                )
            )

    for rel_path, expected in baseline.permitted_regions.items():
        full_path = resolved_root / rel_path
        if rel_path not in current_paths:
            violations.append(
                Violation(
                    code="missing_scaffolded_file",
                    message=f"permitted file {rel_path} no longer exists",
                    path=rel_path,
                )
            )
            continue

        node_id = PurePosixPath(rel_path).stem
        text = full_path.read_text(encoding="utf-8")
        regions = _parse_regions(text, node_id)
        missing = regions.missing_marker_names()
        if missing:
            violations.append(
                Violation(
                    code="missing_marker",
                    message=f"{rel_path} is missing marker(s): {', '.join(missing)}",
                    path=rel_path,
                )
            )
            # Slicing outside-marker text needs all four offsets, so
            # there is nothing further to compare safely for this file.
            continue

        if _body_region_is_empty(text, regions):
            violations.append(
                Violation(
                    code="empty_body_region",
                    message=f"{rel_path} has an empty body region",
                    path=rel_path,
                )
            )

        actual_hash = _sha256_text(_outside_marker_text(text, regions))
        if actual_hash != expected.outside_hash:
            violations.append(
                Violation(
                    code="outside_marker_text_changed",
                    message=f"{rel_path} was edited outside its marker regions",
                    path=rel_path,
                )
            )

    for rel_path in sorted(current_paths - baseline.scaffolded_paths):
        violations.append(
            Violation(
                code="unexpected_new_file",
                message=f"{rel_path} was created outside the scaffolded set",
                path=rel_path,
            )
        )

    result = BoundaryCheckResult(violations=tuple(violations))
    if not result.ok:
        raise BoundaryViolationError(result)
    return result


__all__ = [
    "BoundaryCheckResult",
    "BoundaryScaffoldError",
    "BoundaryViolationError",
    "ProjectBaseline",
    "RegionSnapshot",
    "Violation",
    "capture_baseline",
    "check_boundary",
]
