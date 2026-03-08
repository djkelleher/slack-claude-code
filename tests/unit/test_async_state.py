"""Unit tests for loop-safe async state helpers."""

import asyncio
from dataclasses import dataclass
from typing import Optional

import pytest

from src.backends.process_registry import ProcessRegistry
from src.utils.pending_manager import PendingManager


@dataclass
class _PendingItem:
    session_id: str
    future: Optional[asyncio.Future]


@pytest.mark.asyncio
async def test_pending_manager_clears_stale_items_after_event_loop_change():
    """Pending items from a previous loop should be dropped safely."""
    manager = PendingManager[_PendingItem]()
    manager._pending["stale"] = _PendingItem(session_id="s1", future=None)
    manager._loop = object()

    assert await manager.count() == 0


def test_process_registry_build_track_id_uses_unique_fallback():
    """Anonymous executions should not collide on the default track id."""
    first = ProcessRegistry.build_track_id(None, None, "C123")
    second = ProcessRegistry.build_track_id(None, None, "C123")

    assert first.startswith("C123_anon-")
    assert second.startswith("C123_anon-")
    assert first != second
