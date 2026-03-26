"""Unit tests for Claude live PTY manager internals."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.claude.live_pty import ClaudeLivePtyManager, _LivePtySession


class _FakeProcess:
    """Minimal process stand-in for PTY manager tests."""

    def __init__(self) -> None:
        self.returncode = None

    def send_signal(self, _sig) -> None:
        return None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _build_session() -> _LivePtySession:
    return _LivePtySession(
        scope="C123:thread:123.456",
        process=_FakeProcess(),
        master_fd=0,
        session_id="11111111-1111-1111-1111-111111111111",
        working_directory="/tmp",
        model="claude-opus-4-6",
        permission_mode="bypassPermissions",
        added_dirs=(),
    )


class TestClaudeLivePtyManager:
    """Tests for PTY completion and cancellation hardening behavior."""

    @pytest.mark.asyncio
    async def test_promptless_fallback_waits_for_late_output(self):
        """Promptless mode should not terminate immediately on first idle gap."""
        manager = ClaudeLivePtyManager()
        session = _build_session()
        reads = iter(["first\n", None, "second\n", None, None])

        async def _fake_read(_session, timeout_seconds: float):
            item = next(reads)
            if item is None:
                await asyncio.sleep(0.03)
            return item

        with patch.object(
            manager, "_ensure_session", new=AsyncMock(return_value=session)
        ):
            with patch.object(manager, "_drain_pending_output", new=AsyncMock()):
                with patch.object(manager, "_write_text", new=AsyncMock()):
                    with patch.object(
                        manager, "_read_chunk", new=AsyncMock(side_effect=_fake_read)
                    ):
                        result = await manager.execute_turn(
                            session_scope=session.scope,
                            prompt="run",
                            working_directory="/tmp",
                            resume_session_id=session.session_id,
                            model=session.model,
                            permission_mode=session.permission_mode,
                            added_dirs=[],
                            on_chunk=None,
                            turn_id="turn-1",
                            turn_timeout_seconds=5,
                            read_timeout_seconds=0.0,
                            settle_seconds=0.0,
                            cancel_settle_seconds=0.1,
                            promptless_idle_fallback_seconds=0.05,
                            idle_session_timeout_seconds=600,
                            log_prefix="",
                        )

        assert result.success is True
        assert "first" in result.output
        assert "second" in result.output

    @pytest.mark.asyncio
    async def test_cancel_without_output_finishes_quickly(self):
        """Cancellation should settle even when no assistant chunk has arrived yet."""
        manager = ClaudeLivePtyManager()
        session = _build_session()

        async def _fake_write(s: _LivePtySession, text: str) -> None:
            _ = text
            s.cancel_requested = True

        async def _fake_read(_session, timeout_seconds: float):
            _ = timeout_seconds
            await asyncio.sleep(0.02)
            return None

        with patch.object(
            manager, "_ensure_session", new=AsyncMock(return_value=session)
        ):
            with patch.object(manager, "_drain_pending_output", new=AsyncMock()):
                with patch.object(
                    manager, "_write_text", new=AsyncMock(side_effect=_fake_write)
                ):
                    with patch.object(
                        manager, "_read_chunk", new=AsyncMock(side_effect=_fake_read)
                    ):
                        result = await manager.execute_turn(
                            session_scope=session.scope,
                            prompt="run",
                            working_directory="/tmp",
                            resume_session_id=session.session_id,
                            model=session.model,
                            permission_mode=session.permission_mode,
                            added_dirs=[],
                            on_chunk=None,
                            turn_id="turn-2",
                            turn_timeout_seconds=5,
                            read_timeout_seconds=0.0,
                            settle_seconds=0.5,
                            cancel_settle_seconds=0.01,
                            promptless_idle_fallback_seconds=0.5,
                            idle_session_timeout_seconds=600,
                            log_prefix="",
                        )

        assert result.success is False
        assert result.was_cancelled is True
        assert result.error == "Cancelled"
