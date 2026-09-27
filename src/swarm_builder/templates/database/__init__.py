"""Database starter catalog: the default operation, example data and file set
for each database node kind.

**Why this is separate from :mod:`swarm_builder.templates.registry`.** That
module is the *agent* template catalog: ``TemplateId``, ``infer_template`` and
``TEMPLATE_CATALOG`` are consumed by the emitter, the frontend and Phase 1, and
an existing test pins its size at exactly three entries. A database node has no
template -- it has a *kind* -- so folding these entries in would mean either a
narrower ``TemplateId`` literal or a union of two unrelated shapes. Keeping them
apart leaves both contracts exact.

**How a starter is used.** The Inspector fetches this catalog when a node is
created and copies ``starter_spec`` + ``starter_io`` into the graph document.
Nothing is applied at scaffold time: after creation the document is
self-contained and the Inspector shows what will actually run. That is why
every accessor here returns a *validated model* rather than the raw JSON -- a
malformed starter has to fail while the catalog is being read, not in a
generated project nobody inspected.

**File layout.** ``<kind>/starter.json`` carries the label, description, default
spec and the mandatory I/O pair; ``<kind>/repository.py.tmpl`` and
``<kind>/factory.py.tmpl`` are the adapter and its factory block;
``portshape.py.tmpl``, ``embedding.py.tmpl`` and ``factory.py.tmpl`` are shared
by every kind. Every ``.tmpl`` is rendered by
:mod:`swarm_builder.compile.database` with ``string.Template``, which means a
literal ``$`` in a template is written ``$$`` -- the NoSQL adapter's
``$$eq``-style operator names are escaped that way and come out as single ``$``
in the rendered module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swarm_builder.models import NodeIo, NosqlSpec, SqlSpec, VectorSpec

#: The directory every template below is resolved against.
DATABASE_DIR = Path(__file__).resolve().parent

#: The three kinds, in the order everything emitted is ordered by: catalog
#: entries, ``[project.optional-dependencies]`` lines, ``.env.example`` lines
#: and the rendered repository modules. One order, so two compiles of one
#: document cannot produce two different files.
KIND_ORDER = ("sql", "nosql", "vector")

#: Split markers inside ``<kind>/factory.py.tmpl``: the block above the second
#: marker is the kind's import lines, the block below it is its getter(s).
#: Markers rather than two files, because the two blocks only make sense
#: together and a second file per kind would be one more thing to keep in step.
KIND_IMPORTS_MARKER = "# --- swarm:db-kind-imports ---"
KIND_GETTERS_MARKER = "# --- swarm:db-kind-getters ---"

#: The templates every kind shares (paths relative to ``DATABASE_DIR``), listed
#: so a test can assert the manifest is complete rather than trusting the
#: renderer to be exhaustive.
SHARED_TEMPLATE_FILES = ("portshape.py.tmpl", "embedding.py.tmpl", "factory.py.tmpl")

#: kind -> the model its ``starter.json`` ``spec`` must validate into.
_SPEC_MODELS: dict[str, type[SqlSpec] | type[NosqlSpec] | type[VectorSpec]] = {
    "sql": SqlSpec,
    "nosql": NosqlSpec,
    "vector": VectorSpec,
}

#: The static, code-side half of each entry: which generated module the kind
#: maps to, the opt-in extra that makes it live, and the environment variables
#: that extra needs. Kept here rather than in the JSON because these are facts
#: about the *emitted project's packaging*, not about a node's default data.
_METADATA: dict[str, dict[str, Any]] = {
    "sql": {
        "package_module": "sql",
        "live_extra_name": "live-sql",
        "live_extra_deps": ("psycopg[binary]>=3.2",),
        "env_vars": ("SWARM_SQL_DSN",),
        "manifest": ("starter.json", "repository.py.tmpl", "factory.py.tmpl"),
    },
    "nosql": {
        "package_module": "nosql",
        "live_extra_name": "live-nosql",
        "live_extra_deps": ("pymongo>=4.9",),
        # The DSN names the database; the collection is the node's own declared
        # one, with this variable as the fallback for a caller that has only a
        # seed path.
        "env_vars": ("SWARM_NOSQL_DSN", "SWARM_NOSQL_COLLECTION"),
        "manifest": ("starter.json", "repository.py.tmpl", "factory.py.tmpl"),
    },
    "vector": {
        "package_module": "vector",
        "live_extra_name": "live-vector",
        "live_extra_deps": ("qdrant-client>=1.12",),
        # Qdrant addresses a *collection* as well as a URL, and its URL has
        # nowhere to put one -- so the collection is either the node's declared
        # one or this variable, never an invented default. The API key sits with
        # the DSN (both are facts about the connection, not about which
        # collection is read) and is optional: a local Qdrant is normally
        # unauthenticated, so only the DSN is required to go live.
        "env_vars": (
            "SWARM_VECTOR_DSN",
            "SWARM_VECTOR_API_KEY",
            "SWARM_VECTOR_COLLECTION",
        ),
        "manifest": ("starter.json", "repository.py.tmpl", "factory.py.tmpl"),
    },
}


@dataclass(frozen=True)
class DatabaseKindEntry:
    """One database kind's catalog entry, as served by ``GET /api/database-starters``.

    ``label``/``description`` and the starter pair are the picker-facing fields
    (they are what a new node is created from); ``package_module``,
    ``live_extra_name``/``live_extra_deps``, ``env_vars`` and ``manifest`` are
    the scaffolding concerns behind them. ``env_vars`` lists *every* variable the
    live engine needs, because the generated README has to name all of them and
    the ``.env.example`` is written from the same tuple.
    """

    kind: str
    label: str
    description: str
    #: Basename of the generated repository module under ``repositories/``
    #: (``sql`` -> ``repositories/sql.py``).
    package_module: str
    #: The default operation, validated into its kind's spec model.
    starter_spec: SqlSpec | NosqlSpec | VectorSpec
    #: The mandatory I/O pair for the kind (``str -> list[json]``). Copied into
    #: the node at creation because a database node's ports are not a choice.
    starter_io: NodeIo
    #: The opt-in extra that installs the live driver, and its requirement
    #: strings -- one extra per engine, so a sql+vector graph needs both.
    live_extra_name: str
    live_extra_deps: tuple[str, ...]
    #: Every environment variable the live engine reads.
    env_vars: tuple[str, ...]
    #: Relative paths under ``templates/database/<kind>/`` this kind ships.
    manifest: tuple[str, ...]


def _kind_directory(kind: str) -> Path:
    """The ``templates/database/<kind>/`` directory, for a known kind only."""
    if kind not in KIND_ORDER:
        raise ValueError(
            f"unknown database kind {kind!r}; expected one of {list(KIND_ORDER)}"
        )
    return DATABASE_DIR / kind


def _read_starter_json(kind: str) -> dict[str, Any]:
    """Read one kind's ``starter.json`` verbatim, at call time.

    Private on purpose: the raw payload never leaves this module, because a
    caller holding the unvalidated dict is a caller that can compile a project
    from a starter the schema would have rejected.
    """
    path = _kind_directory(kind) / "starter.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must hold a JSON object")
    return payload


def _starter_text(kind: str, key: str) -> str:
    """One required non-empty string field of ``starter.json``."""
    value = _read_starter_json(kind).get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{_kind_directory(kind) / 'starter.json'} has no {key!r} string")
    return value


def load_starter_spec(kind: str) -> SqlSpec | NosqlSpec | VectorSpec:
    """Read and validate one kind's default spec.

    Args:
        kind: ``sql``, ``nosql`` or ``vector``.

    Returns:
        The starter's spec as its kind's model, so a malformed starter fails
        here rather than inside a generated project.

    Raises:
        ValueError: If ``kind`` is unknown, or the file's ``spec`` object does
            not validate against that kind's spec model.
    """
    spec = _read_starter_json(kind).get("spec")
    if not isinstance(spec, dict):
        raise ValueError(
            f"{_kind_directory(kind) / 'starter.json'} has no 'spec' object"
        )
    return _SPEC_MODELS[kind].model_validate(spec)


def load_starter_io(kind: str) -> NodeIo:
    """Read and validate one kind's mandatory I/O pair (``str -> list[json]``).

    Args:
        kind: ``sql``, ``nosql`` or ``vector``.

    Returns:
        The starter's declared ports, copied into a new node at creation.

    Raises:
        ValueError: If ``kind`` is unknown or the file's ``io`` object does not
            validate.
    """
    io = _read_starter_json(kind).get("io")
    if not isinstance(io, dict):
        raise ValueError(f"{_kind_directory(kind) / 'starter.json'} has no 'io' object")
    return NodeIo.model_validate(io)


def load_starter_label(kind: str) -> str:
    """The starter's picker label."""
    return _starter_text(kind, "label")


