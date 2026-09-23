"""``POST /api/graphs/generate``: describe a workflow, get a saved graph.

Runs :func:`swarm_builder.compile.generate.generate_graph` on the resolved
model route (same precedence as a compile: request override, then
``settings.yaml``, then ``SWARM_MODEL``), saves the review-clean result
through the graph store so it is an ordinary document from that moment,
and returns it with any review warnings.

Same lazy-import degradation seam as ``routes/compile.py``: the generator
imports ``pydantic_ai``, so a missing provider extra fails this endpoint
with 503 rather than server startup.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UserError

from swarm_builder import runtime
from swarm_builder.config import get_dsh_home, get_workspace_dir
from swarm_builder.inherit.routes import UnmappableRouteError, build_live_model
from swarm_builder.inherit.settings import resolve_effective_model
from swarm_builder.models import ModelSelection, SwarmGraph
from swarm_builder.routes.graphs import FindingOut
from swarm_builder.routes.health import credential_blocker
from swarm_builder.store.graphs import GraphStoreError, put_graph

router = APIRouter(tags=["generate"])

#: Longest description accepted. Long enough for a paragraph or three;
#: short enough that a pasted document is refused rather than sent to the
#: model wholesale.
MAX_DESCRIPTION_CHARS = 8000


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class GenerateGraphRequest(_CamelModel):
    """Body of ``POST /api/graphs/generate``."""

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    name: str | None = None
    graph_id: str | None = Field(
        default=None,
        description="Reuse an existing graph id (replace its document); default mints a new one.",
    )
    model_override: ModelSelection | None = None


class GenerateModelOut(_CamelModel):
    provider: str
    model: str
    source: str


class GenerateGraphResponse(_CamelModel):
    graph: SwarmGraph
    warnings: list[FindingOut]
    attempts: int
    model: GenerateModelOut
    #: True when the draft came from the deterministic stub rather than a
    #: model, so the UI can say so instead of presenting stub output as a
    #: model's work.
    dry_run: bool


@router.post(
    "/graphs/generate",
    response_model=GenerateGraphResponse,
    responses={
        422: {"description": "the model could not produce a review-clean graph"},
        502: {"description": "the model API refused or failed the request"},
        503: {"description": "no usable route or credential, or the generator is unavailable"},
    },
)
async def generate_graph_route(body: GenerateGraphRequest) -> GenerateGraphResponse:
    """Generate, save, and return a graph for ``body.description``.

    Raises:
        HTTPException: 422 when every attempt failed (the detail carries the
            last review findings as ``problems``); 503 when the resolved
            route is unmappable, no model is configured, or the generator
            cannot be imported; 500 when the graph cannot be saved.
    """
    try:
        # Lazy degradation seam -- see the module docstring.
        from swarm_builder.compile import generate as generate_module
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "generate is not available: swarm_builder.compile.generate "
                "could not be imported"
            ),
        ) from exc

    override = None
    if body.model_override is not None:
        override = (
            body.model_override.provider,
            body.model_override.model,
            body.model_override.reasoning_effort,
        )
    effective = resolve_effective_model(get_dsh_home(), override)

    # Dry run (the app's own switch, or one of the SWARM_FAKE_* environment
    # variables) replaces the model with a deterministic stub, so neither the
    # "nothing configured" gate nor the credential check applies: a brand-new
    # user who turned dry run on must be able to describe a workflow without
    # a provider at all.
    dry_run = runtime.dry_run_active()

    model = None
    if not dry_run:
        if effective.source == "bundle-default":
            raise HTTPException(
                status_code=503,
                detail=(
                    "No model is configured yet. Choose a provider and add your API "
                    "key in Model settings, or turn on Dry run mode to build and run "
                    "offline."
                ),
            )
        missing_credential = credential_blocker(effective)
        if missing_credential is not None:
            raise HTTPException(status_code=503, detail=missing_credential)
        try:
            model = build_live_model(effective).model
        except UnmappableRouteError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    graph_id = body.graph_id or str(uuid4())
    try:
        result = await generate_module.generate_graph(
            body.description, model=model, graph_id=graph_id
        )
    except generate_module.GenerateError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "the model could not produce a review-clean graph",
                "problems": exc.problems,
                "attempts": exc.attempts,
            },
        ) from exc
    except UserError as exc:
        # pydantic-ai's own configuration errors (a provider missing its API
        # key, an unknown model name): a setup problem, not a server bug.
        raise HTTPException(status_code=503, detail=f"model configuration error: {exc}") from exc
    except UnexpectedModelBehavior as exc:
        # The model never returned a reply that validates as a GraphDraft
        # within the retry budget: an upstream quality failure, reported as
        # such, with the reason the model was told on its last retry.
        raise HTTPException(
            status_code=502, detail=f"the model did not produce a valid draft: {exc}"
        ) from exc
    except ModelAPIError as exc:
        # The provider refused or failed the request (4xx/5xx from the model
        # API): report it verbatim as an upstream failure, never a traceback.
        raise HTTPException(status_code=502, detail=f"model API error: {exc}") from exc

    graph = result.graph
    if body.name:
        graph = graph.model_copy(update={"name": body.name.strip() or graph.name})
    if body.model_override is not None:
        graph = graph.model_copy(update={"model": body.model_override})

    try:
        put_graph(get_workspace_dir(), graph)
    except GraphStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return GenerateGraphResponse(
        graph=graph,
        warnings=[
            FindingOut(code=f.code, message=f.message, node_ids=list(f.node_ids))
            for f in result.warnings
        ],
        attempts=result.attempts,
        model=GenerateModelOut(
            provider=effective.provider, model=effective.model, source=effective.source
        ),
        dry_run=dry_run,
    )
