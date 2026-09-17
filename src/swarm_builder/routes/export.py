"""``GET /api/graphs/:id/export`` -- hand the generated project back to
the user.

Reports the project's path on disk plus the exact ``uv`` commands needed
to sync and validate it, so the project can be run outside Swarm Builder
without any further instructions.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from swarm_builder.config import get_uv_cache_dir, get_workspace_dir
from swarm_builder.store._ids import InvalidGraphIdError
from swarm_builder.store.projects import project_dir, project_exists

router = APIRouter(tags=["export"])


class _CamelModel(BaseModel):
    """Local camelCase-alias base for this module's response shapes.

    A local replica rather than an import of ``models.py``'s
    ``SwarmBaseModel``: that class also sets ``extra="forbid"`` for graph
    *documents*, which is not this response's contract.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ExportResponse(_CamelModel):
    """Where the compiled project lives and how to run it."""

    project_path: str
    run_command: str


@router.get("/graphs/{graph_id}/export", response_model=ExportResponse)
def export_graph(graph_id: str) -> ExportResponse:
    """404 (naming the expected, never-created path) when the graph has
    no compiled project yet. This route does not itself validate that a
    GRAPH document with this id exists -- only that a PROJECT directory
    does -- because ``project_exists``/``project_dir`` already validate
    ``graph_id`` as a path segment on their own (via
    ``store/_ids.py``); an invalid id therefore surfaces as a 422
    before any filesystem check runs, never as a false 404.
    """
    workspace_dir = get_workspace_dir()

    try:
        exists = project_exists(workspace_dir, graph_id)
    except InvalidGraphIdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    path = project_dir(workspace_dir, graph_id)

    if not exists:
        raise HTTPException(
            status_code=404,
            detail=f"no compiled project for graph {graph_id!r} yet; expected at {path}",
        )

    uv_cache_dir = get_uv_cache_dir()
    run_command = (
        f"cd {path} && "
        f"UV_CACHE_DIR={uv_cache_dir} uv sync && "
        f"UV_CACHE_DIR={uv_cache_dir} uv run python validate/dry_run.py"
    )
    return ExportResponse(project_path=str(path), run_command=run_command)
