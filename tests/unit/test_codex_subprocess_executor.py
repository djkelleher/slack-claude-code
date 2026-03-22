"""Unit tests for Codex CLI subprocess executor."""

import asyncio
import signal
from unittest.mock import AsyncMock, patch

import pytest

from src.codex.subprocess_executor import SubprocessExecutor
from src.config import config


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

    def __init__(self, content: str = "") -> None:
        self._content = content

    async def read(self) -> bytes:
        return self._content.encode("utf-8")


class _DummyProcess:
    """Simple subprocess mock compatible with asyncio interfaces."""

    def __init__(self, lines: list[str], *, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = _DummyStdout(lines)
        self.stderr = _DummyStderr(stderr)
        self.returncode = returncode
        self.signals: list[signal.Signals] = []

    async def wait(self) -> int:
        return self.returncode

    def send_signal(self, sig: signal.Signals) -> None:
        self.signals.append(sig)
        self.returncode = 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


class TestCodexSubprocessExecutor:
    """Tests for CLI execution behavior."""

    @pytest.mark.asyncio
    async def test_execute_uses_codex_exec_and_parses_stream(self, monkeypatch):
        """Executor should start `codex exec --json` and accumulate assistant output."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)
        process = _DummyProcess(
            [
                '{"type":"thread.started","thread_id":"thread-1"}',
                '{"type":"turn.started"}',
                '{"type":"assistant","content":"Hello"}',
                '{"type":"turn.completed","usage":{"cost":0.12},"duration_ms":321}',
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ) as mock_exec:
            result = await executor.execute(
                prompt="Say hi",
                working_directory="/tmp/workspace",
                sandbox_mode="workspace-write",
                approval_mode="never",
                model="gpt-5.3-codex-high",
            )

        args = mock_exec.await_args.args
        assert args[:8] == (
            "codex",
            "-a",
            "never",
            "-s",
            "workspace-write",
            "-C",
            "/tmp/workspace",
            "-m",
        )
        assert "gpt-5.3-codex" in args
        assert "exec" in args
        assert "--json" in args
        assert result.success is True
        assert result.session_id == "thread-1"
        assert result.output == "Hello"
        assert result.duration_ms == 321
        assert result.cost_usd == 0.12

    @pytest.mark.asyncio
    async def test_execute_resume_uses_exec_resume_subcommand(self, monkeypatch):
        """Executor should use `codex exec resume` when a session ID is provided."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)
        process = _DummyProcess(
            [
                '{"type":"thread.started","thread_id":"thread-1"}',
                '{"type":"turn.completed"}',
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ) as mock_exec:
            result = await executor.execute(
                prompt="Continue",
                working_directory="/tmp/workspace",
                resume_session_id="thread-1",
            )

        args = mock_exec.await_args.args
        resume_index = args.index("exec")
        assert args[resume_index : resume_index + 3] == ("exec", "resume", "thread-1")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_ignores_transient_reconnecting_errors(self, monkeypatch):
        """Transient reconnect messages should not fail a successful run."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)
        process = _DummyProcess(
            [
                '{"type":"thread.started","thread_id":"thread-1"}',
                '{"type":"error","message":"Reconnecting... 2/5 (stream disconnected)"}',
                '{"type":"assistant","content":"done"}',
                '{"type":"turn.completed"}',
            ]
        )

        executor = SubprocessExecutor()
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            result = await executor.execute(
                prompt="Retry",
                working_directory="/tmp/workspace",
            )

        assert result.success is True
        assert result.error is None
        assert result.output == "done"

    @pytest.mark.asyncio
    async def test_review_start_uses_exec_review_json(self):
        """Review helper should run the CLI review subcommand."""
        process = _DummyProcess(
            [
                '{"type":"thread.started","thread_id":"review-1"}',
                '{"type":"assistant","content":"Looks good"}',
                '{"type":"turn.completed"}',
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ) as mock_exec:
            result = await executor.review_start(
                thread_id="ignored",
                target={"type": "uncommittedChanges"},
                working_directory="/tmp/workspace",
            )

        args = mock_exec.await_args.args
        assert args == (
            "codex",
            "-C",
            "/tmp/workspace",
            "exec",
            "review",
            "--json",
            "--skip-git-repo-check",
            "--uncommitted",
        )
        assert result["turn"]["id"] == "review-1"
        assert result["output"] == "Looks good"

    @pytest.mark.asyncio
    async def test_active_turn_controls_are_disabled(self):
        """CLI mode should report no active turn support."""
        executor = SubprocessExecutor()

        assert await executor.has_active_turn("scope-1") is False
        assert await executor.get_active_turn("scope-1") is None

        steer_result = await executor.steer_active_turn("scope-1", "continue", timeout=0.1)
        interrupt_result = await executor.interrupt_active_turn("scope-1", timeout=0.1)

        assert steer_result.success is False
        assert "not supported" in (steer_result.error or "")
        assert interrupt_result.success is False
        assert "not supported" in (interrupt_result.error or "")
