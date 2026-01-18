"""Centralized async task lifecycle management.

Provides tracking, timeout enforcement, and automatic cleanup
for asyncio tasks across the application.
"""

import asyncio
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger


@dataclass
class TrackedTask:
    """A tracked asyncio task with metadata.

    Parameters
    ----------
    task : asyncio.Task
        The asyncio task being tracked.
    created_at : datetime
        When the task was created.
    channel_id : str
        The Slack channel ID associated with the task.
    task_type : str
        Type of task (e.g., "queue_processor", "workflow").
    timeout_at : datetime, optional
        When the task should be considered timed out.
    """

    task: asyncio.Task
    created_at: datetime
    channel_id: str
    task_type: str
    timeout_at: Optional[datetime] = None

    @property
    def is_expired(self) -> bool:
        """Check if task has exceeded its timeout."""
        if self.timeout_at is None:
            return False
        return datetime.now() > self.timeout_at

    @property
    def is_done(self) -> bool:
        """Check if task has completed."""
        return self.task.done()


class TaskManager:
    """Centralized async task lifecycle management.

    Provides registration, cancellation, and automatic cleanup of tasks.
    Thread-safe through asyncio.Lock with double-checked locking.
    """

    _tasks: dict[str, TrackedTask] = {}
    _lock: Optional[asyncio.Lock] = None
    _init_lock: threading.Lock = threading.Lock()
    _cleanup_task: Optional[asyncio.Task] = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        """Get or create the lock (lazily created for the current event loop).

        Uses double-checked locking pattern with threading lock to ensure
        thread-safe initialization of the asyncio lock.
        """
        if cls._lock is None:
            with cls._init_lock:
                if cls._lock is None:
                    cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    async def register(
        cls,
        task_id: str,
        task: asyncio.Task,
        channel_id: str,
        task_type: str,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        """Register a task for tracking.

        Parameters
        ----------
        task_id : str
            Unique identifier for the task.
        task : asyncio.Task
            The asyncio task to track.
        channel_id : str
            The Slack channel ID associated with the task.
        task_type : str
            Type of task (e.g., "queue_processor", "workflow").
        timeout_seconds : float, optional
            Timeout in seconds after which task should be cancelled.
        """
        timeout_at = None
        if timeout_seconds is not None:
            timeout_at = datetime.now() + timedelta(seconds=timeout_seconds)

        tracked = TrackedTask(
            task=task,
            created_at=datetime.now(),
            channel_id=channel_id,
            task_type=task_type,
            timeout_at=timeout_at,
        )

        async with cls._get_lock():
            # Cancel existing task with same ID if present
            if task_id in cls._tasks:
                existing = cls._tasks[task_id]
                if not existing.task.done():
                    existing.task.cancel()
                    logger.warning(f"Replaced existing task {task_id}")

            cls._tasks[task_id] = tracked

        logger.debug(f"Registered task {task_id} ({task_type}) for channel {channel_id}")

    @classmethod
    async def get(cls, task_id: str) -> Optional[TrackedTask]:
        """Get a tracked task by ID.

        Parameters
        ----------
        task_id : str
            The task ID to look up.

        Returns
        -------
        TrackedTask, optional
            The tracked task if found, None otherwise.
        """
        async with cls._get_lock():
            return cls._tasks.get(task_id)

    @classmethod
    async def get_by_channel(cls, channel_id: str) -> list[TrackedTask]:
        """Get all tasks for a channel.

        Parameters
        ----------
        channel_id : str
            The Slack channel ID.

        Returns
        -------
        list[TrackedTask]
            All tracked tasks for the channel.
        """
        async with cls._get_lock():
            return [t for t in cls._tasks.values() if t.channel_id == channel_id]

    @classmethod
    async def get_by_type(cls, task_type: str) -> list[TrackedTask]:
        """Get all tasks of a specific type.

        Parameters
        ----------
        task_type : str
            The task type to filter by.

        Returns
        -------
        list[TrackedTask]
            All tracked tasks of the specified type.
        """
        async with cls._get_lock():
            return [t for t in cls._tasks.values() if t.task_type == task_type]

    @classmethod
    async def cancel(cls, task_id: str) -> bool:
        """Cancel a task by ID.

        Parameters
        ----------
        task_id : str
            The task ID to cancel.

        Returns
        -------
        bool
            True if task was found and cancelled, False otherwise.
        """
        async with cls._get_lock():
            tracked = cls._tasks.get(task_id)
            if tracked is None:
                return False

            if not tracked.task.done():
                tracked.task.cancel()
                logger.info(f"Cancelled task {task_id}")

            del cls._tasks[task_id]
            return True

    @classmethod
    async def cancel_by_channel(cls, channel_id: str) -> int:
        """Cancel all tasks for a channel.

        Parameters
        ----------
        channel_id : str
            The Slack channel ID.

        Returns
        -------
        int
            Number of tasks cancelled.
        """
        cancelled = 0
        async with cls._get_lock():
            to_remove = []
            for task_id, tracked in cls._tasks.items():
                if tracked.channel_id == channel_id:
                    if not tracked.task.done():
                        tracked.task.cancel()
                        cancelled += 1
                    to_remove.append(task_id)

            for task_id in to_remove:
                del cls._tasks[task_id]

        if cancelled > 0:
            logger.info(f"Cancelled {cancelled} tasks for channel {channel_id}")
        return cancelled

    @classmethod
    async def cleanup_expired(cls) -> int:
        """Remove completed tasks and cancel timed-out tasks.

        Returns
        -------
        int
            Number of tasks cleaned up.
        """
        cleaned = 0
        async with cls._get_lock():
            to_remove = []
            for task_id, tracked in cls._tasks.items():
                # Remove completed tasks
                if tracked.task.done():
                    to_remove.append(task_id)
                    cleaned += 1
                    continue

                # Cancel expired tasks
                if tracked.is_expired:
                    tracked.task.cancel()
                    to_remove.append(task_id)
                    cleaned += 1
                    logger.warning(f"Task {task_id} timed out and was cancelled")

            for task_id in to_remove:
                del cls._tasks[task_id]

        if cleaned > 0:
            logger.debug(f"Cleaned up {cleaned} tasks")
        return cleaned

    @classmethod
    async def start_cleanup_loop(cls, interval: int = 30) -> None:
        """Start background cleanup loop.

        Parameters
        ----------
        interval : int
            Seconds between cleanup runs.
        """
        if cls._cleanup_task is not None and not cls._cleanup_task.done():
            logger.warning("Cleanup loop already running")
            return

        async def cleanup_loop():
            while True:
                await asyncio.sleep(interval)
                try:
                    await cls.cleanup_expired()
                except Exception as e:
                    logger.error(f"Cleanup loop error: {e}")

        cls._cleanup_task = asyncio.create_task(cleanup_loop())
        logger.info(f"Started task cleanup loop (interval={interval}s)")

    @classmethod
    async def stop_cleanup_loop(cls) -> None:
        """Stop the background cleanup loop."""
        if cls._cleanup_task is not None and not cls._cleanup_task.done():
            cls._cleanup_task.cancel()
            try:
                await cls._cleanup_task
            except asyncio.CancelledError:
                pass
            cls._cleanup_task = None
            logger.info("Stopped task cleanup loop")

    @classmethod
    async def clear(cls) -> int:
        """Cancel and remove all tasks. Useful for testing.

        Returns
        -------
        int
            Number of tasks cleared.
        """
        async with cls._get_lock():
            count = len(cls._tasks)
            for tracked in cls._tasks.values():
                if not tracked.task.done():
                    tracked.task.cancel()
            cls._tasks.clear()
            return count

    @classmethod
    async def status(cls) -> dict:
        """Get summary status of all tasks.

        Returns
        -------
        dict
            Summary with counts by type and state.
        """
        async with cls._get_lock():
            by_type = {}
            active = 0
            expired = 0
            done = 0

            for tracked in cls._tasks.values():
                by_type[tracked.task_type] = by_type.get(tracked.task_type, 0) + 1
                if tracked.task.done():
                    done += 1
                elif tracked.is_expired:
                    expired += 1
                else:
                    active += 1

            return {
                "total": len(cls._tasks),
                "active": active,
                "expired": expired,
                "done": done,
                "by_type": by_type,
            }
