"""PTY session pool for managing multiple concurrent sessions.

Uses a simple dict-based registry with async-safe operations.
"""

import asyncio
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from loguru import logger

from .session import PTYSession
from .types import PTYSessionConfig, SessionState

if TYPE_CHECKING:
    from src.codex.subprocess_executor import ExecutionResult
    from src.codex.streaming import StreamMessage


class PTYSessionPool:
    """Manages PTY sessions per Slack channel/thread.

    Simple dict-based registry following project conventions.
    Async-safe with asyncio.Lock.
    """

    _sessions: dict[str, PTYSession] = {}
    _lock: Optional[asyncio.Lock] = None
    _cleanup_task: Optional[asyncio.Task] = None
    _init_lock: threading.Lock = threading.Lock()

    # Configuration (can be overridden)
    max_sessions: int = 10
    idle_timeout_seconds: float = 1800.0  # 30 minutes
    cleanup_interval_seconds: float = 60.0

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        """Get or create the async lock (must be called from async context)."""
        if cls._lock is None:
            with cls._init_lock:
                if cls._lock is None:
                    cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    def _make_key(cls, channel_id: str, thread_ts: Optional[str]) -> str:
        """Create session key from channel and thread."""
        if thread_ts:
            return f"{channel_id}:{thread_ts}"
        return channel_id

    @classmethod
    async def get_or_create(
        cls,
        channel_id: str,
        thread_ts: Optional[str],
        config: PTYSessionConfig,
        on_state_change: Optional[Callable[[str, SessionState], Awaitable[None]]] = None,
    ) -> PTYSession:
        """Get existing session or create new one.

        Args:
            channel_id: Slack channel ID
            thread_ts: Thread timestamp (None for channel-level session)
            config: Session configuration
            on_state_change: Callback for state changes (session_key, state)

        Returns:
            PTYSession instance
        """
        key = cls._make_key(channel_id, thread_ts)
        lock = cls._get_lock()

        async with lock:
            if key in cls._sessions:
                session = cls._sessions[key]
                if session.is_alive() and session.state in (
                    SessionState.IDLE,
                    SessionState.BUSY,
                    SessionState.STARTING,
                ):
                    logger.debug(f"Reusing existing PTY session: {key}")
                    return session
                # Clean up dead/stopped/error sessions
                logger.info(f"Cleaning up stale PTY session: {key} (state: {session.state.value})")
                await session.stop()
                del cls._sessions[key]

            # Check max sessions limit
            if len(cls._sessions) >= cls.max_sessions:
                # Remove oldest idle session
                oldest_idle = None
                oldest_time = None
                for k, s in cls._sessions.items():
                    if s.state == SessionState.IDLE:
                        if oldest_time is None or s.last_activity < oldest_time:
                            oldest_idle = k
                            oldest_time = s.last_activity

                if oldest_idle:
                    logger.info(f"Evicting oldest idle session: {oldest_idle}")
                    await cls._sessions[oldest_idle].stop()
                    del cls._sessions[oldest_idle]
                else:
                    raise RuntimeError(
                        f"Max sessions ({cls.max_sessions}) reached and no idle sessions to evict"
                    )

            # Create new session
            async def state_callback(state: SessionState) -> None:
                if on_state_change:
                    await on_state_change(key, state)

            session = PTYSession(
                session_id=key,
                config=config,
                on_state_change=state_callback,
            )

            success = await session.start()
            if not success:
                raise RuntimeError(f"Failed to start PTY session for {key}")

            cls._sessions[key] = session
            logger.info(f"Created new PTY session: {key} (total: {len(cls._sessions)})")
            return session

    @classmethod
    async def get(cls, channel_id: str, thread_ts: Optional[str]) -> Optional[PTYSession]:
        """Get existing session by channel/thread.

        Returns None if session doesn't exist or is dead.
        """
        key = cls._make_key(channel_id, thread_ts)
        lock = cls._get_lock()

        async with lock:
            session = cls._sessions.get(key)
            if session and session.is_alive():
                return session
            return None

    @classmethod
    async def remove(cls, channel_id: str, thread_ts: Optional[str]) -> bool:
        """Stop and remove a session.

        Returns True if session was found and removed.
        """
        key = cls._make_key(channel_id, thread_ts)
        lock = cls._get_lock()

        async with lock:
            session = cls._sessions.pop(key, None)

        if session:
            await session.stop()
            logger.info(f"Removed PTY session: {key}")
            return True
        return False

    @classmethod
    async def remove_by_channel(cls, channel_id: str) -> int:
        """Stop and remove all sessions for a channel (including thread sessions)."""
        lock = cls._get_lock()
        prefix = f"{channel_id}:"

        async with lock:
            sessions_to_remove = [
                (key, session)
                for key, session in cls._sessions.items()
                if key == channel_id or key.startswith(prefix)
            ]
            for key, _ in sessions_to_remove:
                cls._sessions.pop(key, None)

        removed_count = 0
        for key, session in sessions_to_remove:
            await session.stop()
            logger.info(f"Removed PTY session: {key}")
            removed_count += 1

        return removed_count

    @classmethod
    async def send_to_session(
        cls,
        channel_id: str,
        thread_ts: Optional[str],
        prompt: str,
        config: PTYSessionConfig,
        on_chunk: Optional[Callable[["StreamMessage"], Awaitable[None]]] = None,
        timeout: float = 216000.0,
    ) -> "ExecutionResult":
        """Send a prompt to a session, creating it if needed.

        Args:
            channel_id: Slack channel ID
            thread_ts: Thread timestamp
            prompt: The prompt to send
            config: Session configuration
            on_chunk: Optional streaming callback
            timeout: Maximum time to wait for response

        Returns:
            ExecutionResult with the result
        """
        session = await cls.get_or_create(
            channel_id=channel_id,
            thread_ts=thread_ts,
            config=config,
        )

        return await session.send_prompt(
            prompt=prompt,
            on_chunk=on_chunk,
            timeout=timeout,
        )

    @classmethod
    async def interrupt_session(cls, channel_id: str, thread_ts: Optional[str]) -> bool:
        """Send interrupt (Ctrl+C) to a session."""
        session = await cls.get(channel_id, thread_ts)
        if session:
            return await session.interrupt()
        return False

    @classmethod
    async def interrupt_by_channel(cls, channel_id: str) -> int:
        """Send interrupt (Ctrl+C) to all sessions in a channel."""
        lock = cls._get_lock()
        prefix = f"{channel_id}:"

        async with lock:
            sessions = [
                session
                for key, session in cls._sessions.items()
                if key == channel_id or key.startswith(prefix)
            ]

        interrupted_count = 0
        for session in sessions:
            if await session.interrupt():
                interrupted_count += 1

        return interrupted_count

    @classmethod
    def get_session_info(
        cls, channel_id: Optional[str] = None, thread_ts: Optional[str] = None
    ) -> Optional[dict] | list[dict]:
        """Get info about session(s).

        Args:
            channel_id: Optional channel ID to filter by
            thread_ts: Optional thread timestamp

        Returns:
            If channel_id provided: dict with session info, or None if not found.
            If no channel_id: list of dicts for all sessions.
        """
        # Take a snapshot to avoid reading the dict while it may be modified
        sessions_snapshot = dict(cls._sessions)

        if channel_id is not None:
            key = cls._make_key(channel_id, thread_ts)
            session = sessions_snapshot.get(key)
            if session:
                return {
                    "session_id": session.session_id,
                    "codex_session_id": session.codex_session_id,
                    "state": session.state.value,
                    "working_directory": session.config.working_directory,
                    "created_at": session.created_at.isoformat(),
                    "last_activity": session.last_activity.isoformat(),
                    "idle_seconds": (datetime.now() - session.last_activity).total_seconds(),
                    "is_alive": session.is_alive(),
                    "pid": session.pid,
                }
            return None

        return [
            {
                "session_id": s.session_id,
                "codex_session_id": s.codex_session_id,
                "state": s.state.value,
                "working_directory": s.config.working_directory,
                "created_at": s.created_at.isoformat(),
                "last_activity": s.last_activity.isoformat(),
                "idle_seconds": (datetime.now() - s.last_activity).total_seconds(),
                "is_alive": s.is_alive(),
                "pid": s.pid,
            }
            for s in sessions_snapshot.values()
        ]

    @classmethod
    async def list_sessions(cls) -> list[str]:
        """List all session keys."""
        lock = cls._get_lock()
        async with lock:
            return list(cls._sessions.keys())

    @classmethod
    def count(cls) -> int:
        """Get number of active sessions.

        Note: len() on a CPython dict is atomic, so no lock needed here.
        """
        return len(cls._sessions)

    @classmethod
    async def start_cleanup_loop(cls, interval: Optional[float] = None) -> None:
        """Start background cleanup task."""
        if interval is not None:
            cls.cleanup_interval_seconds = interval

        async def cleanup_loop():
            while True:
                await asyncio.sleep(cls.cleanup_interval_seconds)
                await cls._cleanup_idle_sessions()

        cls._cleanup_task = asyncio.create_task(cleanup_loop())
        logger.info(
            f"Started PTY cleanup loop (interval: {cls.cleanup_interval_seconds}s, "
            f"idle timeout: {cls.idle_timeout_seconds}s)"
        )

    @classmethod
    async def _cleanup_idle_sessions(cls) -> int:
        """Remove sessions that have been idle too long.

        Returns number of sessions cleaned up.
        """
        now = datetime.now()
        idle_timeout = timedelta(seconds=cls.idle_timeout_seconds)
        keys_to_remove: list[str] = []

        # Collect keys to remove under lock
        lock = cls._get_lock()
        async with lock:
            for key, session in cls._sessions.items():
                # Check if session is dead
                if not session.is_alive():
                    logger.info(f"Cleaning up dead PTY session: {key}")
                    keys_to_remove.append(key)
                    continue

                # Check idle timeout (only for IDLE sessions)
                if session.state == SessionState.IDLE:
                    idle_time = now - session.last_activity
                    if idle_time > idle_timeout:
                        logger.info(
                            f"Cleaning up idle PTY session: {key} "
                            f"(idle for {idle_time.total_seconds():.0f}s)"
                        )
                        keys_to_remove.append(key)

        # Remove outside lock (remove() acquires the lock internally)
        for key in keys_to_remove:
            await cls.remove(*cls._parse_key(key))

        if keys_to_remove:
            logger.info(f"Cleaned up {len(keys_to_remove)} PTY session(s)")

        return len(keys_to_remove)

    @classmethod
    def _parse_key(cls, key: str) -> tuple[str, Optional[str]]:
        """Parse session key back to channel_id and thread_ts."""
        if ":" in key:
            parts = key.split(":", 1)
            return parts[0], parts[1]
        return key, None

    @classmethod
    async def cleanup_all(cls) -> None:
        """Stop all sessions (for shutdown)."""
        # Cancel cleanup task
        if cls._cleanup_task and not cls._cleanup_task.done():
            cls._cleanup_task.cancel()
            try:
                await cls._cleanup_task
            except asyncio.CancelledError:
                pass
        cls._cleanup_task = None

        # Stop all sessions
        keys = list(cls._sessions.keys())
        for key in keys:
            channel_id, thread_ts = cls._parse_key(key)
            await cls.remove(channel_id, thread_ts)

        logger.info("Cleaned up all PTY sessions")
