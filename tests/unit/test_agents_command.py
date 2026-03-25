"""Unit tests for `/agents` execution updates."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.models import (
    AgentConfig,
    AgentModelChoice,
    AgentPermissionMode,
    AgentSource,
)
from src.backends.execution_result import BackendExecutionResult
from src.database.models import Session
from src.handlers.claude.agents_command import _run_agent_with_streaming


class _FakeStreamingState:
    """Minimal streaming state used to isolate agent completion updates."""

    def __init__(self, **kwargs):
        self.message_ts = kwargs["message_ts"]
        self.accumulated_output = ""

    def start_heartbeat(self) -> None:
        """Match the production interface without starting background work."""

    async def stop_heartbeat(self) -> None:
        """Match the production interface without background work."""


def _agent() -> AgentConfig:
    return AgentConfig(
        name="helper",
        description="Helpful agent",
        source=AgentSource.BUILTIN,
        system_prompt="System prompt",
        model=AgentModelChoice.INHERIT,
        permission_mode=AgentPermissionMode.INHERIT,
        is_builtin=True,
    )


def _session() -> Session:
    return Session(
        id=7,
        channel_id="C123",
        model="sonnet",
        permission_mode="default",
        working_directory="/repo",
    )


@pytest.mark.asyncio
async def test_run_agent_with_streaming_updates_completed_status():
    deps = SimpleNamespace(
        executor=SimpleNamespace(
            execute=AsyncMock(
                return_value=BackendExecutionResult(
                    success=True,
                    output="done",
                    duration_ms=321,
                    cost_usd=0.125,
                )
            )
        )
    )
    client = SimpleNamespace(chat_update=AsyncMock())

    with patch("src.handlers.claude.agents_command.StreamingMessageState", _FakeStreamingState):
        with patch(
            "src.handlers.claude.agents_command.create_streaming_callback",
            return_value=AsyncMock(),
        ):
            await _run_agent_with_streaming(
                deps=deps,
                client=client,
                logger=MagicMock(),
                channel_id="C123",
                thread_ts="123.456",
                message_ts="123.789",
                agent=_agent(),
                task="Ship the fix",
                session=_session(),
            )

    kwargs = client.chat_update.await_args.kwargs
    assert kwargs["text"] == "Agent helper completed"
    assert kwargs["blocks"][0]["text"]["text"].startswith(
        ":heavy_check_mark: Agent `helper` completed"
    )
    assert kwargs["blocks"][-1]["elements"][0]["text"] == "Duration: 321ms | Cost: $0.1250"


@pytest.mark.asyncio
async def test_run_agent_with_streaming_updates_failed_status():
    deps = SimpleNamespace(
        executor=SimpleNamespace(
            execute=AsyncMock(
                return_value=BackendExecutionResult(
                    success=False,
                    output="",
                    error="boom",
                )
            )
        )
    )
    client = SimpleNamespace(chat_update=AsyncMock())

    with patch("src.handlers.claude.agents_command.StreamingMessageState", _FakeStreamingState):
        with patch(
            "src.handlers.claude.agents_command.create_streaming_callback",
            return_value=AsyncMock(),
        ):
            await _run_agent_with_streaming(
                deps=deps,
                client=client,
                logger=MagicMock(),
                channel_id="C123",
                thread_ts=None,
                message_ts="123.789",
                agent=_agent(),
                task="Ship the fix",
                session=_session(),
            )

    kwargs = client.chat_update.await_args.kwargs
    assert kwargs["text"] == "Agent helper failed"
    assert kwargs["blocks"][0]["text"]["text"].startswith(":x: Agent `helper` failed")
    assert kwargs["blocks"][2]["text"]["text"] == "boom"
