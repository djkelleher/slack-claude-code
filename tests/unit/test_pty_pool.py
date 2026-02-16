"""Unit tests for PTY session pool channel-wide operations."""

import asyncio

import pytest

from src.pty.pool import PTYSessionPool


class _FakeSession:
    def __init__(self, interrupt_result: bool = True) -> None:
        self.interrupt_result = interrupt_result
        self.stop_calls = 0
        self.interrupt_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1

    async def interrupt(self) -> bool:
        self.interrupt_calls += 1
        return self.interrupt_result

    def is_alive(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_remove_by_channel_removes_thread_sessions() -> None:
    original_sessions = PTYSessionPool._sessions
    original_lock = PTYSessionPool._lock
    try:
        c1_root = _FakeSession()
        c1_thread = _FakeSession()
        c2_root = _FakeSession()
        PTYSessionPool._sessions = {
            "C1": c1_root,
            "C1:123.456": c1_thread,
            "C2": c2_root,
        }
        PTYSessionPool._lock = asyncio.Lock()

        removed = await PTYSessionPool.remove_by_channel("C1")

        assert removed == 2
        assert "C1" not in PTYSessionPool._sessions
        assert "C1:123.456" not in PTYSessionPool._sessions
        assert "C2" in PTYSessionPool._sessions
        assert c1_root.stop_calls == 1
        assert c1_thread.stop_calls == 1
        assert c2_root.stop_calls == 0
    finally:
        PTYSessionPool._sessions = original_sessions
        PTYSessionPool._lock = original_lock


@pytest.mark.asyncio
async def test_interrupt_by_channel_counts_matching_sessions() -> None:
    original_sessions = PTYSessionPool._sessions
    original_lock = PTYSessionPool._lock
    try:
        c1_root = _FakeSession(interrupt_result=True)
        c1_thread = _FakeSession(interrupt_result=False)
        c2_root = _FakeSession(interrupt_result=True)
        PTYSessionPool._sessions = {
            "C1": c1_root,
            "C1:123.456": c1_thread,
            "C2": c2_root,
        }
        PTYSessionPool._lock = asyncio.Lock()

        interrupted = await PTYSessionPool.interrupt_by_channel("C1")

        assert interrupted == 1
        assert c1_root.interrupt_calls == 1
        assert c1_thread.interrupt_calls == 1
        assert c2_root.interrupt_calls == 0
    finally:
        PTYSessionPool._sessions = original_sessions
        PTYSessionPool._lock = original_lock
