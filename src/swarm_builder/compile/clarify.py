"""Ask the user the few questions that actually change the graph.

A description like *"handle our invoices"* is not under-specified in a way a
reviewer can catch: the resulting graph is *valid*, it is just not the workflow
the user has in mind. One cheap model call before drafting closes that gap. It
either says "nothing here changes the shape, here is what I will assume", or it
returns at most :data:`~swarm_builder.attachments.limits.MAX_CLARIFY_QUESTIONS`
questions, each with concrete options and a recommended default, which the panel
renders as radios with an "Other…" escape.

Three design decisions worth knowing:

* **One call, no repair loop.** A second clarifying call would cost more than it
  saves: the user is right there with the answer. The draft itself still runs the
  repair loop in :mod:`swarm_builder.compile.generate`.
* **The draft model is permissive; this module does the bounding.** Everything
  the model returns goes through :func:`sanitize_analysis`, which truncates,
  drops unusable questions, coerces a "recommended" answer that is not one of the
  options, and assigns question ids. Constraints on the *output schema* would
  instead make a sloppy answer a hard failure (``UnexpectedModelBehavior`` -> 502)
  before any of this code could repair it -- and the questions would become
  untestable, because a half-valid answer could never reach the sanitizer at all.
* **The dependency runs one way.** This module owns
  :class:`ClarifyAnswerIn`; ``compile/generate.py`` imports it. The fake-switch
  predicates live in :mod:`swarm_builder.runtime`, which both import.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_ai import Agent, ModelSettings, PromptedOutput, UsageLimits
from pydantic_ai.models import Model

from swarm_builder import runtime
from swarm_builder.attachments.context import render_attachments_context
from swarm_builder.attachments.limits import MAX_CLARIFY_QUESTIONS
from swarm_builder.attachments.models import LoadedAttachment

#: Output-schema validation retries per request, matching the drafter's value:
#: the same class of failure (a model that returns prose instead of the shape).
AGENT_RETRIES = 3

#: Model requests one analysis may spend. One call plus pydantic-ai's own
#: validation retries; the analysis has no repair loop of its own.
REQUEST_LIMIT = 4

#: Longest question, option label, or assumption kept.
MAX_TEXT_CHARS = 300

#: Most assumptions carried into the panel.
MAX_ASSUMPTIONS = 6

#: Options kept per question. The draft schema allows any number (a constraint
#: there would turn a sloppy answer into a 502 before this module could bound it).
MAX_OPTIONS_PER_QUESTION = 4

#: Shortest description (in words) that is trusted without a question under dry
#: run; below it the deterministic stub asks its canned question so the flow is
#: exercisable with no credentials.
FAKE_QUESTION_WORD_THRESHOLD = 40


class _DraftModel(BaseModel):
    """camelCase aliases like the rest of the app; extras forbidden so a stray
    key is a validation retry rather than a silently ignored field."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ClarifyOptionDraft(_DraftModel):
    """One suggested answer. ``description`` is an optional clarifying note."""

    label: str = ""
    description: str | None = None


class ClarifyQuestionDraft(_DraftModel):
    """One question, as the model wrote it (nothing here is trusted)."""

    question: str = ""
    why: str = ""
    options: list[ClarifyOptionDraft] = Field(default_factory=list)
    recommended: str = ""


class ClarifyAnalysis(_DraftModel):
    """The model's whole answer to the analysis pass."""

    needs_clarification: bool = False
    questions: list[ClarifyQuestionDraft] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    understanding: str = ""


@dataclass(frozen=True)
class ClarifyQuestion:
    """A sanitized question: ids assigned here, options bounded."""

    id: str
    question: str
    why: str
    options: list[ClarifyOptionDraft]
    recommended: str


@dataclass(frozen=True)
class ClarifyAnswerIn:
    """One answer travelling from the panel into the drafter.

    ``question`` is optional because the answer is what the model must honour;
    the question text is included when the client has it, so the prompt block
    reads as a dialogue rather than a list of fragments.
    """

    question_id: str
    question: str | None
    answer: str


@dataclass(frozen=True)
class ClarifyResult:
    """What the analysis pass decided."""

    needs_clarification: bool
    questions: list[ClarifyQuestion]
    assumptions: list[str]
    understanding: str


