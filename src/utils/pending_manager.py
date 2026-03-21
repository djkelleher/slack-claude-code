"""Shared async pending-item manager for question/approval workflows."""

import asyncio
from typing import Generic, Optional, Protocol, TypeVar

from loguru import logger


class PendingItem(Protocol):
    """Protocol for pending items with session scoping and a completion future."""

    session_id: str
    future: Optional[asyncio.Future]


T = TypeVar("T", bound=PendingItem)


class PendingManager(Generic[T]):
    """Thread-safe in-memory manager for pending interactive items."""

    def __init__(self) -> None:
        self._pending: dict[str, T] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_lock(self) -> asyncio.Lock:
        """Return a lock bound to the current event loop.

        Pending interactive items cannot be resumed safely across event loops
        because their futures belong to the original loop. If the application
        is reloaded onto a new loop, drop the stale in-memory state.
        """
        current_loop = asyncio.get_running_loop()
        if self._loop is not current_loop:
            if self._loop is not None and self._pending:
                logger.warning("Clearing pending interactive state after event loop change")
                self._pending = {}
            self._loop = current_loop
            self._lock = asyncio.Lock()
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @staticmethod
    def _require_future(item: T) -> asyncio.Future:
        """Return a non-null future for a pending item."""
        if item.future is None:
            raise RuntimeError("Pending item future is not initialized")
        return item.future

    async def add(self, item_id: str, item: T) -> None:
        """Register a pending item by ID."""
        async with self._get_lock():
            self._pending[item_id] = item

    async def get(self, item_id: str) -> Optional[T]:
        """Get a pending item by ID."""
        async with self._get_lock():
            return self._pending.get(item_id)

    async def pop(self, item_id: str) -> Optional[T]:
        """Remove and return a pending item by ID."""
        async with self._get_lock():
            return self._pending.pop(item_id, None)

    async def resolve(self, item_id: str, result: object) -> Optional[T]:
        """Resolve a pending item by setting its future result."""
        async with self._get_lock():
            item = self._pending.get(item_id)
            if not item:
                return None
            try:
                self._require_future(item).set_result(result)
            except asyncio.InvalidStateError:
                self._pending.pop(item_id, None)
                return None
            return item

    async def wait_for_result(self, item_id: str) -> object | None:
        """Wait for a pending item to resolve and then remove it."""
        item = await self.get(item_id)
        if not item:
            return None

        try:
            return await self._require_future(item)
        except asyncio.CancelledError:
            return None
        finally:
            await self.pop(item_id)

    def _cancel_and_remove(self, item_id: str) -> bool:
        """Cancel an item future if needed and remove it from storage."""
        item = self._pending.get(item_id)
        if not item:
            return False

        future = self._require_future(item)
        if not future.done():
            future.cancel()
        self._pending.pop(item_id, None)
        return True

    async def cancel(self, item_id: str) -> bool:
        """Cancel and remove a pending item by ID."""
        async with self._get_lock():
            return self._cancel_and_remove(item_id)

    async def cancel_for_session(self, session_id: str) -> int:
        """Cancel all pending items for a given session ID."""
        async with self._get_lock():
            ids = [
                item_id for item_id, item in self._pending.items() if item.session_id == session_id
            ]
            for item_id in ids:
                self._cancel_and_remove(item_id)
            return len(ids)

    async def list(self, session_id: Optional[str] = None) -> list[T]:
        """List pending items, optionally filtered by session ID."""
        async with self._get_lock():
            items = list(self._pending.values())
            if session_id:
                items = [item for item in items if item.session_id == session_id]
            return items

    async def count(self) -> int:
        """Return total count of pending items."""
        async with self._get_lock():
            return len(self._pending)
