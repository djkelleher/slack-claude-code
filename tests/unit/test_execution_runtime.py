"""Unit tests for execution runtime trace finalization."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.database.models import Session
from src.handlers.execution_runtime import execute_prompt_with_runtime


class _FakeStreamingState:
    def __init__(self, **kwargs):
        self.message_ts = kwargs["message_ts"]
        self.accumulated_output = ""

    def start_heartbeat(self):
        return None

    async def stop_heartbeat(self):
        return None

    async def finalize(self, is_error: bool = False):
        return None


@pytest.mark.asyncio
async def test_execute_prompt_with_runtime_finalizes_trace_run_on_exception():
    """Unexpected execution errors should close the trace run as failed."""
    session = Session(id=1, channel_id="C123", model="gpt-5.4")
    trace_run = SimpleNamespace(id=41)
    deps = SimpleNamespace(
        db=SimpleNamespace(
            add_command=AsyncMock(return_value=SimpleNamespace(id=9)),
            update_command_status=AsyncMock(),
        ),
        trace_service=SimpleNamespace(
            start_run=AsyncMock(return_value=SimpleNamespace(run=trace_run)),
            finalize_run_with_status=AsyncMock(),
        ),
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    with patch("src.handlers.execution_runtime.StreamingMessageState", _FakeStreamingState):
        with patch(
            "src.handlers.execution_runtime.create_streaming_callback",
            side_effect=lambda _state: AsyncMock(),
        ):
            with patch(
                "src.handlers.execution_runtime.execute_for_session",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ):
                with patch(
                    "src.handlers.execution_runtime.QuestionManager.cancel_for_session",
                    new=AsyncMock(),
                ):
                    with pytest.raises(RuntimeError, match="boom"):
                        await execute_prompt_with_runtime(
                            deps=deps,
                            session=session,
                            prompt="run tracing",
                            channel_id="C123",
                            thread_ts="123.456",
                            client=client,
                            logger=MagicMock(),
                        )

    deps.trace_service.finalize_run_with_status.assert_awaited_once_with(
        trace_run_id=41,
        final_status="failed",
        output="",
        error="boom",
        git_tool_events=[],
    )


@pytest.mark.asyncio
async def test_execute_prompt_with_runtime_finalizes_trace_run_on_cancellation():
    """Cancelled execution should close the trace run as cancelled."""
    session = Session(id=1, channel_id="C123", model="gpt-5.4")
    trace_run = SimpleNamespace(id=42)
    deps = SimpleNamespace(
        db=SimpleNamespace(
            add_command=AsyncMock(return_value=SimpleNamespace(id=10)),
            update_command_status=AsyncMock(),
        ),
        trace_service=SimpleNamespace(
            start_run=AsyncMock(return_value=SimpleNamespace(run=trace_run)),
            finalize_run_with_status=AsyncMock(),
        ),
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    with patch("src.handlers.execution_runtime.StreamingMessageState", _FakeStreamingState):
        with patch(
            "src.handlers.execution_runtime.create_streaming_callback",
            side_effect=lambda _state: AsyncMock(),
        ):
            with patch(
                "src.handlers.execution_runtime.execute_for_session",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ):
                with patch(
                    "src.handlers.execution_runtime.QuestionManager.cancel_for_session",
                    new=AsyncMock(),
                ):
                    with pytest.raises(asyncio.CancelledError):
                        await execute_prompt_with_runtime(
                            deps=deps,
                            session=session,
                            prompt="run tracing",
                            channel_id="C123",
                            thread_ts="123.456",
                            client=client,
                            logger=MagicMock(),
                        )

    deps.trace_service.finalize_run_with_status.assert_awaited_once_with(
        trace_run_id=42,
        final_status="cancelled",
        output="",
        error="Cancelled",
        git_tool_events=[],
    )
