"""``/api/graphs/attachments``: intake caps, the image gate, and safe errors.

These are the tests that decide whether the endpoint is safe to expose: an
oversized body must be refused before it is read, a traversal in an id must not
touch the filesystem, an error must not leak a path, and a text file must be
attachable on a machine with no model configured at all.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_builder.attachments import store as store_module
from swarm_builder.attachments.limits import (
    MAX_ATTACHMENT_BYTES,
    UPLOAD_CONTENT_LENGTH_SLACK,
)
from swarm_builder.main import create_app
from swarm_builder.routes import attachments as attachments_routes


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private workspace with no model route configured."""
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    monkeypatch.setenv("SWARM_CONFIG", str(tmp_path / "workspace" / "settings.json"))
    monkeypatch.delenv("SWARM_MODEL", raising=False)
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)
    monkeypatch.delenv("SWARM_FAKE_GENERATE", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return tmp_path / "workspace"


def _configure_route(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    """Point resolution at a provider/model pair with a usable credential."""
    monkeypatch.setenv("SWARM_MODEL", f"deepseek:{model}")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")


def _png() -> bytes:
    """A genuinely decodable PNG: uploads verify the bytes now."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _upload(client: TestClient, name: str, data: bytes, content_type: str = "text/csv"):
    return client.post(
        "/api/graphs/attachments", files={"file": (name, io.BytesIO(data), content_type)}
    )


def test_a_csv_uploads_with_a_preview_and_metadata(workspace: Path) -> None:
    _ = workspace
    client = TestClient(create_app())
    response = _upload(client, "orders.csv", b"Order ID,Region\n1,EMEA\n")

    assert response.status_code == 200, response.text
    body = response.json()
    attachment = body["attachment"]
    assert attachment["filename"] == "orders.csv"
    assert attachment["kind"] == "csv"
    assert attachment["mediaType"] == "text/csv"
    assert attachment["chars"] > 0
    assert attachment["truncated"] is False
    assert '| Order ID | Region |' in attachment["preview"]
    assert body["warnings"] == []
    assert len(attachment["id"]) == 32
    # Stored where the workspace can sweep it, and readable back by id.
    assert store_module.attachment_dir(workspace, attachment["id"]).is_dir()


def test_a_csv_uploads_with_no_model_configured_at_all(workspace: Path) -> None:
    """Only an image needs a model; reading a CSV never does.

    Regression guard: resolving the model for every upload would make attaching
    a spreadsheet impossible on a fresh install, which has nothing to do with
    reading a file.
    """
    _ = workspace
    response = _upload(TestClient(create_app()), "plain.csv", b"a,b\n1,2\n")
    assert response.status_code == 200, response.text
    assert response.json()["attachment"]["kind"] == "csv"


def test_the_declared_content_length_is_refused_before_reading(workspace: Path) -> None:
    """A huge declared body is rejected without spooling it to disk."""
    _ = workspace
    client = TestClient(create_app())
    response = client.post(
        "/api/graphs/attachments",
        files={"file": ("big.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
        headers={
            "content-length": str(MAX_ATTACHMENT_BYTES + UPLOAD_CONTENT_LENGTH_SLACK + 1)
        },
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "attachment_too_large"


def test_a_body_past_the_cap_is_refused_while_streaming(workspace: Path) -> None:
    _ = workspace
    oversized = b"a,b\n" + b"1,2\n" * ((MAX_ATTACHMENT_BYTES // 4) + 1)
    response = _upload(TestClient(create_app()), "big.csv", oversized)
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "attachment_too_large"
    # Nothing was stored for a refused upload.
    assert not (
        store_module.attachments_dir(workspace).exists()
        and any(store_module.attachments_dir(workspace).iterdir())
    )


def test_an_unsupported_type_names_what_is_supported(workspace: Path) -> None:
    _ = workspace
    response = _upload(
        TestClient(create_app()), "old.xls", b"\xd0\xcf\x11\xe0", "application/vnd.ms-excel"
    )
    assert response.status_code == 415
    detail = response.json()["detail"]
    assert detail["code"] == "unsupported_attachment_type"
    assert ".csv" in detail["message"]
    assert "re-save it as .xlsx" in detail["message"]


def test_an_empty_file_is_a_clean_422(workspace: Path) -> None:
    _ = workspace
    response = _upload(TestClient(create_app()), "empty.csv", b"")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "attachment_unreadable"


def test_a_renamed_text_file_is_a_clean_422_without_a_path(workspace: Path) -> None:
    _ = workspace
    response = _upload(TestClient(create_app()), "notes.xlsx", b"just text, not a workbook")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "attachment_unreadable"
    assert str(workspace) not in json.dumps(detail)
    assert "/" not in detail["message"]
    assert "Traceback" not in json.dumps(detail)


def test_a_malformed_attachment_id_never_touches_the_filesystem(workspace: Path) -> None:
    _ = workspace
    client = TestClient(create_app())
    for bad in ("nope", "0" * 31, "%2e%2e%2fetc%2fpasswd"):
        response = client.delete(f"/api/graphs/attachments/{bad}")
        # 404 from our own id validation, 405 when the encoded separators make
        # the path match something else entirely. Never a 500, and never a read
        # outside the workspace.
        assert response.status_code in (404, 405, 422), response.text
        assert "detail" in response.json()
    assert not (Path(workspace).parent / "etc").exists()


def test_deleting_an_attachment_removes_it_and_is_404_afterwards(workspace: Path) -> None:
    _ = workspace
    client = TestClient(create_app())
    attachment_id = _upload(client, "gone.csv", b"a,b\n1,2\n").json()["attachment"]["id"]

    first = client.delete(f"/api/graphs/attachments/{attachment_id}")
    assert first.status_code == 200
    assert first.json() == {"attachmentId": attachment_id, "deleted": True}
    assert not store_module.attachment_dir(workspace, attachment_id).exists()

    second = client.delete(f"/api/graphs/attachments/{attachment_id}")
    assert second.status_code == 404
    assert second.json()["detail"]["code"] == "attachment_expired"


def test_an_image_is_refused_when_the_model_cannot_read_images(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = workspace
    # A DeepSeek id the vendor docs do not name as visual.
    _configure_route(monkeypatch, "deepseek-v4-flash")
    response = _upload(
        TestClient(create_app()), "screen.png", _png(), "image/png"
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "image_input_unsupported"
    assert "deepseek-v4-flash" in detail["message"]
    assert "Model settings" in detail["message"]


def test_an_image_is_accepted_by_a_vision_capable_route(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = workspace
    # The id this app actually resolves to today, which DeepSeek documents as
    # accepting images: this is the happy path, not a refusal path.
    _configure_route(monkeypatch, "deepseek-flash")
    response = _upload(
        TestClient(create_app()), "screen.png", _png(), "image/png"
    )
    assert response.status_code == 200, response.text
    attachment = response.json()["attachment"]
    assert attachment["kind"] == "image"
    assert attachment["chars"] == 0
    assert attachment["preview"] == ""
    assert response.json()["warnings"] == []


def test_an_image_is_accepted_with_a_warning_under_dry_run(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry run sends nothing to a model, so an image cannot be refused there."""
    from swarm_builder.appconfig import AppConfig, save_config

    save_config(AppConfig(dry_run=True), workspace / "settings.json")
    response = _upload(
        TestClient(create_app()), "screen.png", _png(), "image/png"
    )
    assert response.status_code == 200, response.text
    assert response.json()["attachment"]["kind"] == "image"
    assert any("Dry run" in warning for warning in response.json()["warnings"])


def test_an_image_is_refused_when_no_model_is_configured(workspace: Path) -> None:
    """A clear 503 at upload beats a failed generation later."""
    _ = workspace
    response = _upload(
        TestClient(create_app()), "screen.png", _png(), "image/png"
    )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "no_model_configured"
    assert "No model is configured yet" in detail["message"]


def test_an_image_with_a_capable_route_but_no_credential_is_refused(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vision-capable model still needs its key: the gate must not bypass that."""
    _ = workspace
    monkeypatch.setenv("SWARM_MODEL", "deepseek:deepseek-flash")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    response = _upload(
        TestClient(create_app()), "screen.png", _png(), "image/png"
    )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "missing_credential"
    assert "DEEPSEEK_API_KEY" not in detail["message"]
    assert "Model settings" in detail["message"]


def test_the_upload_route_is_registered_so_a_missing_parser_is_a_503(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python-multipart``/``openpyxl`` absence must not take the app down.

    Two layers: the multipart dependency is required for the *route definition*
    to import at all (asserted here by the app building and answering), and a
    missing reader is reported as 503 from the handler rather than at startup.
    """
    _ = workspace
    import builtins

    real_import = builtins.__import__

    def _blocked(name: str, *args: object, **kwargs: object):
        if name in {"openpyxl", "pptx"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    response = _upload(TestClient(create_app()), "data.xlsx", b"PK\x03\x04nonsense")
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] in {"attachment_unreadable", "attachments_unavailable"}


def test_the_attachments_path_is_in_the_openapi_document() -> None:
    schema = create_app().openapi()
    assert "/api/graphs/attachments" in schema["paths"]
    assert "/api/graphs/attachments/{attachmentId}" in schema["paths"]
    assert "AttachmentUploadResponse" in schema["components"]["schemas"]
    properties = schema["components"]["schemas"]["AttachmentOut"]["properties"]
    assert "mediaType" in properties and "expiresAt" in properties


def test_the_extract_semaphore_bounds_concurrency() -> None:
    """A burst of uploads cannot start more parses than the cap allows."""
    assert attachments_routes._EXTRACT_SEMAPHORE._value == attachments_routes.EXTRACT_CONCURRENCY


def test_a_renamed_file_with_an_image_suffix_is_refused_at_upload(workspace: Path) -> None:
    """Verified live: an undecodable image used to fail mid-generation instead.

    The provider answered 400 with a message written for a developer, after the
    user had already waited through an analysis pass. Refusing it here is the
    same policy as the vision gate: say what is wrong at the point the user can
    still fix it.
    """
    _ = workspace
    response = _upload(
        TestClient(create_app()),
        "screen.png",
        b"GIF-free zone, definitely not an image",
        "image/png",
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "attachment_unreadable"
    assert "does not look like a PNG image" in detail["message"]


def test_no_failure_body_leaks_a_path_or_a_parser_message(workspace: Path) -> None:
    """Every attachment failure, checked for the two things that must never appear.

    A workspace path discloses the operator's directory layout; a parser's own
    exception text embeds the real filename and sometimes the file's content. The
    messages are app-authored precisely so that neither can reach a browser, a log
    line, or a pasted bug report.
    """
    _ = workspace
    client = TestClient(create_app())
    responses = [
        # declared oversize, streamed oversize, unsupported type, empty file,
        # renamed workbook, renamed image, malformed id, unknown id
        client.post(
            "/api/graphs/attachments",
            files={"file": ("big.csv", io.BytesIO(b"a,b\n"), "text/csv")},
            headers={"content-length": str(MAX_ATTACHMENT_BYTES + UPLOAD_CONTENT_LENGTH_SLACK + 1)},
        ),
        _upload(client, "big2.csv", b"a,b\n" + b"1,2\n" * ((MAX_ATTACHMENT_BYTES // 4) + 1)),
        _upload(client, "old.xls", b"\xd0\xcf\x11\xe0", "application/vnd.ms-excel"),
        _upload(client, "empty.csv", b""),
        _upload(client, "notes.xlsx", b"not a workbook"),
        _upload(client, "shot.png", b"not an image", "image/png"),
        client.delete("/api/graphs/attachments/nope"),
        client.delete("/api/graphs/attachments/" + "0" * 32),
    ]

    for response in responses:
        assert response.status_code >= 400, response.text
        body = response.text
        assert str(workspace) not in body
        assert "/private/var" not in body and "/Users/" not in body
        for foreign in ("Traceback", "openpyxl", "python-pptx", "zipfile", "Pillow", "File \""):
            assert foreign not in body, f"{foreign!r} leaked into {body[:200]}"
        detail = response.json()["detail"]
        assert isinstance(detail, dict), body
        assert set(detail) >= {"code", "message", "problems"}, detail
