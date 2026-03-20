"""Unit tests for Claude subprocess executor."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.claude.subprocess_executor import SubprocessExecutor


class _DummyStdout:
    """Simple async stdout stream for subprocess mocks."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [line + "\n" for line in lines]

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0).encode("utf-8")


class _DummyStderr:
    """Simple async stderr stream for subprocess mocks."""

    async def read(self) -> bytes:
        return b""


class _DummyProcess:
    """Simple subprocess mock compatible with asyncio interfaces."""

    def __init__(self, lines: list[str]) -> None:
        self.stdout = _DummyStdout(lines)
        self.stderr = _DummyStderr()
        self.returncode = None
        self.terminated = False

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


def _json_line(payload: dict) -> str:
    return json.dumps(payload)


class TestClaudeSubprocessExecutor:
    """Tests for Claude subprocess execution behavior."""

    @pytest.mark.asyncio
    async def test_exit_plan_mode_is_ignored_in_bypass_mode(self):
        """ExitPlanMode tool calls should not trigger plan approval outside plan mode."""
        process = _DummyProcess(
            [
                _json_line({"type": "system", "session_id": "session-1"}),
                _json_line(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "tool_use", "id": "toolu_01", "name": "ExitPlanMode"}
                            ]
                        },
                    }
                ),
                _json_line({"type": "result", "result": "done", "duration_ms": 1}),
            ]
        )

        executor = SubprocessExecutor()
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            result = await executor.execute(
                prompt="analyze this project",
                working_directory="/tmp",
                permission_mode="bypassPermissions",
                db_session_id=2,
            )

        assert result.success is True
        assert result.has_pending_plan_approval is False
        assert result.output == "done"
        assert process.terminated is False

    @pytest.mark.asyncio
    async def test_exit_plan_mode_triggers_plan_approval_in_plan_mode(self):
        """ExitPlanMode tool calls should trigger plan approval in plan mode."""
        process = _DummyProcess(
            [
                _json_line({"type": "system", "session_id": "session-1"}),
                _json_line(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "tool_use", "id": "toolu_01", "name": "ExitPlanMode"}
                            ]
                        },
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            result = await executor.execute(
                prompt="create a plan",
                working_directory="/tmp",
                permission_mode="plan",
                db_session_id=2,
            )

        assert result.success is True
        assert result.has_pending_plan_approval is True
        assert process.terminated is True