def load_starter_description(kind: str) -> str:
    """The starter's picker description."""
    return _starter_text(kind, "description")


def _load_entry(kind: str) -> DatabaseKindEntry:
    """Assemble one catalog entry from the starter file and :data:`_METADATA`."""
    metadata = _METADATA[kind]
    return DatabaseKindEntry(
        kind=kind,
        label=load_starter_label(kind),
        description=load_starter_description(kind),
        package_module=metadata["package_module"],
        starter_spec=load_starter_spec(kind),
        starter_io=load_starter_io(kind),
        live_extra_name=metadata["live_extra_name"],
        live_extra_deps=metadata["live_extra_deps"],
        env_vars=metadata["env_vars"],
        manifest=metadata["manifest"],
    )


#: kind -> its catalog entry. Built through the same loaders a reload would use,
#: so the served catalog and a call-time reload cannot disagree about what a
#: starter is; the entries themselves are frozen and hold validated models.
DATABASE_CATALOG: dict[str, DatabaseKindEntry] = {
    kind: _load_entry(kind) for kind in KIND_ORDER
}


def get_database_entry(kind: str) -> DatabaseKindEntry:
    """One kind's catalog entry.

    Args:
        kind: ``sql``, ``nosql`` or ``vector``.

    Returns:
        The kind's entry, its spec and I/O already validated.

    Raises:
        ValueError: If ``kind`` is not a database kind, naming the known ones --
            an unknown kind must never degrade into "no database node".
    """
    entry = DATABASE_CATALOG.get(kind)
    if entry is None:
        raise ValueError(
            f"unknown database kind {kind!r}; expected one of {list(KIND_ORDER)}"
        )
    return entry


