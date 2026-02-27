"""Unit tests for session formatter helpers."""

from datetime import datetime

from src.database.models import Session
from src.utils.formatters.session import session_cleanup_result, session_list


def test_session_list_empty():
    """Empty session list renders a friendly empty-state message."""
    blocks = session_list([])
    assert len(blocks) == 3
    assert blocks[2]["type"] == "section"
    assert "No sessions found" in blocks[2]["text"]["text"]


def test_session_list_renders_backend_and_scope():
    """Session list includes backend, scope, and activity metadata."""
    sessions = [
        Session(
            id=1,
            channel_id="C123",
            thread_ts=None,
            model="opus",
            claude_session_id="claude-1",
            last_active=datetime.now(),
        ),
        Session(
            id=2,
            channel_id="C123",
            thread_ts="123.45",
            model="gpt-5.3-codex",
            codex_session_id="thread-1",
            last_active=datetime.now(),
        ),
    ]

    blocks = session_list(sessions)
    section_blocks = [block for block in blocks if block["type"] == "section"]
    assert len(section_blocks) == 2
    assert any("`claude`" in field["text"] for field in section_blocks[0]["fields"])
    assert any("`codex`" in field["text"] for field in section_blocks[1]["fields"])
    assert any("`channel`" in field["text"] for field in section_blocks[0]["fields"])
    assert any("`thread`" in field["text"] for field in section_blocks[1]["fields"])


def test_session_cleanup_result_message():
    """Cleanup result block includes deleted count and retention window."""
    blocks = session_cleanup_result(7, 30)
    assert len(blocks) == 1
    text = blocks[0]["text"]["text"]
    assert "7" in text
    assert "30" in text
