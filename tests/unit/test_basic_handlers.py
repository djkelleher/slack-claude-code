"""Unit tests for basic slash command handlers."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database.models import CommandHistory, Session
from src.handlers.basic import _parse_history_selection, register_basic_commands


class _FakeApp:
    """Minimal Slack app stub for command registration tests."""

    def __init__(self):
        self.handlers: dict[str, object] = {}

    def command(self, name: str):
        def decorator(func):
            self.handlers[name] = func
            return func

        return decorator


class TestHistorySelectionParsing:
    """Tests for `/hist` argument parsing."""

    def test_defaults_to_recent_window(self):
        """Empty input should select the default recent history range."""
        assert _parse_history_selection("") == (1, 10)

    def test_parses_single_index(self):
        """Single numbers should map to one prompt."""
        assert _parse_history_selection("3") == (3, 3)

    def test_parses_range(self):
        """Inclusive history ranges should be supported."""
        assert _parse_history_selection(" 2 : 5 ") == (2, 5)

    def test_rejects_invalid_order(self):
        """Descending ranges are invalid."""
        with pytest.raises(ValueError, match="greater than or equal"):
            _parse_history_selection("4:1")

    def test_rejects_large_range(self):
        """Ranges should stay within the Slack-friendly display limit."""
        with pytest.raises(ValueError, match="Maximum span is 20"):
            _parse_history_selection("1:21")


@pytest.mark.asyncio
async def test_registers_history_aliases():
    """Basic commands should include both `/hist` and `/h`."""
    app = _FakeApp()
    deps = SimpleNamespace(db=SimpleNamespace(), executor=SimpleNamespace())

    register_basic_commands(app, deps)

    assert "/hist" in app.handlers
    assert "/h" in app.handlers


@pytest.mark.asyncio
async def test_history_single_index_fetches_latest_prompt():
    """`/hist 1` should fetch the most recent prompt in the current session."""
    app = _FakeApp()
    session = Session(id=7, working_directory="/repo")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            get_prompt_history=AsyncMock(
                return_value=(
                    [
                        CommandHistory(
                            id=11,
                            session_id=7,
                            command="latest prompt",
                            status="completed",
                            created_at=datetime(
                                2026, 3, 25, 15, 30, tzinfo=timezone.utc
                            ),
                        )
                    ],
                    4,
                )
            ),
        ),
        executor=SimpleNamespace(),
    )
    register_basic_commands(app, deps)

    handler = app.handlers["/hist"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "1",
            "command": "/hist",
            "thread_ts": "123.456",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.get_prompt_history.assert_awaited_once_with(7, limit=1, offset=0)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["thread_ts"] == "123.456"
    assert kwargs["text"] == "Prompt history"
    assert "Showing prompt #1 of 4" in kwargs["blocks"][0]["text"]["text"]
    rich_text_elements = kwargs["blocks"][2]["elements"]
    assert "#1 | completed" in rich_text_elements[0]["elements"][0]["text"]
    assert rich_text_elements[1]["elements"][0]["text"] == "latest prompt"


@pytest.mark.asyncio
async def test_history_range_clamps_when_fewer_prompts_exist():
    """`/hist 1:4` should show available prompts even when the session has fewer."""
    app = _FakeApp()
    session = Session(id=8, working_directory="/repo")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            get_prompt_history=AsyncMock(
                return_value=(
                    [
                        CommandHistory(
                            id=21,
                            session_id=8,
                            command="most recent",
                            status="completed",
                            created_at=datetime(
                                2026, 3, 25, 15, 31, tzinfo=timezone.utc
                            ),
                        ),
                        CommandHistory(
                            id=20,
                            session_id=8,
                            command="second most recent",
                            status="failed",
                            created_at=datetime(
                                2026, 3, 25, 15, 0, tzinfo=timezone.utc
                            ),
                        ),
                    ],
                    2,
                )
            ),
        ),
        executor=SimpleNamespace(),
    )
    register_basic_commands(app, deps)

    handler = app.handlers["/h"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "1:4",
            "command": "/h",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.get_prompt_history.assert_awaited_once_with(8, limit=4, offset=0)
    blocks = client.chat_postMessage.await_args.kwargs["blocks"]
    assert "Requested through #4" in blocks[2]["elements"][0]["text"]
    assert blocks[4]["elements"][0]["elements"][0]["text"].startswith("#1 | completed")
    assert blocks[6]["elements"][0]["elements"][0]["text"].startswith("#2 | failed")


@pytest.mark.asyncio
async def test_history_reports_empty_session():
    """Sessions without recorded prompts should get a simple empty-state reply."""
    app = _FakeApp()
    session = Session(id=9, working_directory="/repo")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            get_prompt_history=AsyncMock(return_value=([], 0)),
        ),
        executor=SimpleNamespace(),
    )
    register_basic_commands(app, deps)

    handler = app.handlers["/hist"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/hist",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "No prompt history yet for this session."
    assert "No prompt history yet" in kwargs["blocks"][0]["text"]["text"]


@pytest.mark.asyncio
async def test_history_reports_out_of_range_index():
    """Indexes beyond available history should return a clear error."""
    app = _FakeApp()
    session = Session(id=10, working_directory="/repo")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            get_prompt_history=AsyncMock(return_value=([], 3)),
        ),
        executor=SimpleNamespace(),
    )
    register_basic_commands(app, deps)

    handler = app.handlers["/hist"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "5",
            "command": "/hist",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "History index out of range. This session has 3 prompt(s)."


@pytest.mark.asyncio
async def test_history_rejects_invalid_range():
    """Invalid history syntax should be rejected before hitting the database."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(),
            get_prompt_history=AsyncMock(),
        ),
        executor=SimpleNamespace(),
    )
    register_basic_commands(app, deps)

    handler = app.handlers["/hist"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "4:1",
            "command": "/hist",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.get_or_create_session.assert_not_awaited()
    assert "greater than or equal" in client.chat_postMessage.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_history_uses_prompt_only_query():
    """`/hist` should use prompt history rather than the generic command history query."""
    app = _FakeApp()
    session = Session(id=11, working_directory="/repo")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            get_prompt_history=AsyncMock(return_value=([], 0)),
            get_command_history=AsyncMock(),
        ),
        executor=SimpleNamespace(),
    )
    register_basic_commands(app, deps)

    handler = app.handlers["/hist"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/hist",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.get_prompt_history.assert_awaited_once_with(11, limit=10, offset=0)
    deps.db.get_command_history.assert_not_called()
