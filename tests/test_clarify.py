"""``swarm_builder.compile.clarify``: the analysis pass, and its bounding.

The interesting half of this module is :func:`sanitize_analysis`, because that is
where a permissive model answer becomes something the panel can render. The
fixtures here are hand-built drafts (not model calls), which is exactly the point
of keeping the output schema permissive: an almost-right answer must reach the
sanitizer instead of failing a generation with a 502.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_builder.attachments import store as store_module
from swarm_builder.attachments.models import Extraction
from swarm_builder.compile import clarify as clarify_module
from swarm_builder.compile.clarify import (
    ClarifyAnalysis,
    ClarifyAnswerIn,
    ClarifyOptionDraft,
    ClarifyQuestionDraft,
    analyze,
    fake_analysis,
    fake_clarify_enabled,
    sanitize_analysis,
)
from swarm_builder.main import create_app

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _draft(*questions: ClarifyQuestionDraft, **kwargs: object) -> ClarifyAnalysis:
    return ClarifyAnalysis(questions=list(questions), **kwargs)  # type: ignore[arg-type]


def _option(label: str) -> ClarifyOptionDraft:
    return ClarifyOptionDraft(label=label)


def test_a_perfect_answer_is_passed_through_with_ids_assigned() -> None:
    analysis = _draft(
        ClarifyQuestionDraft(
            question="Where do refund decisions come from?",
            why="It decides the first branch.",
            options=[_option("A refund-policy agent"), _option("A human reviewer")],
            recommended="A refund-policy agent",
        ),
        needs_clarification=True,
        assumptions=["The ticket arrives as text."],
        understanding="Triage a support ticket.",
    )

    result = sanitize_analysis(analysis)

    assert result.needs_clarification is True
    assert [question.id for question in result.questions] == ["q1"]
    assert result.questions[0].recommended == "A refund-policy agent"
    assert result.assumptions == ["The ticket arrives as text."]
    assert result.understanding == "Triage a support ticket."


def test_more_questions_than_the_cap_are_truncated_not_rejected() -> None:
    questions = [
        ClarifyQuestionDraft(
            question=f"Question {index}?",
            why="Because.",
            options=[_option("a"), _option("b")],
            recommended="a",
        )
        for index in range(6)
    ]
    result = sanitize_analysis(_draft(*questions))
    assert len(result.questions) == 4
    assert [question.id for question in result.questions] == ["q1", "q2", "q3", "q4"]


def test_a_recommended_option_outside_the_list_is_coerced() -> None:
    result = sanitize_analysis(
        _draft(
            ClarifyQuestionDraft(
                question="Who reads the input?",
                why="Shape.",
                options=[_option("An agent"), _option("A script")],
                recommended="Something else entirely",
            )
        )
    )
    assert result.questions[0].recommended == "An agent"


def test_a_question_with_one_option_is_dropped() -> None:
    result = sanitize_analysis(
        _draft(
            ClarifyQuestionDraft(
                question="Only one choice?",
                why="Shape.",
                options=[_option("Yes")],
                recommended="Yes",
            ),
            ClarifyQuestionDraft(
                question="A real question?",
                why="Shape.",
                options=[_option("Yes"), _option("No")],
                recommended="No",
            ),
        )
    )
    assert [question.question for question in result.questions] == ["A real question?"]


def test_blank_questions_and_labels_are_dropped_and_duplicates_collapsed() -> None:
    result = sanitize_analysis(
        _draft(
            ClarifyQuestionDraft(question="   ", options=[_option("a"), _option("b")]),
            ClarifyQuestionDraft(
                question="Real?",
                options=[_option("same"), _option("same"), _option("   "), _option("other")],
                recommended="same",
            ),
        )
    )
    assert len(result.questions) == 1
    assert [option.label for option in result.questions[0].options] == ["same", "other"]


def test_more_options_than_allowed_are_cut_to_the_cap() -> None:
    """The draft schema is permissive, so the panel's "2-4 options" rule is here.

    A model that returns nine options must produce four -- not a 502, and not a
    nine-radio question the user has to read.
    """
    result = sanitize_analysis(
        _draft(
            ClarifyQuestionDraft(
                question="Which one?",
                why="Shape.",
                options=[_option(f"option {index}") for index in range(9)],
                recommended="option 3",
            )
        )
    )
    labels = [option.label for option in result.questions[0].options]
    assert labels == ["option 0", "option 1", "option 2", "option 3"]
    # The recommendation survived because it is inside the cut.
    assert result.questions[0].recommended == "option 3"


def test_a_question_with_no_options_at_all_is_dropped() -> None:
    result = sanitize_analysis(_draft(ClarifyQuestionDraft(question="Which?", options=[])))
    assert result.questions == []
    assert result.needs_clarification is False


def test_needs_clarification_is_forced_to_agree_with_the_questions() -> None:
    """A model that says "yes, ask" but returns nothing usable is a plain draft."""
    result = sanitize_analysis(_draft(needs_clarification=True))
    assert result.needs_clarification is False

    result = sanitize_analysis(
        _draft(
            ClarifyQuestionDraft(
                question="Real?", options=[_option("a"), _option("b")], recommended="a"
            ),
            needs_clarification=False,
        )
    )
    assert result.needs_clarification is True


def test_assumptions_are_capped_deduped_and_cleaned() -> None:
    analysis = ClarifyAnalysis(
        assumptions=["  one   spaced  ", "one spaced", "two", "three", "four", "five", "six"],
        understanding="  A   workflow.  ",
    )
    result = sanitize_analysis(analysis)
    assert len(result.assumptions) <= 6
    assert result.assumptions[0] == "one spaced"
    assert len(result.assumptions) == len(set(result.assumptions))
    assert result.understanding == "A workflow."


def test_long_text_is_truncated_with_an_ellipsis() -> None:
    result = sanitize_analysis(
        _draft(
            ClarifyQuestionDraft(
                question="q" * 500,
                options=[_option("a" * 500), _option("b")],
                recommended="a" * 500,
            )
        )
    )
    assert len(result.questions[0].question) == 301
    assert len(result.questions[0].options[0].label) == 301
    # The coerced recommendation still matches a surviving label.
    assert result.questions[0].recommended == result.questions[0].options[0].label


# ---------------------------------------------------------------------------
# The dry-run stub
# ---------------------------------------------------------------------------


def test_the_fake_asks_one_canned_question_for_a_short_description() -> None:
    result = sanitize_analysis(fake_analysis("Handle invoices."))
    assert result.needs_clarification is True
    assert len(result.questions) == 1
    assert len(result.questions[0].options) == 3
    assert result.questions[0].recommended == "A single piece of text"


def test_the_fake_answers_ok_for_a_detailed_description() -> None:
    description = " ".join(["step"] * 50)
    result = sanitize_analysis(fake_analysis(description))
    assert result.needs_clarification is False
    assert result.questions == []
    assert result.assumptions


@pytest.mark.parametrize(
    ("fake_env", "dry_run", "expected"),
    [
        (None, False, False),
        ("1", False, True),
        (None, True, True),
        ("1", True, True),
    ],
)
def test_the_fake_switch_agrees_with_the_drafter(
    fake_env: str | None,
    dry_run: bool,
    expected: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One switch: the analysis pass and the draft must never disagree."""
    from swarm_builder import runtime
    from swarm_builder.appconfig import AppConfig, save_config
    from swarm_builder.compile import generate as generate_module

    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("SWARM_CONFIG", str(tmp_path / "settings.json"))
    if fake_env is None:
        monkeypatch.delenv("SWARM_FAKE_GENERATE", raising=False)
    else:
        monkeypatch.setenv("SWARM_FAKE_GENERATE", fake_env)
    if dry_run:
        save_config(AppConfig(dry_run=True), tmp_path / "settings.json")

    assert fake_clarify_enabled() is expected
    assert runtime.fake_draft_enabled() is expected
    assert generate_module.fake_generate_enabled() is expected


