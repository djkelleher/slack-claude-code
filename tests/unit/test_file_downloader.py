"""Unit tests for file downloader helpers."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.utils.execution_scope import build_session_scope
from src.utils.file_downloader import (
    FileDownloadError,
    FileTooLargeError,
    _prepare_local_path,
    is_snippet,
    save_snippet_content,
)


def test_build_session_scope_formats_channel_and_thread_scopes() -> None:
    """Execution scopes should distinguish channel and thread sessions."""
    assert build_session_scope("C123", None) == "C123:channel"
    assert build_session_scope("C123", "123.456") == "C123:thread:123.456"


def test_is_snippet_detects_mode_and_filetype() -> None:
    """Slack snippet detection should accept both snippet modes and filetypes."""
    assert is_snippet({"mode": "snippet"}) is True
    assert is_snippet({"filetype": "text"}) is True
    assert is_snippet({"mode": "hosted", "filetype": "png"}) is False


def test_prepare_local_path_sanitizes_hidden_and_duplicate_names(tmp_path) -> None:
    """Local path preparation should sanitize names and avoid collisions."""
    existing = tmp_path / "report.txt"
    existing.write_text("taken", encoding="utf-8")

    duplicate = _prepare_local_path(
        destination_dir=str(tmp_path),
        filename="report.txt",
        fallback_name="upload_f1",
    )
    hidden = _prepare_local_path(
        destination_dir=str(tmp_path),
        filename=".secret",
        fallback_name="snippet_f2",
        default_suffix=".txt",
    )

    assert duplicate.name == "report_1.txt"
    assert hidden.name == "snippet_f2.txt"


def test_save_snippet_content_writes_content_and_uses_preview_fallback(tmp_path) -> None:
    """Snippet saving should write content locally and fall back to preview text."""
    client = SimpleNamespace(token="xoxb-test")

    async def immediate_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    with patch("src.utils.file_downloader.asyncio.to_thread", new=immediate_to_thread):
        local_path, metadata = asyncio.run(
            save_snippet_content(
                client=client,
                file_id="F123",
                file_info={
                    "name": "../notes.py",
                    "size": 12,
                    "content": "print('hi')\n",
                    "mimetype": "text/x-python",
                },
                destination_dir=str(tmp_path),
            )
        )

    assert Path(local_path).read_text(encoding="utf-8") == "print('hi')\n"
    assert metadata["filename"] == "../notes.py"
    assert metadata["is_snippet"] is True
    assert Path(local_path).name == "notes.py"

    with patch("src.utils.file_downloader.asyncio.to_thread", new=immediate_to_thread):
        preview_path, preview_meta = asyncio.run(
            save_snippet_content(
                client=client,
                file_id="F124",
                file_info={
                    "name": "preview.txt",
                    "size": 7,
                    "preview": "preview",
                },
                destination_dir=str(tmp_path),
            )
        )

    assert Path(preview_path).read_text(encoding="utf-8") == "preview"
    assert preview_meta["local_path"] == preview_path


def test_save_snippet_content_rejects_oversized_or_empty_content(tmp_path) -> None:
    """Snippet saving should reject oversized and empty files cleanly."""
    client = SimpleNamespace(token="xoxb-test")

    with pytest.raises(FileTooLargeError):
        asyncio.run(
            save_snippet_content(
                client=client,
                file_id="F200",
                file_info={"name": "big.txt", "size": 20},
                destination_dir=str(tmp_path),
                max_size_bytes=10,
            )
        )

    with pytest.raises(FileDownloadError, match="No content available"):
        asyncio.run(
            save_snippet_content(
                client=client,
                file_id="F201",
                file_info={"name": "empty.txt", "size": 0},
                destination_dir=str(tmp_path),
            )
        )
