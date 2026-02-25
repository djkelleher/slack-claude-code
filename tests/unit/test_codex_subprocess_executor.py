"""Unit tests for Codex subprocess executor command construction."""

from unittest.mock import AsyncMock, patch

import pytest

from src.config import config
from src.codex.subprocess_executor import SubprocessExecutor


class _DummyStdout:
    """Simple async stdout stream for subprocess mocks."""

    async def readline(self) -> bytes:
        return b""


class _DummyStderr:
    """Simple async stderr stream for subprocess mocks."""

    async def read(self) -> bytes:
        return b""


class _DummyProcess:
    """Simple subprocess mock compatible with asyncio interfaces."""

    def __init__(self) -> None:
        self.stdout = _DummyStdout()
        self.stderr = _DummyStderr()
        self.returncode = 0

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -1


class TestCodexSubprocessExecutor:
    """Tests for Codex CLI command construction."""

    @pytest.mark.asyncio
    async def test_resume_uses_dangerous_bypass_and_supported_positional_order(self):
        """Resume commands place options before session/prompt and skip unsupported flags."""
        executor = SubprocessExecutor()
        process = _DummyProcess()

        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ) as mock_exec:
            result = await executor.execute(
                prompt="follow-up prompt",
                working_directory="/tmp/workspace",
                resume_session_id="019c922c-2b7a-7782-80c7-f83fb79ca53c",
                sandbox_mode="workspace-write",
                approval_mode="on-request",
                model="gpt-5.3-codex-high",
            )

        args = mock_exec.await_args.args

        assert args[:5] == (
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "exec",
            "resume",
            "--json",
        )
        assert "--sandbox" not in args
        assert "--cd" not in args
        assert "--full-auto" not in args
        assert args[-2] == "019c922c-2b7a-7782-80c7-f83fb79ca53c"
        assert args[-1] == "follow-up prompt"
        assert result.success is True

    @pytest.mark.asyncio
    async def test_new_execution_uses_dangerous_bypass_and_cd(self):
        """New executions use dangerous bypass wrapper behavior and still pass --cd."""
        executor = SubprocessExecutor()
        process = _DummyProcess()

        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ) as mock_exec:
            result = await executor.execute(
                prompt="new prompt",
                working_directory="/tmp/workspace",
                resume_session_id=None,
                sandbox_mode="workspace-write",
                approval_mode="never",
                model="gpt-5.3-codex",
            )

        args = mock_exec.await_args.args

        assert args[:4] == (
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "exec",
            "--json",
        )
        assert "--sandbox" not in args
        assert "--cd" in args
        assert "--full-auto" not in args
        assert args[-1] == "new prompt"
        assert result.success is True

    @pytest.mark.asyncio
    async def test_new_execution_includes_sandbox_and_full_auto_when_dangerous_disabled(
        self, monkeypatch
    ):
        """When dangerous bypass is disabled, executor uses sandbox/full-auto flags."""
        monkeypatch.setattr(config, "CODEX_USE_DANGEROUS_BYPASS", False)
        executor = SubprocessExecutor()
        process = _DummyProcess()

        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ) as mock_exec:
            result = await executor.execute(
                prompt="new prompt",
                working_directory="/tmp/workspace",
                resume_session_id=None,
                sandbox_mode="workspace-write",
                approval_mode="never",
                model="gpt-5.3-codex",
            )

        args = mock_exec.await_args.args

        assert args[:3] == ("codex", "exec", "--json")
        assert "--sandbox" in args
        assert "--cd" in args
        assert "--full-auto" in args
        assert args[-1] == "new prompt"
        assert result.success is True

    @pytest.mark.asyncio
    async def test_exec_prepends_default_instructions_when_file_exists(self, monkeypatch, tmp_path):
        """Executor prepends default instructions from file before prompt."""
        instructions = tmp_path / "default_instructions.txt"
        instructions.write_text("ALWAYS BE CONCISE", encoding="utf-8")
        monkeypatch.setattr(config, "CODEX_DEFAULT_INSTRUCTIONS_FILE", str(instructions))
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", True)

        executor = SubprocessExecutor()
        process = _DummyProcess()

        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ) as mock_exec:
            result = await executor.execute(
                prompt="do a repo review",
                working_directory="/tmp/workspace",
                resume_session_id=None,
                sandbox_mode="workspace-write",
                approval_mode="on-request",
                model="gpt-5.3-codex",
            )

        args = mock_exec.await_args.args
        assert args[-1] == "ALWAYS BE CONCISE\n\ndo a repo review"
        assert result.success is True