async def test_analyze_without_a_model_and_without_the_fake_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWARM_CONFIG", str(tmp_path / "settings.json"))
    monkeypatch.delenv("SWARM_FAKE_GENERATE", raising=False)
    with pytest.raises(ValueError):
        await analyze("A description.", model=None)


async def test_analyze_uses_the_scripted_agent_and_sanitizes_its_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWARM_CONFIG", str(tmp_path / "settings.json"))
    monkeypatch.delenv("SWARM_FAKE_GENERATE", raising=False)
    captured: dict[str, object] = {}

    class _Run:
        output = ClarifyAnalysis(
            needs_clarification=True,
            questions=[
                ClarifyQuestionDraft(
                    question="Where does the data come from?",
                    why="It changes the entry node.",
                    options=[_option("An email"), _option("A file")],
                    recommended="A file",
                )
            ],
        )

    class _Agent:
        async def run(self, prompt: object, **kwargs: object) -> _Run:
            captured["prompt"] = prompt
            return _Run()

    monkeypatch.setattr(clarify_module, "build_clarify_agent", lambda model, **_: _Agent())

    result = await analyze("Handle the invoices.", model="test:model")

    assert result.needs_clarification is True
    assert result.questions[0].id == "q1"
    assert captured["prompt"] == "Handle the invoices."


