"""Unit tests for subprocess lifecycle helpers."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.utils import process_utils


class _FakeProcess:
    """Minimal asyncio subprocess stub."""

    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.wait = AsyncMock()

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_terminate_process_safely_skips_finished_process() -> None:
    """Finished processes should not be signaled again."""
    process = _FakeProcess(returncode=0)

    await process_utils.terminate_process_safely(process)

    assert process.terminated is False
    assert process.killed is False
    process.wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminate_process_safely_terminates_responsive_process() -> None:
    """Responsive processes should terminate without a kill fallback."""
    process = _FakeProcess()
    process.wait.return_value = None

    await process_utils.terminate_process_safely(process, timeout=0.01)

    assert process.terminated is True
    assert process.killed is False


@pytest.mark.asyncio
async def test_terminate_process_safely_kills_and_warns_after_timeouts(monkeypatch) -> None:
    """Unresponsive processes should be killed and then warn if still stuck."""
    process = _FakeProcess()
    process.wait.side_effect = [asyncio.TimeoutError(), asyncio.TimeoutError()]
    warnings = []
    monkeypatch.setattr(process_utils.logger, "warning", lambda message: warnings.append(message))

    await process_utils.terminate_process_safely(process, timeout=0.01)

    assert process.terminated is True
    assert process.killed is True
    assert warnings == ["Process did not respond to kill signal"]
