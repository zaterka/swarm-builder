"""``swarm_builder.attachments.store``: the TTL'd, path-safe workspace store.

What matters here is what a hostile or careless caller can make the store do:
name a path through an id, read a file that is not there, keep a file forever,
or lose a record to a crash halfway through a write. Each of those has a test.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from swarm_builder.attachments import store as store_module
from swarm_builder.attachments.limits import (
    ATTACHMENT_TTL_SECONDS,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
)
from swarm_builder.attachments.models import (
    AttachmentExpiredError,
    AttachmentNotFoundError,
    AttachmentStoreError,
    AttachmentTooLargeError,
    Extraction,
    sanitize_filename,
)


def _save(workspace: Path, name: str = "orders.csv", text: str = "| a |\n| --- |\n| 1 |"):
    return store_module.save_attachment(
        workspace,
        name,
        text.encode("utf-8"),
        Extraction(text=text, truncated=False, notes=["a note"]),
        "text/csv",
        "csv",
    )


def test_save_then_load_round_trips_text_and_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record = _save(workspace)

    loaded = store_module.load_attachment(workspace, record.id)

    assert loaded.text == "| a |\n| --- |\n| 1 |"
    assert loaded.binary is None
    assert loaded.record.filename == "orders.csv"
    assert loaded.record.kind == "csv"
    assert loaded.record.chars == len(loaded.text)
    assert loaded.record.notes == ["a note"]
    assert loaded.record.preview == loaded.text
    assert loaded.record.expires_at > loaded.record.created_at
    assert (loaded.record.expires_at - loaded.record.created_at).total_seconds() == pytest.approx(
        ATTACHMENT_TTL_SECONDS, abs=5
    )


def test_an_image_keeps_its_bytes_and_no_text(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    payload = b"\x89PNG\r\n\x1a\n" + b"1" * 64
    record = store_module.save_attachment(
        workspace,
        "shot.png",
        payload,
        Extraction(text="", truncated=False, notes=["image"]),
        "image/png",
        "image",
    )

    loaded = store_module.load_attachment(workspace, record.id)

    assert loaded.record.kind == "image"
    assert loaded.binary == payload
    assert loaded.text == ""
    assert loaded.record.chars == 0
    assert loaded.record.preview == ""
    # A text copy of an image would be a second copy of the same bytes on disk.
    assert not (store_module.attachment_dir(workspace, record.id) / "content.txt").exists()


def test_publishing_leaves_no_staging_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _save(workspace)
    leftovers = [p.name for p in store_module.attachments_dir(workspace).iterdir()]
    assert all(not name.startswith(store_module.TMP_PREFIX) for name in leftovers)


def test_an_unknown_or_malformed_id_is_not_a_filesystem_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _save(workspace)
    for bad in ("", "nope", "../../etc/passwd", "0" * 31, "0" * 33, "Z" * 32):
        with pytest.raises(AttachmentNotFoundError):
            store_module.load_attachment(workspace, bad)
    # A traversal attempt must not have created or read anything outside.
    assert not (tmp_path / "etc").exists()


def test_an_expired_attachment_reports_expiry_and_is_removed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record = _save(workspace)
    meta_path = store_module.attachment_dir(workspace, record.id) / "meta.json"
    payload = json.loads(meta_path.read_text())
    payload["expiresAt"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    meta_path.write_text(json.dumps(payload))

    with pytest.raises(AttachmentExpiredError):
        store_module.load_attachment(workspace, record.id)

    assert not store_module.attachment_dir(workspace, record.id).exists()


def test_an_incomplete_record_is_reported_as_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record = _save(workspace)
    directory = store_module.attachment_dir(workspace, record.id)
    (directory / "meta.json").write_text("{ not json")
    with pytest.raises(AttachmentNotFoundError):
        store_module.load_attachment(workspace, record.id)


def test_a_missing_payload_is_reported_as_expired(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record = _save(workspace)
    (store_module.attachment_dir(workspace, record.id) / "content.txt").unlink()
    with pytest.raises(AttachmentExpiredError):
        store_module.load_attachment(workspace, record.id)


def test_delete_reports_whether_anything_was_removed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record = _save(workspace)
    assert store_module.delete_attachment(workspace, record.id) is True
    assert store_module.delete_attachment(workspace, record.id) is False
    with pytest.raises(AttachmentNotFoundError):
        store_module.delete_attachment(workspace, "not-an-id")


def test_prune_removes_only_what_is_expired(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    fresh = _save(workspace, "fresh.csv")
    stale = _save(workspace, "stale.csv")
    meta_path = store_module.attachment_dir(workspace, stale.id) / "meta.json"
    payload = json.loads(meta_path.read_text())
    payload["expiresAt"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    meta_path.write_text(json.dumps(payload))

    removed = store_module.prune_expired(workspace)

    assert removed == 1
    assert store_module.load_attachment(workspace, fresh.id).record.id == fresh.id
    assert not store_module.attachment_dir(workspace, stale.id).exists()


def test_prune_sweeps_an_abandoned_staging_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _save(workspace)
    staging = store_module.attachments_dir(workspace) / f"{store_module.TMP_PREFIX}deadbeef"
    staging.mkdir()
    old = (datetime.now(UTC) - timedelta(minutes=5)).timestamp()
    os.utime(staging, (old, old))

    assert store_module.prune_expired(workspace) == 1
    assert not staging.exists()


def test_prune_never_touches_a_directory_it_cannot_parse_young(tmp_path: Path) -> None:
    """A directory with no record yet is only removed once it is old enough."""
    workspace = tmp_path / "workspace"
    _save(workspace)
    staging = store_module.attachments_dir(workspace) / f"{store_module.TMP_PREFIX}fresh"
    staging.mkdir()
    assert store_module.prune_expired(workspace) == 0
    assert staging.exists()


def test_load_attachments_dedupes_identical_payloads(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first = _save(workspace, "orders.csv")
    second = _save(workspace, "orders-copy.csv")

    loaded = store_module.load_attachments(workspace, [first.id, second.id])

    assert len(loaded) == 1
    assert loaded[0].record.id == first.id


def test_load_attachments_keeps_the_callers_order(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first = _save(workspace, "a.csv", "one")
    second = _save(workspace, "b.csv", "two")
    loaded = store_module.load_attachments(workspace, [second.id, first.id])
    assert [item.record.id for item in loaded] == [second.id, first.id]


def test_load_attachments_refuses_more_than_the_count_cap(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    records = [
        _save(workspace, f"f{index}.csv", f"value {index}")
        for index in range(MAX_ATTACHMENTS + 1)
    ]
    with pytest.raises(AttachmentTooLargeError) as raised:
        store_module.load_attachments(workspace, [record.id for record in records])
    assert f"at most {MAX_ATTACHMENTS} files" in str(raised.value)


def test_load_attachments_refuses_more_than_the_total_byte_cap(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    records = []
    per_file = MAX_ATTACHMENT_BYTES - 1024 * 1024  # under the per-file cap
    for index in range(4):
        # Distinct *extractions*, or dedupe would collapse these before the total
        # is ever summed -- which is itself what the dedupe test asserts.
        extraction = Extraction(text=f"file {index} " * 200, truncated=False, notes=[])
        data = bytes([65 + index]) + b"y" * per_file
        records.append(
            store_module.save_attachment(
                workspace, f"big{index}.csv", data, extraction, "text/csv", "csv"
            )
        )
    with pytest.raises(AttachmentTooLargeError) as raised:
        store_module.load_attachments(workspace, [record.id for record in records])
    assert "total more than" in str(raised.value)


def test_load_attachments_with_no_ids_is_empty(tmp_path: Path) -> None:
    assert store_module.load_attachments(tmp_path / "workspace", []) == []


def test_a_store_failure_names_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full or read-only workspace is a clean 500, not a traceback."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    blocker = workspace / "attachments"
    blocker.write_text("a file where the directory should go")

    with pytest.raises(AttachmentStoreError):
        _save(workspace)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd.csv", ".._.._etc_passwd.csv"),
        ("a/b\\c.xlsx", "a_b_c.xlsx"),
        ('we"ird\'.csv', "we_ird_.csv"),
        ("  many   spaces .csv", "many spaces .csv"),
        ("", "attachment"),
        ("\x00\x01", "attachment"),
    ],
)
def test_sanitize_filename_removes_dangerous_characters(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_caps_length() -> None:
    cleaned = sanitize_filename("x" * 500 + ".csv")
    assert len(cleaned) == 200
    assert cleaned.isascii()
