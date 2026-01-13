"""PTY session pool for managing multiple concurrent sessions.

Uses a simple dict-based registry with async-safe operations.
"""

import asyncio
import threading
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from ..config import config
from .session import PTYSession, PTYSessionConfig, SessionResponse, SessionState


class PTYSessionPool:
    """Manages PTY sessions per Slack channel.

    Simple dict-based registry following project conventions.
    Async-safe with asyncio.Lock.
    """

    _sessions: dict[str, PTYSession] = {}
    _lock: Optional[asyncio.Lock] = None
    _cleanup_task: Optional[asyncio.Task] = None
    _init_lock: threading.Lock = threading.Lock()  # Thread-safe lock initialization

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        """Get or create the async lock (must be called from async context).

        Uses double-checked locking pattern with a threading.Lock to ensure
        thread-safe initialization of the asyncio.Lock.
        """
        if cls._lock is None:
            with cls._init_lock:
                if cls._lock is None:  # Double-check after acquiring lock
                    cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    async def get_or_create(
        cls,
        session_id: str,
        working_directory: str = "~",
        on_state_change: Optional[Callable[[str, SessionState], Awaitable[None]]] = None,
        on_output: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> PTYSession:
        """Get existing session or create new one.

        Args:
            session_id: Unique identifier (typically channel_id)
            working_directory: Working directory for the session
            on_state_change: Callback for state changes (session_id, state)
            on_output: Callback for raw output (session_id, output)

        Returns:
            PTYSession instance
        """
        lock = cls._get_lock()
        async with lock:
            if session_id in cls._sessions:
                session = cls._sessions[session_id]
                if session.is_alive():
                    # Return existing session regardless of state (IDLE, BUSY, etc.)
                    # The session's internal lock will serialize access
                    if session.state in (
                        SessionState.IDLE,
                        SessionState.BUSY,
                        SessionState.AWAITING_APPROVAL,
                        SessionState.STARTING,
                    ):
                        return session
                # Clean up dead/stopped/error sessions
                await session.stop()
                del cls._sessions[session_id]

            # Create new session
            session_config = PTYSessionConfig(working_directory=working_directory)

            async def state_callback(state: SessionState) -> None:
                if on_state_change:
                    await on_state_change(session_id, state)

            async def output_callback(data: str) -> None:
                if on_output:
                    await on_output(session_id, data)

            session = PTYSession(
                session_id=session_id,
                config=session_config,
                on_state_change=state_callback,
                on_output=output_callback,
            )

            success = await session.start()
            if not success:
                raise RuntimeError(f"Failed to start PTY session for {session_id}")

            cls._sessions[session_id] = session
            return session

    @classmethod
    async def get(cls, session_id: str) -> Optional[PTYSession]:
        """Get existing session by ID.

        Returns None if session doesn't exist or is dead.
        """
        lock = cls._get_lock()
        async with lock:
            session = cls._sessions.get(session_id)
            if session and session.is_alive():
                return session
            return None

    @classmethod
    async def remove(cls, session_id: str) -> bool:
        """Stop and remove a session.

        Returns True if session was found and removed.
        """
        lock = cls._get_lock()
        async with lock:
            session = cls._sessions.pop(session_id, None)

        if session:
            await session.stop()
            return True
        return False

    @classmethod
    async def list_sessions(cls) -> list[str]:
        """List all session IDs (thread-safe)."""
        lock = cls._get_lock()
        async with lock:
            return list(cls._sessions.keys())

    @classmethod
    def get_session_info(cls, session_id: Optional[str] = None) -> Optional[dict] | list[dict]:
        """Get info about session(s).

        Args:
            session_id: Optional specific session to get info for.
                       If None, returns info for all sessions.

        Returns:
            If session_id provided: dict with session info, or None if not found.
            If no session_id: list of dicts for all sessions.
        """
        # Safe to read without lock - just returns a snapshot
        if session_id is not None:
            session = cls._sessions.get(session_id)
            if session:
                return {
                    "session_id": session.session_id,
                    "state": session.state.value,
                    "working_directory": session.config.working_directory,
                    "created_at": session.created_at.isoformat(),
                    "last_activity": session.last_activity.isoformat(),
                    "is_alive": session.is_alive(),
                    "pid": session.pid,
                }
            return None

        sessions = list(cls._sessions.values())

        return [
            {
                "session_id": s.session_id,
                "state": s.state.value,
                "working_directory": s.config.working_directory,
                "created_at": s.created_at.isoformat(),
                "last_activity": s.last_activity.isoformat(),
                "is_alive": s.is_alive(),
                "pid": s.pid,
            }
            for s in sessions
        ]

    @classmethod
    async def cleanup_all(cls) -> None:
        """Stop all sessions (for shutdown)."""
        session_ids = list(cls._sessions.keys())

        for session_id in session_ids:
            await cls.remove(session_id)

        if cls._cleanup_task and not cls._cleanup_task.done():
            cls._cleanup_task.cancel()
            try:
                await cls._cleanup_task
            except asyncio.CancelledError:
                pass
        cls._cleanup_task = None

    @classmethod
    async def start_cleanup_loop(cls) -> None:
        """Start background cleanup task."""

        async def cleanup_loop():
            while True:
                await asyncio.sleep(config.timeouts.pty.cleanup_interval)
                await cls._cleanup_idle_sessions()

        cls._cleanup_task = asyncio.create_task(cleanup_loop())

    @classmethod
    async def _cleanup_idle_sessions(cls) -> None:
        """Remove sessions that have been idle too long."""
        now = datetime.now()
        session_ids = list(cls._sessions.keys())

        for session_id in session_ids:
            session = cls._sessions.get(session_id)

            if not session:
                continue

            # Check if session is dead
            if not session.is_alive():
                await cls.remove(session_id)
                continue

            # Check idle timeout
            if session.state == SessionState.IDLE:
                idle_time = now - session.last_activity
                idle_timeout = timedelta(seconds=config.timeouts.pty.idle)
                if idle_time > idle_timeout:
                    await cls.remove(session_id)

    @classmethod
    def count(cls) -> int:
        """Get number of active sessions."""
        return len(cls._sessions)

    @classmethod
    async def send_to_session(
        cls,
        session_id: str,
        prompt: str,
        working_directory: str = "~",
        on_chunk: Optional[Callable] = None,
        timeout: float = 300.0,
    ) -> SessionResponse:
        """Send a prompt to a session, creating it if needed.

        This is a convenience method that combines get_or_create and send_prompt.

        Args:
            session_id: Session identifier (typically channel_id)
            prompt: The prompt to send
            working_directory: Working directory if creating new session
            on_chunk: Optional streaming callback
            timeout: Maximum time to wait for response

        Returns:
            SessionResponse with the result
        """
        session = await cls.get_or_create(
            session_id=session_id,
            working_directory=working_directory,
        )

        return await session.send_prompt(
            prompt=prompt,
            on_chunk=on_chunk,
            timeout=timeout,
        )

    @classmethod
    async def interrupt_session(cls, session_id: str) -> bool:
        """Send interrupt (Ctrl+C) to a session."""
        session = await cls.get(session_id)
        if session:
            return await session.interrupt()
        return False

    @classmethod
    async def respond_to_approval(
        cls,
        session_id: str,
        approved: bool,
    ) -> bool:
        """Respond to a permission request in a session."""
        session = await cls.get(session_id)
        if session:
            return await session.respond_to_approval(approved)
        return False
