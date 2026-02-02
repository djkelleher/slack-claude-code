"""Unit tests for streaming message utilities."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.claude.streaming import ToolActivity
from src.config import PLANS_DIR
from src.utils.streaming import StreamingMessageState, create_streaming_callback


class TestStreamingMessageState:
    """Tests for StreamingMessageState class."""

    def test_init_default_values(self):
        """StreamingMessageState initializes with correct defaults."""
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test prompt",
            client=MagicMock(),
            logger=MagicMock(),
        )

        assert state.channel_id == "C123"
        assert state.message_ts == "123.456"
        assert state.prompt == "test prompt"
        assert state.accumulated_output == ""
        assert state.smart_concat is False
        assert state.track_tools is False
        assert state.tool_activities == {}

    def test_get_tool_list_empty(self):
        """get_tool_list returns empty list when no tools tracked."""
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=MagicMock(),
            logger=MagicMock(),
        )

        assert state.get_tool_list() == []

    def test_get_tool_list_with_tools(self):
        """get_tool_list returns tracked tools."""
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=MagicMock(),
            logger=MagicMock(),
            track_tools=True,
        )

        tool = ToolActivity(
            id="tool-123",
            name="Read",
            input={"file_path": "/test.py"},
            input_summary="`/test.py`",
        )
        state.tool_activities["tool-123"] = tool

        tools = state.get_tool_list()
        assert len(tools) == 1
        assert tools[0].name == "Read"

    def test_get_session_plan_filename_with_id(self):
        """get_session_plan_filename generates correct filename."""
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=MagicMock(),
            logger=MagicMock(),
            db_session_id=42,
        )

        assert state.get_session_plan_filename() == "plan-session-42.md"

    def test_get_session_plan_filename_without_id(self):
        """get_session_plan_filename returns default when no session ID."""
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=MagicMock(),
            logger=MagicMock(),
        )

        assert state.get_session_plan_filename() == "plan.md"

    def test_get_session_plan_path(self):
        """get_session_plan_path returns correct path."""
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=MagicMock(),
            logger=MagicMock(),
            db_session_id=42,
        )

        path = state.get_session_plan_path()
        assert path == f"{PLANS_DIR}/plan-session-42.md"

    def test_get_execution_plan_filename_with_execution_id(self):
        """get_execution_plan_filename includes execution ID."""
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=MagicMock(),
            logger=MagicMock(),
            db_session_id=42,
        )

        assert state.get_execution_plan_filename("abc123") == "plan-session-42-abc123.md"

    def test_get_execution_plan_filename_without_execution_id(self):
        """get_execution_plan_filename without execution ID."""
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=MagicMock(),
            logger=MagicMock(),
            db_session_id=42,
        )

        assert state.get_execution_plan_filename() == "plan-session-42.md"


class TestStreamingHeartbeat:
    """Tests for heartbeat functionality."""

    @pytest.mark.asyncio
    async def test_start_heartbeat_creates_task(self):
        """start_heartbeat creates a background task."""
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=AsyncMock(),
            logger=MagicMock(),
        )

        state.start_heartbeat()

        assert state._heartbeat_task is not None
        assert not state._heartbeat_task.done()

        # Cleanup
        await state.stop_heartbeat()

    @pytest.mark.asyncio
    async def test_stop_heartbeat_cancels_task(self):
        """stop_heartbeat cancels the background task."""
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=AsyncMock(),
            logger=MagicMock(),
        )

        state.start_heartbeat()
        task = state._heartbeat_task

        await state.stop_heartbeat()

        assert state._heartbeat_task is None
        assert task.done() or task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_heartbeat_idempotent(self):
        """stop_heartbeat is safe to call multiple times."""
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=AsyncMock(),
            logger=MagicMock(),
        )

        # Should not raise even without starting
        await state.stop_heartbeat()
        await state.stop_heartbeat()

        assert state._heartbeat_task is None


class TestStreamingAppendAndUpdate:
    """Tests for append_and_update functionality."""

    @pytest.mark.asyncio
    async def test_append_accumulates_content(self):
        """append_and_update accumulates output content."""
        client = AsyncMock()
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=client,
            logger=MagicMock(),
        )

        await state.append_and_update("Hello ")
        await state.append_and_update("world!")

        assert "Hello" in state.accumulated_output
        assert "world!" in state.accumulated_output

    @pytest.mark.asyncio
    async def test_append_tracks_tools(self):
        """append_and_update tracks tool activities when enabled."""
        client = AsyncMock()
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=client,
            logger=MagicMock(),
            track_tools=True,
        )

        tool = ToolActivity(
            id="tool-123",
            name="Read",
            input={"file_path": "/test.py"},
            input_summary="`/test.py`",
        )

        await state.append_and_update("", [tool])

        assert "tool-123" in state.tool_activities
        assert state.tool_activities["tool-123"].name == "Read"

    @pytest.mark.asyncio
    async def test_append_updates_existing_tool(self):
        """append_and_update updates existing tool with result."""
        client = AsyncMock()
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=client,
            logger=MagicMock(),
            track_tools=True,
        )

        # First add the tool
        tool = ToolActivity(
            id="tool-123",
            name="Read",
            input={"file_path": "/test.py"},
            input_summary="`/test.py`",
        )
        await state.append_and_update("", [tool])

        # Then update with result
        tool_with_result = ToolActivity(
            id="tool-123",
            name="Read",
            input={"file_path": "/test.py"},
            input_summary="`/test.py`",
            result="file contents",
            full_result="full file contents",
            is_error=False,
            duration_ms=100,
        )
        await state.append_and_update("", [tool_with_result])

        assert state.tool_activities["tool-123"].result == "file contents"
        assert state.tool_activities["tool-123"].duration_ms == 100


class TestCreateStreamingCallback:
    """Tests for create_streaming_callback factory."""

    @pytest.mark.asyncio
    async def test_callback_updates_state_on_assistant_message(self):
        """Callback updates state for assistant messages."""
        client = AsyncMock()
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=client,
            logger=MagicMock(),
        )

        callback = create_streaming_callback(state)

        # Create a mock message
        msg = MagicMock()
        msg.type = "assistant"
        msg.content = "Hello from Claude"
        msg.tool_activities = []

        await callback(msg)

        assert "Hello from Claude" in state.accumulated_output

    @pytest.mark.asyncio
    async def test_callback_ignores_non_assistant_messages(self):
        """Callback ignores non-assistant message types."""
        client = AsyncMock()
        state = StreamingMessageState(
            channel_id="C123",
            message_ts="123.456",
            prompt="test",
            client=client,
            logger=MagicMock(),
        )

        callback = create_streaming_callback(state)

        # Create a user message (should be ignored)
        msg = MagicMock()
        msg.type = "user"
        msg.content = "User content"
        msg.tool_activities = []

        await callback(msg)

        assert state.accumulated_output == ""
