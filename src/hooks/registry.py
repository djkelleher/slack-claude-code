"""Hook registry for event handling.

Simple dict-based registry with thread-safe operations following project conventions.
"""

import asyncio
import threading
import time
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from .types import HookContext, HookEvent, HookEventType, HookResult

# Type alias for hook handlers
HookHandler = Callable[[HookEvent], Awaitable[Optional[Any]]]


class HookRegistry:
    """Registry for hook handlers.

    Uses simple dict-based storage with RLock for thread safety.
    Handlers are async functions that receive HookEvent and return optional result.
    """

    _handlers: dict[HookEventType, list[tuple[str, HookHandler]]] = {}
    _lock = threading.RLock()

    @classmethod
    def register(
        cls,
        event_type: HookEventType,
        handler: HookHandler,
        name: Optional[str] = None,
    ) -> None:
        """Register a handler for an event type.

        Args:
            event_type: The type of event to handle
            handler: Async function to call when event occurs
            name: Optional name for the handler (for logging)
        """
        handler_name = name or handler.__name__

        with cls._lock:
            if event_type not in cls._handlers:
                cls._handlers[event_type] = []

            cls._handlers[event_type].append((handler_name, handler))
            logger.debug(f"Registered hook handler '{handler_name}' for {event_type.value}")

    @classmethod
    def unregister(
        cls,
        event_type: HookEventType,
        handler: Optional[HookHandler] = None,
        name: Optional[str] = None,
    ) -> bool:
        """Unregister a handler.

        Args:
            event_type: The event type
            handler: The handler function to remove (optional if name provided)
            name: The handler name to remove (optional if handler provided)

        Returns:
            True if handler was found and removed
        """
        with cls._lock:
            if event_type not in cls._handlers:
                return False

            handlers = cls._handlers[event_type]
            for i, (h_name, h_func) in enumerate(handlers):
                if (handler and h_func is handler) or (name and h_name == name):
                    handlers.pop(i)
                    logger.debug(f"Unregistered hook handler '{h_name}' for {event_type.value}")
                    return True

            return False

    @classmethod
    async def emit(cls, event: HookEvent) -> list[HookResult]:
        """Emit an event to all registered handlers.

        Handlers are called concurrently. Errors in one handler don't affect others.

        Args:
            event: The event to emit

        Returns:
            List of results from all handlers
        """
        with cls._lock:
            handlers = cls._handlers.get(event.event_type, []).copy()

        if not handlers:
            return []

        async def run_handler(name: str, handler: HookHandler) -> HookResult:
            start = time.time()
            try:
                result = await handler(event)
                duration_ms = int((time.time() - start) * 1000)
                return HookResult(
                    success=True,
                    handler_name=name,
                    result=result,
                    duration_ms=duration_ms,
                )
            except Exception as e:
                duration_ms = int((time.time() - start) * 1000)
                logger.error(f"Hook handler '{name}' failed: {e}")
                return HookResult(
                    success=False,
                    handler_name=name,
                    error=str(e),
                    duration_ms=duration_ms,
                )

        tasks = [run_handler(name, handler) for name, handler in handlers]
        results = await asyncio.gather(*tasks)

        return list(results)

    @classmethod
    def list_handlers(cls, event_type: Optional[HookEventType] = None) -> dict:
        """List registered handlers.

        Args:
            event_type: Optional filter by event type

        Returns:
            Dict mapping event types to handler names
        """
        with cls._lock:
            if event_type:
                handlers = cls._handlers.get(event_type, [])
                return {event_type.value: [name for name, _ in handlers]}

            return {
                et.value: [name for name, _ in handlers] for et, handlers in cls._handlers.items()
            }

    @classmethod
    def clear(cls, event_type: Optional[HookEventType] = None) -> None:
        """Clear registered handlers.

        Args:
            event_type: Optional - only clear handlers for this type
        """
        with cls._lock:
            if event_type:
                cls._handlers.pop(event_type, None)
            else:
                cls._handlers.clear()


def hook(event_type: HookEventType, name: Optional[str] = None):
    """Decorator to register a function as a hook handler.

    Usage:
        @hook(HookEventType.TOOL_USE)
        async def handle_tool_use(event: HookEvent):
            print(f"Tool used: {event.data}")
    """

    def decorator(func: HookHandler) -> HookHandler:
        HookRegistry.register(event_type, func, name)
        return func

    return decorator


def create_context(
    session_id: str,
    channel_id: Optional[str] = None,
    thread_ts: Optional[str] = None,
    user_id: Optional[str] = None,
    working_directory: Optional[str] = None,
) -> HookContext:
    """Helper factory for creating HookContext.

    Args:
        session_id: The session ID (typically channel_id)
        channel_id: Slack channel ID (defaults to session_id if not provided)
        thread_ts: Optional Slack thread timestamp
        user_id: Optional user ID who initiated the action
        working_directory: Optional working directory for the session

    Returns:
        HookContext instance
    """
    return HookContext(
        session_id=session_id,
        channel_id=channel_id or session_id,
        thread_ts=thread_ts,
        user_id=user_id,
        working_directory=working_directory,
    )
