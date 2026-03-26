"""Unit tests for Claude live PTY manager internals."""

import asyncio
from time import monotonic
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


def _build_session(
    *,
    scope: str = "C123:thread:123.456",
    session_id: str = "11111111-1111-1111-1111-111111111111",
) -> _LivePtySession:
    return _LivePtySession(
        scope=scope,
        process=_FakeProcess(),
        master_fd=0,
        session_id=session_id,
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
                            max_output_chars=5000,
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
                            max_output_chars=5000,
                            idle_session_timeout_seconds=600,
                            log_prefix="",
                        )

        assert result.success is False
        assert result.was_cancelled is True
        assert result.error == "Cancelled"

    def test_prompt_marker_avoids_common_false_positives(self):
        """Prompt detector should avoid matching quoted content lines."""
        manager = ClaudeLivePtyManager()

        assert manager._tail_has_prompt_marker("\n> ")
        assert manager._tail_has_prompt_marker("\n❯ ")
        assert manager._tail_has_prompt_marker("\n  >  ")
        assert not manager._tail_has_prompt_marker("\n> quoted text")
        assert not manager._tail_has_prompt_marker("Markdown uses > for quotes.\n")

    @pytest.mark.asyncio
    async def test_output_truncation_keeps_recent_tail(self):
        """Large PTY output should retain recent tail and include truncation notice."""
        manager = ClaudeLivePtyManager()
        session = _build_session()
        reads = iter(["AAAAAAAA", "BBBBBBBB", None, None])

        async def _fake_read(_session, timeout_seconds: float):
            _ = timeout_seconds
            return next(reads)

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
                            turn_id="turn-3",
                            turn_timeout_seconds=5,
                            read_timeout_seconds=0.0,
                            settle_seconds=0.0,
                            cancel_settle_seconds=0.1,
                            promptless_idle_fallback_seconds=0.0,
                            max_output_chars=10,
                            idle_session_timeout_seconds=600,
                            log_prefix="",
                        )

        assert result.success is True
        assert "_Output truncated" in result.output
        assert "BBBBBBBB" in result.output
        assert "AAAAAAAA" not in result.output

    @pytest.mark.asyncio
    async def test_idle_cleanup_skips_active_turns(self):
        """Idle cleanup should never reap currently active turns."""
        manager = ClaudeLivePtyManager()
        session = _build_session()
        session.active_turn_id = "turn-active"
        session.last_activity = monotonic() - 3600
        manager._sessions[session.scope] = session

        with patch.object(manager, "_terminate_session", new=AsyncMock()) as mock_term:
            await manager.close_idle_sessions(idle_timeout_seconds=1)

        mock_term.assert_not_awaited()
        assert session.scope in manager._sessions

    @pytest.mark.asyncio
    async def test_idle_janitor_reaps_stale_sessions(self):
        """Background janitor should periodically close stale idle sessions."""
        manager = ClaudeLivePtyManager()
        session = _build_session()
        session.last_activity = monotonic() - 3600
        manager._sessions[session.scope] = session

        with patch.object(manager, "_terminate_session", new=AsyncMock()) as mock_term:
            await manager.ensure_idle_janitor(
                idle_timeout_seconds=1, interval_seconds=0.2
            )
            await asyncio.sleep(0.25)
            await manager.stop_idle_janitor()

        mock_term.assert_awaited_once()
        assert session.scope not in manager._sessions

    @pytest.mark.asyncio
    async def test_scope_lock_allows_other_scopes_while_replacing(self):
        """Slow replacement in one scope should not block session creation in another scope."""
        manager = ClaudeLivePtyManager()
        stale_a = _build_session(
            scope="scope-a", session_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        )
        stale_a.turn_count = 1
        manager._sessions[stale_a.scope] = stale_a

        spawned_a = _build_session(
            scope="scope-a", session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        )
        spawned_b = _build_session(
            scope="scope-b", session_id="cccccccc-cccc-cccc-cccc-cccccccccccc"
        )

        async def _fake_spawn(
            *,
            session_scope: str,
            working_directory: str,
            requested_resume: str | None,
            model: str | None,
            permission_mode: str,
            normalized_dirs: tuple[str, ...],
            log_prefix: str,
        ) -> _LivePtySession:
            _ = (
                working_directory,
                requested_resume,
                model,
                permission_mode,
                normalized_dirs,
                log_prefix,
            )
            return spawned_a if session_scope == "scope-a" else spawned_b

        async def _slow_terminate(session: _LivePtySession) -> None:
            if session.scope == "scope-a":
                await asyncio.sleep(0.2)

        kwargs = dict(
            working_directory="/tmp",
            resume_session_id=None,
            model="claude-opus-4-6",
            permission_mode="bypassPermissions",
            added_dirs=[],
            log_prefix="",
        )

        with patch.object(
            manager, "_spawn_session", new=AsyncMock(side_effect=_fake_spawn)
        ):
            with patch.object(
                manager,
                "_terminate_session",
                new=AsyncMock(side_effect=_slow_terminate),
            ):
                with patch.object(manager, "_drain_pending_output", new=AsyncMock()):
                    task_a = asyncio.create_task(
                        manager._ensure_session(session_scope="scope-a", **kwargs)
                    )
                    await asyncio.sleep(0.02)
                    session_b = await asyncio.wait_for(
                        manager._ensure_session(session_scope="scope-b", **kwargs),
                        timeout=0.1,
                    )
                    session_a = await task_a

        assert session_b.scope == "scope-b"
        assert session_a.scope == "scope-a"