def read_template(relative_path: str) -> str:
    """Read one template file under ``templates/database/``, at call time.

    Every ``.tmpl`` here is rendered with ``string.Template``, and the only
    substitutions are ``$package`` plus, in ``factory.py.tmpl``,
    ``$kind_imports`` and ``$kind_getters``. Reading at call time is deliberate:
    a renderer that cached a template would emit a project from a file that has
    since changed.

    Args:
        relative_path: Path relative to ``templates/database/``, such as
            ``"sql/repository.py.tmpl"``.

    Returns:
        The file's text, unrendered.

    Raises:
        FileNotFoundError: If the path names no template, so a typo in a
            renderer is a named failure rather than a missing output file.
    """
    path = DATABASE_DIR / relative_path
    if not path.is_file():
        raise FileNotFoundError(f"no database template at {relative_path!r}")
    return path.read_text(encoding="utf-8")


__all__ = [
    "DATABASE_CATALOG",
    "DATABASE_DIR",
    "KIND_GETTERS_MARKER",
    "KIND_IMPORTS_MARKER",
    "KIND_ORDER",
    "SHARED_TEMPLATE_FILES",
    "DatabaseKindEntry",
    "get_database_entry",
    "load_starter_description",
    "load_starter_io",
    "load_starter_label",
    "load_starter_spec",
    "read_template",
]
