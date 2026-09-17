"""``GET /api/templates`` -- the template catalog.

Serves id, label, description, default tools and required env for every
template the picker offers.

**Lazy import, deliberately -- this is a degradation seam.**
``swarm_builder.templates.registry`` is used by this endpoint and by the
compile pipeline, and nothing else: importing it at module level would
mean a broken registry (a missing ``agent.py.tmpl``, a bad catalog entry
that raises at import) takes down the whole server at startup, including
the endpoints a user needs to *diagnose* that. Imported inside the
handler, a broken registry degrades to a clear ``503`` on this one route.

**Why this has a ``response_model``.** The frontend's TypeScript types are
generated from this server's own OpenAPI schema (``scripts/
generate_web_types.py``, "one schema, two consumers"). A bare
``list[dict[str, object]]`` return annotation types every field as
``unknown`` in the generated TypeScript -- correct at runtime but useless
to a generated client. Declaring :class:`TemplateEntryOut` gives the
schema the same five named, correctly-typed fields every other route in
this package already provides.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

router = APIRouter(tags=["templates"])


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
