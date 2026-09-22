"""Graph CRUD plus the static-review route.

Serves ``GET /api/graphs``, ``GET /api/graphs/:id``, ``PUT
/api/graphs/:id``, ``DELETE /api/graphs/:id`` and ``POST
/api/graphs/:id/review``.

Every handler calls :func:`swarm_builder.config.get_workspace_dir`
fresh -- never cached, matching this package's blanket no-caching rule
(``routes/__init__.py``'s module docstring) -- and delegates all
filesystem and path-safety work to ``store/graphs.py`` /
``store/projects.py``, which this module only ever imports from.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from swarm_builder.config import get_workspace_dir
from swarm_builder.models import SwarmGraph
from swarm_builder.store._ids import InvalidGraphIdError
from swarm_builder.store.graphs import (
    GraphNotFoundError,
    GraphStoreError,
    delete_graph,
    get_graph,
    graph_path,
    list_graphs,
    put_graph,
)
from swarm_builder.store.projects import (
    ProjectStoreError,
    delete_langgraph_project_dir,
    delete_project_dir,
)

router = APIRouter(tags=["graphs"])


def _load_graph_or_http_error(workspace_dir: Path, graph_id: str) -> SwarmGraph:
    """Read one saved graph, mapping every store failure to an HTTP error.

    Shared by every route that reads a single graph. ``get_graph``'s own
    docstring states that a malformed JSON file or a file that fails
    :class:`SwarmGraph` validation propagates ``json.JSONDecodeError`` /
    ``pydantic.ValidationError`` *unwrapped*, deliberately leaving the
    HTTP mapping to this layer -- mirroring how ``list_graphs`` reports
    the same two failure shapes into its own ``errors`` array rather than
    failing the whole listing.

    Both are mapped to 422 rather than 500: a file this endpoint cannot
    read is either a client's own bad write or a document corrupted by an
    interrupted process, and either way it is a bad request against this
    id rather than a server malfunction. Mapping it here is what makes a
    single-graph read behave consistently with the plural listing route
    for the exact same on-disk failure.

    Args:
        workspace_dir: The workspace root.
        graph_id: The id of the graph to read.

    Returns:
        The validated document.

    Raises:
        fastapi.HTTPException: 404 when the graph is absent, 422 when it
            is unreadable or invalid, 500 on a store I/O failure.
    """
    try:
        return get_graph(workspace_dir, graph_id)
    except GraphNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"graph {graph_id!r} not found at {graph_path(workspace_dir, graph_id)}",
        ) from exc
    except InvalidGraphIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GraphStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"graph {graph_id!r} at {graph_path(workspace_dir, graph_id)} is not a "
                f"valid saved graph document: {exc}"
            ),
        ) from exc


class _CamelModel(BaseModel):
    """Local replica of ``models.py``'s two-line camelCase config -- see
    ``routes/health.py``'s identical class for the rationale. Used only
    for response shapes this module itself defines (graph summaries,
    review results); ``SwarmGraph`` itself is returned/accepted
    directly and already carries its own camelCase aliasing."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class GraphSummary(_CamelModel):
    """One row of the graph list: enough for a picker, none of the document."""

    id: str
    name: str
    updated_at: datetime
    node_count: int
    edge_count: int


class GraphListError(_CamelModel):
    """A graph file that exists on disk but could not be read or validated.

    Reported alongside the valid rows rather than replacing them, so one
    corrupt file never hides every other saved graph.
    """

    id: str
    detail: str


class GraphListResponse(_CamelModel):
    """The full ``GET /api/graphs`` response body.

    ``errors`` is non-empty only when individual files failed; the request
    itself still succeeded.
    """

    graphs: list[GraphSummary]
    errors: list[GraphListError]


class DeleteGraphResponse(_CamelModel):
    """The outcome of a delete, including whether a project was removed too."""

    deleted: bool
    project_deleted: bool


class FindingOut(_CamelModel):
    """One review finding, as the canvas Inspector displays it."""

    code: str
    message: str
    node_ids: list[str]


class ReviewResponse(_CamelModel):
    """The Phase-1 review outcome: a pass/fail flag plus every finding."""

    ok: bool
    errors: list[FindingOut]
    warnings: list[FindingOut]


@router.get("/graphs", response_model=GraphListResponse)
def list_graphs_route() -> GraphListResponse:
    """List every saved graph as a summary, plus one entry per file that
    failed to parse/validate -- a single bad file never 500s the whole
    listing (``store.graphs.list_graphs``'s own contract)."""
    workspace_dir = get_workspace_dir()
    graphs, errors = list_graphs(workspace_dir)

    summaries = [
        GraphSummary(
            id=g.id,
            name=g.name,
            updated_at=g.updated_at,
            node_count=len(g.nodes),
            edge_count=len(g.edges),
        )
        for g in graphs
    ]
    error_entries = [GraphListError(id=bad_id, detail=detail) for bad_id, detail in errors]
    return GraphListResponse(graphs=summaries, errors=error_entries)


