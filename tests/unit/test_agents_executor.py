"""Unit tests for AgentExecutor lifecycle behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agents.executor import AgentExecutor
from src.agents.models import AgentConfig, AgentSource


class TestAgentExecutor:
    """Tests for AgentExecutor."""

    @pytest.mark.asyncio
    async def test_resume_uses_completed_execution_session(self):
        """resume() should work after initial execution has completed."""
        first_result = SimpleNamespace(
            success=True,
            output="first output",
            detailed_output="first details",
            session_id="session-abc",
            error=None,
            cost_usd=0.1,
            duration_ms=100,
        )
        second_result = SimpleNamespace(
            success=True,
            output="follow-up output",
            detailed_output="follow-up details",
            session_id="session-abc",
            error=None,
            cost_usd=0.2,
            duration_ms=200,
        )
        subprocess_executor = SimpleNamespace(
            execute=AsyncMock(side_effect=[first_result, second_result])
        )
        executor = AgentExecutor(subprocess_executor=subprocess_executor)
        agent = AgentConfig(
            name="planner",
            description="Test agent",
            source=AgentSource.BUILTIN,
            system_prompt="You are helpful.",
        )

        run_result = await executor.run(
            agent=agent,
            task="initial task",
            working_directory="/tmp",
            channel_id="C123",
            run_in_background=False,
        )
        resume_result = await executor.resume(run_result.execution_id, "follow-up task")

        assert resume_result.success is True
        assert subprocess_executor.execute.await_count == 2
        assert (
            subprocess_executor.execute.await_args_list[1].kwargs["resume_session_id"]
            == "session-abc"
        )
