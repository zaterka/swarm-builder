"""``POST /api/graphs/generate`` and ``POST /api/graphs/generate/clarify``.

Two endpoints, one flow. A client that wants a graph asks ``/clarify`` first:
either the request is already concrete and it gets the assumptions the draft will
make, or it gets up to four questions with options. The answers then ride along
with the description (and any attachments) into ``/generate``.

Both endpoints resolve the model route the same way -- request override, then
``settings.yaml``, then ``SWARM_MODEL`` -- and both are subject to the same
attachment rules: ids are loaded from the workspace store (expired ids are a 422,
not a 500), the per-request count and total-size caps are enforced, and an image
is refused unless the resolved model can read one.

Same lazy-import degradation seam as ``routes/compile.py``: the generator imports
``pydantic_ai``, so a missing provider extra fails these endpoints with 503
rather than at server startup.

Both endpoints pass an explicit output-token budget for the model families whose
default is too small for a large structured answer (see
:func:`swarm_builder.known_models.default_max_output_tokens`); every other route
keeps its provider default untouched.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UserError

from swarm_builder import runtime
from swarm_builder.attachments.limits import MAX_ANSWER_CHARS, MAX_ANSWERS
from swarm_builder.attachments.models import (
    AttachmentError,
    AttachmentKind,
    AttachmentNotFoundError,
    AttachmentReadError,
    AttachmentStoreError,
    AttachmentTooLargeError,
    LoadedAttachment,
    UnsupportedAttachmentError,
)
from swarm_builder.compile.clarify import ClarifyAnswerIn, ClarifyResult
from swarm_builder.config import get_dsh_home, get_workspace_dir
from swarm_builder.inherit.routes import UnmappableRouteError, build_live_model
from swarm_builder.inherit.settings import EffectiveModel, resolve_effective_model
from swarm_builder.known_models import resolve_max_output_tokens
from swarm_builder.models import ModelSelection, SwarmGraph
from swarm_builder.routes.attachments import CODE_IMAGE_UNSUPPORTED
from swarm_builder.routes.graphs import FindingOut
from swarm_builder.routes.health import credential_blocker
from swarm_builder.routes.problems import problem
from swarm_builder.store.graphs import GraphStoreError, put_graph
from swarm_builder.vision import supports_image_input

router = APIRouter(tags=["generate"])

#: Longest description accepted. Long enough for a paragraph or three;
#: short enough that a pasted document is refused rather than sent to the
#: model wholesale.
MAX_DESCRIPTION_CHARS = 8000

#: Error code used when the attachments themselves could not be loaded.
CODE_ATTACHMENT_EXPIRED = "attachment_expired"


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ClarifyAnswerInRaw(_CamelModel):
    """One answer from the panel."""

    question_id: str = Field(max_length=64)
    question: str | None = Field(default=None, max_length=600)
    answer: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)


class GenerateGraphRequest(_CamelModel):
    """Body of ``POST /api/graphs/generate``."""

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    name: str | None = None
    graph_id: str | None = Field(
        default=None,
        description="Reuse an existing graph id (replace its document); default mints a new one.",
    )
    model_override: ModelSelection | None = None
    attachment_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Ids returned by POST /api/graphs/attachments. The count and total-size "
            "caps are enforced by the store, which reports them as a 413 with code "
            "attachment_too_large -- not as a schema-validation error."
        ),
    )
    answers: list[ClarifyAnswerInRaw] = Field(
        default_factory=list,
        max_length=MAX_ANSWERS,
        description="Answers to the clarify pass' questions; authoritative for the draft.",
    )


class ClarifyRequest(_CamelModel):
    """Body of ``POST /api/graphs/generate/clarify``.

    No ``modelOverride``: the analysis pass uses the same resolved route as the
    draft, so model selection happens in exactly one place for this flow.
    """

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    attachment_ids: list[str] = Field(default_factory=list)


class ClarifyOptionOut(_CamelModel):
    label: str
    description: str | None = None


class ClarifyQuestionOut(_CamelModel):
    id: str
    question: str
    why: str
    options: list[ClarifyOptionOut]
    recommended: str


class GenerateModelOut(_CamelModel):
    provider: str
    model: str
    source: str


class ClarifyResponse(_CamelModel):
    needs_clarification: bool
    questions: list[ClarifyQuestionOut]
    assumptions: list[str]
    understanding: str
    model: GenerateModelOut
    dry_run: bool


class AttachmentSummaryOut(_CamelModel):
    """One attachment as it was used by a generation."""

    id: str
    filename: str
    kind: AttachmentKind
    chars: int
    truncated: bool
    notes: list[str]


class GenerateGraphResponse(_CamelModel):
    graph: SwarmGraph
    warnings: list[FindingOut]
    attempts: int
    model: GenerateModelOut
    #: True when the draft came from the deterministic stub rather than a
    #: model, so the UI can say so instead of presenting stub output as a
    #: model's work.
    dry_run: bool
    #: What the draft was given, echoed so the panel can report it.
    attachments: list[AttachmentSummaryOut] = Field(default_factory=list)


def _normalize_answers(raw: list[ClarifyAnswerInRaw]) -> list[ClarifyAnswerIn]:
    """Trim, dedupe (last answer per question wins) and order the answers.

    Validation has already bounded the count and each length; this is about
    semantics: a panel that re-asks a question (the user went back and changed
    their mind) must send one answer per question, and a whitespace-only answer
    is not an answer at all.

    Args:
        raw: The answers as submitted.

    Returns:
        Answers in submission order, one per question id, blanks dropped.
    """
    by_question: dict[str, ClarifyAnswerIn] = {}
    for item in raw:
        answer = item.answer.strip()
        if not answer:
            continue
        question = (item.question or "").strip() or None
        by_question[item.question_id] = ClarifyAnswerIn(
            question_id=item.question_id, question=question, answer=answer
        )
    return list(by_question.values())


def _load_request_attachments(ids: list[str]) -> list[LoadedAttachment]:
    """Load and validate the attachments a request names.

    Raises:
        HTTPException: 422 for an unknown/expired id or an unreadable record, 413
            when the count or total-size cap is exceeded, 500 on a store failure.
    """
    if not ids:
        return []
    from swarm_builder.attachments import store as store_module

    try:
        return store_module.load_attachments(get_workspace_dir(), ids)
    except AttachmentNotFoundError as exc:
        raise problem(422, CODE_ATTACHMENT_EXPIRED, str(exc), [str(exc)]) from exc
    except AttachmentTooLargeError as exc:
        raise problem(413, "attachment_too_large", str(exc), [str(exc)]) from exc
    except AttachmentReadError as exc:
        raise problem(422, "attachment_unreadable", str(exc), [str(exc)]) from exc
    except UnsupportedAttachmentError as exc:
        raise problem(415, "unsupported_attachment_type", str(exc), [str(exc)]) from exc
    except AttachmentStoreError as exc:
        raise problem(
            500, "attachment_store_failed", exc.public_message, [exc.public_message]
        ) from exc
    except AttachmentError as exc:
        raise problem(422, "attachment_unreadable", str(exc), [str(exc)]) from exc


def _refuse_unreadable_images(
    attachments: list[LoadedAttachment], effective: EffectiveModel
) -> None:
    """Refuse an image when the resolved model cannot read it.

    Re-checked on every request, not only at upload: the model can change between
    the two, and the failure a user would otherwise see is a raw provider error
    halfway through a generation.

    Raises:
        HTTPException: 422 with ``image_input_unsupported``.
    """
    if not any(item.record.kind == "image" for item in attachments):
        return
    support = supports_image_input(effective.provider, effective.model)
    if not support.supported:
        raise problem(422, CODE_IMAGE_UNSUPPORTED, support.reason, [support.reason])


def _attachment_summaries(attachments: list[LoadedAttachment]) -> list[AttachmentSummaryOut]:
    """Project loaded attachments onto the response summary shape."""
    return [
        AttachmentSummaryOut(
            id=item.record.id,
            filename=item.record.filename,
            kind=item.record.kind,
            chars=item.record.chars,
            truncated=item.record.truncated,
            notes=list(item.record.notes),
        )
        for item in attachments
    ]


def _resolved_model(body_override: ModelSelection | None) -> EffectiveModel:
    """Resolve the effective route for a request."""
    override = None
    if body_override is not None:
        override = (
            body_override.provider,
            body_override.model,
            body_override.reasoning_effort,
        )
    return resolve_effective_model(get_dsh_home(), override)


def _require_live_model(effective: EffectiveModel):
    """Build the live model, or fail with the setup problem the user must fix.

    Raises:
        HTTPException: 503 when nothing is configured, the credential is missing,
            or the route cannot be mapped onto a pydantic-ai model.
    """
    if effective.source == "bundle-default":
        raise HTTPException(
            status_code=503,
            detail=(
                "No model is configured yet. Choose a provider and add your API key in Model "
                "settings, or turn on Dry run mode to build and run offline."
            ),
        )
    missing_credential = credential_blocker(effective)
    if missing_credential is not None:
        raise HTTPException(status_code=503, detail=missing_credential)
    try:
        return build_live_model(effective).model
    except UnmappableRouteError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _clarify_out(result: ClarifyResult) -> list[ClarifyQuestionOut]:
    """Project sanitized questions onto the wire shape."""
    return [
        ClarifyQuestionOut(
            id=question.id,
            question=question.question,
            why=question.why,
            options=[
                ClarifyOptionOut(label=option.label, description=option.description)
                for option in question.options
            ],
            recommended=question.recommended,
        )
        for question in result.questions
    ]


def _map_generator_failure(exc: Exception) -> HTTPException:
    """Map a ``GenerateError`` to the structured 422 the panel already parses."""
    attempts = getattr(exc, "attempts", None)
    return HTTPException(
        status_code=422,
        detail={
            "code": "generation_failed",
            "message": "the model could not produce a review-clean graph",
            "problems": list(getattr(exc, "problems", []) or []),
            "attempts": attempts,
        },
    )


@router.post(
    "/graphs/generate/clarify",
    response_model=ClarifyResponse,
    responses={
        422: {"description": "the description or an attachment is unusable"},
        502: {"description": "the model API refused or failed the request, or answered unusably"},
        503: {"description": "no usable route or credential, or the generator is unavailable"},
    },
)
async def clarify_graph_route(body: ClarifyRequest) -> ClarifyResponse:
    """Decide whether ``body.description`` needs questions before drafting.

    Raises:
        HTTPException: 422 when an attachment id is unknown or expired; 502/503
            for model setup and upstream failures, mirroring ``/generate``.
    """
    try:
        from swarm_builder.compile import clarify as clarify_module
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "the clarify pass is not available: swarm_builder.compile.clarify could "
                "not be imported"
            ),
        ) from exc

    effective = _resolved_model(None)
    dry_run = runtime.dry_run_active()
    attachments = _load_request_attachments(body.attachment_ids)

    model = None
    if not dry_run:
        model = _require_live_model(effective)
        _refuse_unreadable_images(attachments, effective)

    try:
        result = await clarify_module.analyze(
            body.description,
            model=model,
            attachments=attachments,
            max_output_tokens=resolve_max_output_tokens(
                effective.model, effective.max_tokens
            ),
        )
    except UserError as exc:
        # Plain string details, matching /generate: the panel branches on the
        # status code (422 stops, anything else falls through to drafting), so a
        # second vocabulary for the same upstream failures would be noise.
        raise HTTPException(status_code=503, detail=f"model configuration error: {exc}") from exc
    except UnexpectedModelBehavior as exc:
        raise HTTPException(
            status_code=502, detail=f"the model did not produce a usable analysis: {exc}"
        ) from exc
    except ModelAPIError as exc:
        raise HTTPException(status_code=502, detail=f"model API error: {exc}") from exc

    return ClarifyResponse(
        needs_clarification=result.needs_clarification,
        questions=_clarify_out(result),
        assumptions=list(result.assumptions),
        understanding=result.understanding,
        model=GenerateModelOut(
            provider=effective.provider, model=effective.model, source=effective.source
        ),
        dry_run=dry_run,
    )


@router.post(
    "/graphs/generate",
    response_model=GenerateGraphResponse,
    responses={
        413: {"description": "the attachments exceed the per-request size cap"},
        422: {"description": "the model could not produce a review-clean graph"},
        502: {"description": "the model API refused or failed the request"},
        503: {"description": "no usable route or credential, or the generator is unavailable"},
    },
)
async def generate_graph_route(body: GenerateGraphRequest) -> GenerateGraphResponse:
    """Generate, save, and return a graph for ``body.description``.

    Raises:
        HTTPException: 422 when every attempt failed (the detail carries the
            last review findings as ``problems``) or an attachment expired; 413
            when the attachments exceed the size cap; 503 when the resolved
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

    effective = _resolved_model(body.model_override)

    # Dry run (the app's own switch, or one of the SWARM_FAKE_* environment
    # variables) replaces the model with a deterministic stub, so neither the
    # "nothing configured" gate nor the credential check applies: a brand-new
    # user who turned dry run on must be able to describe a workflow without
    # a provider at all.
    dry_run = runtime.dry_run_active()

    model = None
    if not dry_run:
        model = _require_live_model(effective)

    attachments = _load_request_attachments(body.attachment_ids)
    if not dry_run:
        _refuse_unreadable_images(attachments, effective)

    graph_id = body.graph_id or str(uuid4())
    try:
        result = await generate_module.generate_graph(
            body.description,
            model=model,
            graph_id=graph_id,
            attachments=attachments,
            answers=_normalize_answers(body.answers),
            # A thinking model can spend a small provider default on thinking and
            # return no text at all; the rule (and the measurement behind it) is
            # in known_models.default_max_output_tokens, with the user's own
            # Max output tokens setting winning via resolve_max_output_tokens.
            max_output_tokens=resolve_max_output_tokens(
                effective.model, effective.max_tokens
            ),
        )
    except generate_module.GenerateError as exc:
        raise _map_generator_failure(exc) from exc
    except UserError as exc:
        # pydantic-ai's own configuration errors (a provider missing its API
        # key, an unknown model name): a setup problem, not a server bug.
        # A plain string detail, like every other pre-existing failure here --
        # the structured shape below is for attachment failures, which the panel
        # has to tell apart.
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
        attachments=_attachment_summaries(attachments),
    )


__all__ = [
    "MAX_DESCRIPTION_CHARS",
    "AttachmentSummaryOut",
    "ClarifyAnswerInRaw",
    "ClarifyOptionOut",
    "ClarifyQuestionOut",
    "ClarifyRequest",
    "ClarifyResponse",
    "GenerateGraphRequest",
    "GenerateGraphResponse",
    "GenerateModelOut",
    "router",
]