async def test_analyze_sends_attachment_text_as_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWARM_CONFIG", str(tmp_path / "settings.json"))
    monkeypatch.delenv("SWARM_FAKE_GENERATE", raising=False)
    captured: dict[str, object] = {}

    class _Run:
        output = ClarifyAnalysis(needs_clarification=False, assumptions=["assumed"])

    class _Agent:
        async def run(self, prompt: object, **kwargs: object) -> _Run:
            captured["prompt"] = prompt
            return _Run()

    monkeypatch.setattr(clarify_module, "build_clarify_agent", lambda model, **_: _Agent())
    workspace = tmp_path / "workspace"
    record = store_module.save_attachment(
        workspace,
        "orders.csv",
        b"Order ID\n42\n",
        Extraction(text="| Order ID |\n| --- |\n| 42 |", truncated=False, notes=[]),
        "text/csv",
        "csv",
    )
    loaded = store_module.load_attachments(workspace, [record.id])

    await analyze("Handle the orders.", model="test:model", attachments=loaded)

    prompt = captured["prompt"]
    assert isinstance(prompt, list)
    assert "| 42 |" in prompt[1]


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    monkeypatch.setenv("SWARM_CONFIG", str(tmp_path / "workspace" / "settings.json"))
    monkeypatch.setenv("SWARM_FAKE_GENERATE", "1")
    monkeypatch.delenv("SWARM_MODEL", raising=False)
    return tmp_path / "workspace"


def test_the_route_returns_questions_for_a_vague_description(workspace: Path) -> None:
    _ = workspace
    response = TestClient(create_app()).post(
        "/api/graphs/generate/clarify", json={"description": "Handle invoices."}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["needsClarification"] is True
    assert len(body["questions"]) == 1
    question = body["questions"][0]
    assert question["id"] == "q1"
    assert question["why"]
    assert len(question["options"]) >= 2
    assert question["recommended"] in [option["label"] for option in question["options"]]
    assert body["dryRun"] is True
    assert body["model"]["source"] == "bundle-default"


def test_the_route_returns_ok_for_a_detailed_description(workspace: Path) -> None:
    _ = workspace
    response = TestClient(create_app()).post(
        "/api/graphs/generate/clarify",
        json={"description": " ".join(["Take a ticket and classify it."] * 12)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["needsClarification"] is False
    assert body["questions"] == []
    assert body["assumptions"]


def test_the_route_validates_the_description(workspace: Path) -> None:
    _ = workspace
    response = TestClient(create_app()).post(
        "/api/graphs/generate/clarify", json={"description": ""}
    )
    assert response.status_code == 422


def test_the_route_refuses_an_unknown_attachment_id(workspace: Path) -> None:
    _ = workspace
    response = TestClient(create_app()).post(
        "/api/graphs/generate/clarify",
        json={"description": "Handle invoices.", "attachmentIds": ["0" * 32]},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "attachment_expired"


def test_the_route_uses_an_uploaded_attachment(workspace: Path) -> None:
    _ = workspace
    client = TestClient(create_app())
    upload = client.post(
        "/api/graphs/attachments",
        files={"file": ("orders.csv", io.BytesIO(b"Order ID\n42\n"), "text/csv")},
    )
    assert upload.status_code == 200, upload.text
    attachment_id = upload.json()["attachment"]["id"]

    response = client.post(
        "/api/graphs/generate/clarify",
        json={"description": "Handle invoices.", "attachmentIds": [attachment_id]},
    )
    assert response.status_code == 200, response.text


def test_the_route_applies_the_image_gate_before_calling_the_model(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clarify pass must refuse an image the model cannot read.

    It sends text extracts rather than image bytes, but it is still part of the
    same request the user is assembling: failing here with the fix named beats
    drafting a graph that ignored the attachment they chose.
    """
    from swarm_builder.attachments import store as store_module
    from swarm_builder.attachments.models import Extraction

    record = store_module.save_attachment(
        workspace,
        "sketch.png",
        _png(),
        Extraction(text="", truncated=False, notes=["image"]),
        "image/png",
        "image",
    )
    monkeypatch.delenv("SWARM_FAKE_GENERATE", raising=False)
    monkeypatch.setenv("SWARM_MODEL", "deepseek:deepseek-v4-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    response = TestClient(create_app()).post(
        "/api/graphs/generate/clarify",
        json={"description": "Read the sketch.", "attachmentIds": [record.id]},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "image_input_unsupported"


def _png() -> bytes:
    """A genuinely decodable PNG (uploads verify the bytes)."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_the_route_needs_a_model_when_it_is_not_a_dry_run(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = workspace
    monkeypatch.delenv("SWARM_FAKE_GENERATE", raising=False)
    response = TestClient(create_app()).post(
        "/api/graphs/generate/clarify", json={"description": "Handle invoices."}
    )
    assert response.status_code == 503
    assert "No model is configured yet" in response.json()["detail"]


def test_the_clarify_path_is_in_the_openapi_document() -> None:
    schema = create_app().openapi()
    assert "/api/graphs/generate/clarify" in schema["paths"]
    assert "ClarifyResponse" in schema["components"]["schemas"]
    properties = schema["components"]["schemas"]["ClarifyQuestionOut"]["properties"]
    assert "recommended" in properties
    # No per-request model override on the analysis pass.
    assert "modelOverride" not in schema["components"]["schemas"]["ClarifyRequest"]["properties"]


def test_answer_dataclass_is_the_one_the_drafter_imports() -> None:
    from swarm_builder.compile import generate as generate_module

    assert generate_module.ClarifyAnswerIn is ClarifyAnswerIn
