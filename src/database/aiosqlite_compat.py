"""Compatibility patch for environments where aiosqlite callbacks may not wake the loop."""

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any, TypeVar

from aiosqlite import core

ResultT = TypeVar("ResultT")
_POLL_INTERVAL_SECONDS = 0.005
_PATCH_APPLIED = False


async def _await_with_polling(future: asyncio.Future[ResultT]) -> ResultT:
    """Wait for a worker-thread future while periodically yielding to the event loop."""
    while True:
        if future.done():
            return future.result()
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def apply_aiosqlite_compatibility_patch() -> None:
    """Patch aiosqlite internals with polling-based waits when needed."""
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return

    async def _patched_connect(self: core.Connection) -> core.Connection:
        if self._connection is None:
            try:
                future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
                self._tx.put_nowait((future, self._connector))
                self._connection = await _await_with_polling(future)
            except BaseException:
                self._stop_running()
                self._connection = None
                raise

        return self

    async def _patched_execute(
        self: core.Connection, fn: Callable[..., ResultT], *args: Any, **kwargs: Any
    ) -> ResultT:
        if not self._running or not self._connection:
            raise ValueError("Connection closed")

        function = partial(fn, *args, **kwargs)
        future: asyncio.Future[ResultT] = asyncio.get_event_loop().create_future()
        self._tx.put_nowait((future, function))

        return await _await_with_polling(future)

    core.Connection._connect = _patched_connect
    core.Connection._execute = _patched_execute
    _PATCH_APPLIED = True