CLARIFY_INSTRUCTIONS = """\
You prepare to design an agent workflow for Swarm Builder. You will be given a
user's description of a business workflow and, sometimes, excerpts of files they
attached as context. Decide whether you can already design THAT workflow, or
whether a few answers from the user would change its shape.

Ask a question ONLY when the answer changes the graph — for example:
- what the workflow's input actually is, or where it comes from;
- who or what the data is about (a customer, an invoice, a ticket, a document);
- the criteria a decision routes on, or the threshold it compares against;
- how many outcomes a routing step has;
- where the result goes (an answer to the user, a file, a record, an email);
- which step is expected to need a model and which is plain code.

Never ask about anything the description or the attached files already state.
Never ask a question whose answer would leave the graph identical either way.
Do not ask about wording, naming, or style: that is for the canvas afterwards.

Return at most 4 questions. Each question needs 2-4 concrete, mutually exclusive
options that a user can pick without thinking, exactly one of which you mark as
`recommended` (repeat its label exactly). Give each option a short `label` and,
when it helps, a one-sentence `description`. `why` is one short sentence telling
the user why the graph depends on the answer.

If the description is concrete enough to design the workflow, set
`needsClarification` to false, leave `questions` empty, and put what you are
assuming into `assumptions` (at most 6 short sentences). `understanding` is
always one short paragraph restating the workflow you are about to design.
"""


