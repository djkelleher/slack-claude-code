"""Shared async pending-item manager for question/approval workflows."""

import asyncio
from typing import Generic, Optional, Protocol, TypeVar


class PendingItem(Protocol):
    """Protocol for pending items with session scoping and a completion future."""

    session_id: str
    future: Optional[asyncio.Future]


T = TypeVar("T", bound=PendingItem)


class PendingManager(Generic[T]):
    """Thread-safe in-memory manager for pending interactive items."""

    def __init__(self) -> None:
        self._pending: dict[str, T] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    @staticmethod
    def _require_future(item: T) -> asyncio.Future:
        """Return a non-null future for a pending item."""
        if item.future is None:
            raise RuntimeError("Pending item future is not initialized")
        return item.future

    async def add(self, item_id: str, item: T) -> None:
        """Register a pending item by ID."""
        async with self._lock:
            self._pending[item_id] = item

    async def get(self, item_id: str) -> Optional[T]:
        """Get a pending item by ID."""
        async with self._lock:
            return self._pending.get(item_id)

    async def pop(self, item_id: str) -> Optional[T]:
        """Remove and return a pending item by ID."""
        async with self._lock:
            return self._pending.pop(item_id, None)

    async def resolve(self, item_id: str, result: object) -> Optional[T]:
        """Resolve a pending item by setting its future result."""
        async with self._lock:
            item = self._pending.get(item_id)
            if not item:
                return None
            try:
                self._require_future(item).set_result(result)
            except asyncio.InvalidStateError:
                self._pending.pop(item_id, None)
                return None
            return item

    async def cancel(self, item_id: str) -> bool:
        """Cancel and remove a pending item by ID."""
        async with self._lock:
            item = self._pending.get(item_id)
            if not item:
                return False
            future = self._require_future(item)
            if not future.done():
                future.cancel()
            self._pending.pop(item_id, None)
            return True

    async def cancel_for_session(self, session_id: str) -> int:
        """Cancel all pending items for a given session ID."""
        async with self._lock:
            ids = [
                item_id for item_id, item in self._pending.items() if item.session_id == session_id
            ]
            for item_id in ids:
                item = self._pending.get(item_id)
                if item:
                    future = self._require_future(item)
                    if not future.done():
                        future.cancel()
                self._pending.pop(item_id, None)
            return len(ids)

    async def list(self, session_id: Optional[str] = None) -> list[T]:
        """List pending items, optionally filtered by session ID."""
        async with self._lock:
            items = list(self._pending.values())
            if session_id:
                items = [item for item in items if item.session_id == session_id]
            return items

    async def count(self) -> int:
        """Return total count of pending items."""
        async with self._lock:
            return len(self._pending)