@router.get("/graphs/{graph_id}", response_model=SwarmGraph)
def get_graph_route(graph_id: str) -> SwarmGraph:
    """Return the full graph document.

    404 (naming the resolved path that was checked) when no such graph
    exists; 422 for a malformed/path-unsafe id, or for a graph file that
    is not valid JSON or fails :class:`SwarmGraph` validation (a client's
    own bad write, or a file corrupted by an interrupted process -- never
    an uncaught exception reaching the ASGI stack); 500 for any other
    filesystem failure while reading.
    """
    workspace_dir = get_workspace_dir()
    return _load_graph_or_http_error(workspace_dir, graph_id)


@router.put("/graphs/{graph_id}", response_model=SwarmGraph)
def put_graph_route(graph_id: str, graph: SwarmGraph) -> SwarmGraph:
    """Validate + atomically write a graph document.

    The path id is authoritative: a body ``id`` that disagrees with the
    path parameter is rejected with 422 naming both, rather than
    silently overwritten or silently accepted. ``updated_at`` is always
    server-stamped to ``datetime.now(UTC)`` on every successful write,
    overwriting whatever placeholder the client sent -- the response
    body reflects the stamp that was actually persisted.

    Body validation against :class:`SwarmGraph` (including its
    ``_check_structural_integrity`` model validator) happens
    automatically via the ``graph: SwarmGraph`` parameter annotation
    before this function body even runs; a validation failure surfaces
    as FastAPI's normal 422 with no extra handling needed here.
    """
    if graph_id != graph.id:
        raise HTTPException(
            status_code=422,
            detail=f"path id {graph_id!r} does not match body id {graph.id!r}",
        )

    stamped = graph.model_copy(update={"updated_at": datetime.now(UTC)})

    workspace_dir = get_workspace_dir()
    try:
        put_graph(workspace_dir, stamped)
    except InvalidGraphIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GraphStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return stamped


@router.delete("/graphs/{graph_id}", response_model=DeleteGraphResponse)
def delete_graph_route(
    graph_id: str,
    project: bool = Query(default=False),
) -> DeleteGraphResponse:
    """Remove the graph document; also remove its generated project
    directory when ``?project=true`` is given (idempotent -- a project
    that was never compiled is not an error).

    404 when the graph itself does not exist (the ``?project`` flag
    never changes that: a missing graph is always a 404 regardless of
    whether a project directory happens to exist for that id).
    """
    workspace_dir = get_workspace_dir()
    try:
        delete_graph(workspace_dir, graph_id)
    except GraphNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"graph {graph_id!r} not found at {graph_path(workspace_dir, graph_id)}",
        ) from exc
    except InvalidGraphIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GraphStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if project:
        try:
            delete_project_dir(workspace_dir, graph_id)
            delete_langgraph_project_dir(workspace_dir, graph_id)
        except InvalidGraphIdError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ProjectStoreError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return DeleteGraphResponse(deleted=True, project_deleted=project)


@router.post("/graphs/{graph_id}/review", response_model=ReviewResponse)
def review_graph_route(graph_id: str) -> ReviewResponse:
    """Run the Phase-1 static review over a saved graph.

    This is the "initial review" a user triggers before compiling: cycles,
    unreachable nodes, state ownership, port-type mismatches, branch
    consistency, and the rest of the structural rules.

    **Lazy import, deliberately -- this is a degradation seam.** The whole
    ``swarm_builder.compile`` subsystem exists only to serve the compile
    and review endpoints, and it is much heavier than the rest of the
    server (it pulls in the codegen, agent and pipeline modules). Importing
    it at module level would make a broken compile subsystem take the
    entire server down at startup; importing it here means that subsystem
    fails *this endpoint* alone, with a clear 503, while graph CRUD,
    templates, models and health keep working.
    """
    workspace_dir = get_workspace_dir()
    graph = _load_graph_or_http_error(workspace_dir, graph_id)

    try:
        from swarm_builder.compile.review import review
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="review is not available: swarm_builder.compile.review could not be imported",
        ) from exc

    result = review(graph)
    return ReviewResponse(
        ok=result.ok,
        errors=[
            FindingOut(code=f.code, message=f.message, node_ids=list(f.node_ids))
            for f in result.errors
        ],
        warnings=[
            FindingOut(code=f.code, message=f.message, node_ids=list(f.node_ids))
            for f in result.warnings
        ],
    )