def _clean(text: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    """Collapse whitespace and cap one piece of model-authored text."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) > limit:
        return collapsed[:limit].rstrip() + "…"
    return collapsed


def sanitize_analysis(
    analysis: ClarifyAnalysis, *, max_questions: int = MAX_CLARIFY_QUESTIONS
) -> ClarifyResult:
    """Turn whatever the model returned into a bounded, usable result.

    The deterministic half of this module: everything a caller relies on is
    enforced here rather than in the output schema, so an almost-right answer
    still gives the user a useful question instead of a 502.

    Rules: keep at most ``max_questions``; drop questions with no text or fewer
    than two named options; coerce a ``recommended`` label that is not among the
    surviving options to the first one; assign ``q1``, ``q2``, … in order; cap
    assumptions; force ``needs_clarification`` to agree with the question list.

    Args:
        analysis: The model's answer, as validated (permissively).
        max_questions: How many questions to keep.

    Returns:
        The sanitized result, with ids assigned.
    """
    questions: list[ClarifyQuestion] = []
    for draft in analysis.questions:
        if len(questions) >= max_questions:
            break
        question = _clean(draft.question)
        if not question:
            continue
        options: list[ClarifyOptionDraft] = []
        seen: set[str] = set()
        for option in draft.options:
            if len(options) >= MAX_OPTIONS_PER_QUESTION:
                # The output schema is permissive on purpose, so the "2-4 options"
                # rule is enforced here: a model that returns nine has to be cut
                # back, not rejected (and not passed through to the panel).
                break
            label = _clean(option.label)
            if not label or label.lower() in seen:
                continue
            seen.add(label.lower())
            options.append(
                ClarifyOptionDraft(
                    label=label, description=_clean(option.description or "") or None
                )
            )
        if len(options) < 2:
            # A one-option question is not a question; the description plus the
            # assumption list serves the user better than a fake choice.
            continue
        recommended = _clean(draft.recommended)
        if recommended not in {option.label for option in options}:
            recommended = options[0].label
        questions.append(
            ClarifyQuestion(
                id=f"q{len(questions) + 1}",
                question=question,
                why=_clean(draft.why),
                options=options,
                recommended=recommended,
            )
        )
    assumptions: list[str] = []
    for raw in analysis.assumptions:
        cleaned = _clean(raw)
        if cleaned and cleaned not in assumptions:
            assumptions.append(cleaned)
        if len(assumptions) >= MAX_ASSUMPTIONS:
            break
    return ClarifyResult(
        needs_clarification=bool(questions),
        questions=questions,
        assumptions=assumptions,
        understanding=_clean(analysis.understanding, limit=600),
    )


def build_clarify_agent(
    model: Model | str, *, max_output_tokens: int | None = None
) -> Agent[None, ClarifyAnalysis]:
    """The structured-output analysis agent, built per call (never at import).

    ``PromptedOutput`` for the same reason the drafter uses it: the default
    tool-based output forces ``tool_choice`` onto an output tool, which reasoning
    models reject outright. ``max_output_tokens`` is passed for the same reason
    the drafter passes it -- a thinking model can spend a small default on
    thinking and return nothing.
    """
    return Agent(
        model,
        output_type=PromptedOutput(ClarifyAnalysis, name="ClarifyAnalysis"),
        instructions=CLARIFY_INSTRUCTIONS,
        retries=AGENT_RETRIES,
        defer_model_check=True,
        model_settings=ModelSettings(max_tokens=max_output_tokens)
        if max_output_tokens
        else None,
    )


def _build_prompt(description: str, attachments: Sequence[LoadedAttachment]) -> str | list[str]:
    """The analysis prompt: the description, then any attachment context.

    Text extracts only. Images deliberately stay out of the analysis pass: the
    questions are about the workflow's *shape*, and sending image bytes twice
    (analysis and draft) would double their cost for no extra question quality.
    The drafter is where an image's content matters. Recorded here so the choice
    is visible to whoever wonders why a screenshot-only request still gets
    questions about inputs and outputs.
    """
    context = render_attachments_context(attachments)
    if not context:
        return description
    return [description, context]


def fake_analysis(description: str) -> ClarifyAnalysis:
    """A deterministic analysis for dry run / ``SWARM_FAKE_GENERATE=1``.

    A short description gets one canned question, so the question flow can be
    exercised with no credentials at all; anything longer is treated as clear,
    which keeps the stub's behaviour predictable for tests and demos.
    """
    if len(description.split()) < FAKE_QUESTION_WORD_THRESHOLD:
        return ClarifyAnalysis(
            needs_clarification=True,
            understanding="Handle one incoming item and produce a result.",
            questions=[
                ClarifyQuestionDraft(
                    question="Who or what should the first step read?",
                    why="It decides the workflow's input and every step after it.",
                    options=[
                        ClarifyOptionDraft(label="A single piece of text"),
                        ClarifyOptionDraft(label="A file or document"),
                        ClarifyOptionDraft(label="A record from another system"),
                    ],
                    recommended="A single piece of text",
                )
            ],
            assumptions=["Dry run is on, so this question comes from the built-in stub."],
        )
    return ClarifyAnalysis(
        needs_clarification=False,
        understanding="Handle the described steps in order.",
        assumptions=["Dry run is on: the graph will come from the built-in stub."],
    )


def fake_clarify_enabled() -> bool:
    """Whether the deterministic analysis is used (delegates to the shared switch)."""
    return runtime.fake_draft_enabled()


async def analyze(
    description: str,
    *,
    model: Model | str | None,
    attachments: Sequence[LoadedAttachment] = (),
    max_questions: int = MAX_CLARIFY_QUESTIONS,
    max_output_tokens: int | None = None,
) -> ClarifyResult:
    """Decide whether ``description`` needs questions before it is drafted.

    Args:
        description: The user's prose.
        model: What ``Agent(...)`` runs on, or ``None`` under dry run / the fake
            switch.
        attachments: Attachments named by the request; their text is added to
            the prompt as context, never as instructions.
        max_questions: Cap on returned questions.
        max_output_tokens: An explicit output budget, or ``None`` for the
            provider's default.

    Returns:
        The sanitized analysis.

    Raises:
        ValueError: ``model`` is ``None`` and the fake switch is off.
        pydantic_ai.exceptions.UsageLimitExceeded: The request budget ran out.
    """
    if fake_clarify_enabled():
        return sanitize_analysis(fake_analysis(description), max_questions=max_questions)
    if model is None:
        raise ValueError("analyze needs a model unless SWARM_FAKE_GENERATE=1 is set")
    agent = build_clarify_agent(model, max_output_tokens=max_output_tokens)
    run = await agent.run(
        _build_prompt(description, attachments),
        usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
    )
    return sanitize_analysis(run.output, max_questions=max_questions)


__all__ = [
    "AGENT_RETRIES",
    "CLARIFY_INSTRUCTIONS",
    "FAKE_QUESTION_WORD_THRESHOLD",
    "MAX_ASSUMPTIONS",
    "MAX_TEXT_CHARS",
    "REQUEST_LIMIT",
    "ClarifyAnalysis",
    "ClarifyAnswerIn",
    "ClarifyOptionDraft",
    "ClarifyQuestion",
    "ClarifyQuestionDraft",
    "ClarifyResult",
    "analyze",
    "build_clarify_agent",
    "fake_analysis",
    "fake_clarify_enabled",
    "sanitize_analysis",
]
