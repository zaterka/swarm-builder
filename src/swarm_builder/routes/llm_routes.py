"""``GET /api/models`` -- the model picker's data source.

Returns every configured harness route, the resolved default, and each
route's PydanticAI emission classification, so the picker can tell the
user which routes are usable and why the others are not.

**Never cached** -- see ``routes/__init__.py``'s module docstring.
``read_settings``/``resolve_effective_model`` are called fresh inside
:func:`get_models` on every request.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from swarm_builder import runtime
from swarm_builder.config import get_dsh_home
from swarm_builder.inherit.routes import classify_route
from swarm_builder.inherit.settings import (
    RouteConfig,
    read_settings,
    resolve_effective_model,
    route_for_app_model,
)

router = APIRouter(tags=["models"])


class _CamelModel(BaseModel):
    """Local replica of ``models.py``'s two-line camelCase config -- see
    ``routes/health.py``'s identical class for why this is not imported
    from ``models.py`` instead."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ModelInfoOut(_CamelModel):
    """One entry of a route's explicit model list, as the picker sees it."""

    id: str
    name: str | None


class RouteOut(_CamelModel):
    """One configured route, with the picker-relevant classification fields.

    ``emission``/``required_extra``/``unmappable_reason`` are the outcome
    of :func:`swarm_builder.inherit.routes.classify_route`, exposed so the
    picker can disable or explain an unusable route before a compile is
    attempted.
    """

    key: str
    api: str | None
    has_explicit_models: bool
    emission: str
    required_extra: str | None
    unmappable_reason: str | None
    models: list[ModelInfoOut]


class ResolvedDefaultOut(_CamelModel):
    """The model a compile would use right now, plus where that came from."""

    provider: str
    model: str
    source: str


class ModelsResponse(_CamelModel):
    """The full ``GET /api/models`` response body.

    ``app_route`` is the route configured in Swarm Builder's *own* model
    settings, classified exactly like an inherited one, so a picker can list
    it first with the same enable/disable treatment. It is reported
    separately from ``routes`` rather than merged into it because the two
    have different lifetimes: a harness route exists because someone edited a
    machine-wide file, while this one exists because the user configured it
    in this application.
    """

    routes: list[RouteOut]
    app_route: RouteOut | None
    resolved_default: ResolvedDefaultOut
    settings_error: str | None


def _route_out(route: RouteConfig) -> RouteOut:
    """Render one route, classified for a picker."""
    emission = classify_route(route)
    return RouteOut(
        key=route.key,
        api=route.api,
        has_explicit_models=len(route.models) > 0,
        emission=emission.emission,
        required_extra=emission.required_extra,
        unmappable_reason=emission.unmappable_reason,
        models=[ModelInfoOut(id=m.id, name=m.name) for m in route.models],
    )


@router.get("/models", response_model=ModelsResponse)
def get_models() -> ModelsResponse:
    """List every configured harness route plus the resolved default,
    for a model picker UI.

    A route is only ever read from a :class:`Settings` whose ``error``
    is ``None`` (a settings file that failed to parse contributes zero
    routes, matching :func:`resolve_effective_model`'s own fallthrough
    behavior) -- but ``settings_error`` itself is still reported
    verbatim so the picker can explain why the route list is empty.
    """
    dsh_home = get_dsh_home()
    settings = read_settings(dsh_home)

    routes_out: list[RouteOut] = []
    if settings is not None and settings.error is None:
        routes_out = [_route_out(route) for route in settings.routes]

    app_model = runtime.current_model_config()
    app_route = _route_out(route_for_app_model(app_model)) if app_model is not None else None

    effective = resolve_effective_model(dsh_home)
    resolved_default = ResolvedDefaultOut(
        provider=effective.provider, model=effective.model, source=effective.source
    )

    settings_error = settings.error if settings is not None else None

    return ModelsResponse(
        routes=routes_out,
        app_route=app_route,
        resolved_default=resolved_default,
        settings_error=settings_error,
    )
