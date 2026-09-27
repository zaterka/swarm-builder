"""``GET /api/templates`` and ``GET /api/database-starters`` -- the two
catalogs the canvas creates nodes from.

``/api/templates`` serves id, label, description, default tools and
required env for every agent template the picker offers.
``/api/database-starters`` serves the default operation, example data and
mandatory I/O pair for each of the three database node kinds.

**Lazy import, deliberately -- this is a degradation seam.** Both catalogs
are used by these endpoints and by the compile pipeline, and nothing else:
importing either at module level would mean a broken catalog (a missing
``agent.py.tmpl``, a bad registry entry that raises at import, an
unreadable ``starter.json``) takes down the whole server at startup,
including the endpoints a user needs to *diagnose* that. Imported inside
the handlers, a broken catalog degrades to a clear ``503`` on that one
route and every other endpoint keeps working.

**Why this has a ``response_model``.** The frontend's TypeScript types are
generated from this server's own OpenAPI schema (``scripts/
generate_web_types.py``, "one schema, two consumers"). A bare
``list[dict[str, object]]`` return annotation types every field as
``unknown`` in the generated TypeScript -- correct at runtime but useless
to a generated client. Declaring :class:`TemplateEntryOut` and
:class:`DatabaseStarterOut` gives the schema named, correctly-typed fields
every other route in this package already provides.

**Nothing here re-declares a starter.** ``spec``/``io`` are read off the
landed catalog (:mod:`swarm_builder.templates.database`) field for field,
including ``envVars`` -- a second copy of a default operation or of a
seed in this file would drift from the file the generated project is
actually rendered from.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from swarm_builder.models import NodeIo, NosqlSpec, SqlSpec, VectorSpec

router = APIRouter(tags=["templates"])

#: The closed set of database node kinds this route can serve.
#:
#: Declared here rather than imported from the catalog because the catalog
#: must not be imported at module level (see the module docstring -- it reads
#: three ``starter.json`` files at import). It is the *response schema's*
#: closed set: it is what makes the generated TypeScript a three-member union
#: instead of a bare ``string``, which is what lets the frontend index a
#: starter map by node kind with no cast. A fourth catalog kind therefore
#: fails loudly here (the response model refuses to validate it) instead of
#: silently widening the served type.
DatabaseStarterKind = Literal["sql", "nosql", "vector"]


class TemplateEntryOut(BaseModel):
    """The five picker-facing fields of one template catalog entry.

    ``deps`` and ``file_manifest`` are internal scaffolding concerns the
    picker has no use for, so they are deliberately not exposed.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    label: str
    description: str
    default_tools: list[str]
    required_env: list[str]


@router.get("/templates", response_model=list[TemplateEntryOut])
def list_templates() -> list[TemplateEntryOut]:
    """Return the template catalog's five picker-facing fields.

    Raises:
        fastapi.HTTPException: 503 if the template registry cannot be
            imported, so one broken subsystem fails this route rather
            than server startup.
    """
    try:
        # Lazy on purpose (see module docstring): keeps a broken registry
        # from taking down every other endpoint at import time.
        from swarm_builder.templates.registry import TEMPLATE_CATALOG
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "templates are not available: "
                "swarm_builder.templates.registry could not be imported"
            ),
        ) from exc

    return [
        TemplateEntryOut(
            id=entry.id,
            label=entry.label,
            description=entry.description,
            default_tools=list(entry.default_tools),
            required_env=list(entry.required_env),
        )
        for entry in TEMPLATE_CATALOG.values()
    ]


class DatabaseStarterOut(BaseModel):
    """One database kind's picker-facing entry.

    ``spec`` and ``io`` are the whole reason this route exists: a new node
    copies both into the graph document, so the document is self-contained
    and the Inspector shows what will actually run (nothing is substituted at
    scaffold time). ``spec`` is the union of the three spec models, and it is
    the kind's *validated* model on the server side -- a malformed
    ``starter.json`` fails while the catalog is being read, never inside a
    generated project.

    The scaffolding-only fields of the catalog entry (``package_module``,
    ``live_extra_deps``, ``manifest``) are deliberately not exposed:
    ``liveExtra`` names the extra a user must install and ``envVars`` lists
    the variables the live engine reads, which is everything the canvas can
    honestly tell a user. ``SWARM_DB_MODE`` is app-wide rather than an
    engine's, so it is not part of any entry's ``envVars``.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    kind: DatabaseStarterKind
    label: str
    description: str
    spec: SqlSpec | NosqlSpec | VectorSpec
    io: NodeIo
    live_extra: str
    env_vars: list[str]


@router.get("/database-starters", response_model=list[DatabaseStarterOut])
def list_database_starters() -> list[DatabaseStarterOut]:
    """Return one starter entry per database kind, in catalog order.

    Raises:
        fastapi.HTTPException: 503 if the database starter catalog cannot be
            read, so one broken subsystem fails this route rather than server
            startup.
    """
    try:
        # Lazy on purpose (see module docstring). This package is heavier than
        # the agent registry: building DATABASE_CATALOG reads and validates
        # three starter.json files, so its failure modes are file-level
        # (missing, unreadable, malformed) as well as import-level. Every one
        # of them means the same thing to the picker -- "there are no
        # starters right now, do not offer a node you cannot materialize" --
        # so they all degrade to this route's documented 503 rather than a
        # bare 500 the palette would have no state for. The message names the
        # cause, so the failure is still diagnosable from the response.
        from swarm_builder.templates.database import DATABASE_CATALOG
    except (ImportError, OSError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "database starters are not available: "
                f"swarm_builder.templates.database could not be read ({exc})"
            ),
        ) from exc

    return [
        DatabaseStarterOut(
            kind=entry.kind,
            label=entry.label,
            description=entry.description,
            spec=entry.starter_spec,
            io=entry.starter_io,
            live_extra=entry.live_extra_name,
            env_vars=list(entry.env_vars),
        )
        for entry in DATABASE_CATALOG.values()
    ]
