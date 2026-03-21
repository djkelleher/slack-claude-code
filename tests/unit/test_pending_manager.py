"""Unit tests for PendingManager."""

import asyncio
from dataclasses import dataclass
from typing import Optional

import pytest

from src.utils.pending_manager import PendingManager


@dataclass
class _PendingItem:
    session_id: str
    future: Optional[asyncio.Future]


@pytest.mark.asyncio
async def test_pending_manager_add_get_list_and_count() -> None:
    """Manager should track items and support session filtering."""
    manager = PendingManager[_PendingItem]()
    loop = asyncio.get_running_loop()
    first = _PendingItem(session_id="s1", future=loop.create_future())
    second = _PendingItem(session_id="s2", future=loop.create_future())

    await manager.add("a", first)
    await manager.add("b", second)

    assert await manager.get("a") is first
    assert await manager.count() == 2
    assert await manager.list("s1") == [first]
    assert await manager.list() == [first, second]


@pytest.mark.asyncio
async def test_pending_manager_resolve_and_wait_for_result() -> None:
    """Resolved items should return their result and be removed afterwards."""
    manager = PendingManager[_PendingItem]()
    item = _PendingItem(session_id="s1", future=asyncio.get_running_loop().create_future())

    await manager.add("a", item)
    assert await manager.resolve("a", {"ok": True}) is item
    assert await manager.wait_for_result("a") == {"ok": True}
    assert await manager.get("a") is None


@pytest.mark.asyncio
async def test_pending_manager_returns_none_for_missing_items() -> None:
    """Missing lookups should be handled without errors."""
    manager = PendingManager[_PendingItem]()

    assert await manager.resolve("missing", "value") is None
    assert await manager.wait_for_result("missing") is None
    assert await manager.pop("missing") is None


@pytest.mark.asyncio
async def test_pending_manager_resolve_returns_none_for_done_future() -> None:
    """Resolving a completed future should drop the stale item."""
    manager = PendingManager[_PendingItem]()
    future = asyncio.get_running_loop().create_future()
    future.set_result("done")
    item = _PendingItem(session_id="s1", future=future)

    await manager.add("a", item)

    assert await manager.resolve("a", "next") is None
    assert await manager.get("a") is None


@pytest.mark.asyncio
async def test_pending_manager_cancel_methods_remove_items() -> None:
    """Single-item and session-wide cancellation should cancel futures and clear storage."""
    manager = PendingManager[_PendingItem]()
    loop = asyncio.get_running_loop()
    first = _PendingItem(session_id="s1", future=loop.create_future())
    second = _PendingItem(session_id="s1", future=loop.create_future())
    third = _PendingItem(session_id="s2", future=loop.create_future())

    await manager.add("a", first)
    await manager.add("b", second)
    await manager.add("c", third)

    assert await manager.cancel("a") is True
    assert first.future is not None and first.future.cancelled()
    assert await manager.cancel_for_session("s1") == 1
    assert second.future is not None and second.future.cancelled()
    assert await manager.get("c") is third
    assert await manager.cancel("missing") is False


@pytest.mark.asyncio
async def test_pending_manager_wait_for_result_handles_cancelled_future() -> None:
    """Cancelled futures should return None and still be removed."""
    manager = PendingManager[_PendingItem]()
    future = asyncio.get_running_loop().create_future()
    future.cancel()
    item = _PendingItem(session_id="s1", future=future)

    await manager.add("a", item)

    assert await manager.wait_for_result("a") is None
    assert await manager.get("a") is None


@pytest.mark.asyncio
async def test_pending_manager_requires_initialized_future() -> None:
    """Missing futures should raise a clear runtime error."""
    manager = PendingManager[_PendingItem]()
    item = _PendingItem(session_id="s1", future=None)

    await manager.add("a", item)

    with pytest.raises(RuntimeError, match="not initialized"):
        await manager.cancel("a")
